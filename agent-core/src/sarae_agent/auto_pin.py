"""Auto-pin TikTok LIVE Console products, in two steps that share one dedicated Chrome profile.

1. Login: the installed Chrome starts as an ordinary process (no Playwright, no automation flags) on
   ``profile_dir`` and the operator signs in by hand, exactly as in a normal browser.
2. Control: once that Chrome has closed (the operator clicks "Đã đăng nhập xong", or closes it), Playwright
   opens the same profile, so cookies and session are reused, and pins products on the LIVE Console.

Chrome cannot run twice on one profile, so step 2 refuses to start while the login Chrome still holds it.
The product list is read from the console itself (scan_products): every row with a Ghim/Pin button is a
product, named by its longest text line. The operator only removes the ones not to pin.

If the console redirects to a login / verification page, auto-pin stops and asks for step 1 again. This
module never types credentials, never touches CAPTCHA or verification dialogs, and adds no fingerprint or
stealth tricks. DOM selectors may need updates when the console UI changes.
"""
from __future__ import annotations

import logging
from contextlib import nullcontext
import os
import queue
import re
import subprocess
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger("sarae.autopin")
LIVE_ORIGINS = {"shop.tiktok.com", "livecenter.tiktok.com", "seller-vn.tiktok.com", "seller.tiktokglobalshop.com",
                "seller-us.tiktok.com"}
DEFAULT_URL = "https://shop.tiktok.com/streamer/live/product/dashboard"
LOGIN_HOSTS = {"www.tiktok.com", "tiktok.com", "m.tiktok.com"}
LOGIN_MARKERS = ("login", "signin", "sign_in", "sign-in", "verify", "verification", "passport", "captcha")
SYNC_RELOADS = 2  # page loads tried before falling back to the visible rows
SYNC_WAIT_MS = 20000  # per load: TikTok often answers its cart several seconds after the page
SYNC_POLL_MS = 200
SYNC_SETTLE_MS = 5000
CONSOLE_NOT_READY = "TikTok chưa trả danh sách sản phẩm (trang tải chậm hoặc giỏ LIVE trống). Đợi trang hiện đủ sản phẩm rồi bấm Đồng bộ lại"
RELOGIN = "TikTok yêu cầu đăng nhập lại: dùng bước 1 (Đăng nhập TikTok bằng Chrome thường)"
LOGIN_CHROME_OPEN = "Chrome đăng nhập vẫn đang mở: bấm \"Đã đăng nhập xong\" hoặc đóng cửa sổ Chrome đó trước"
MAX_PRODUCTS = 100
CHAT_WAIT_SECONDS = 60  # a chat command queued behind a long sync gives up (and is cancelled) after this
PIN_BUTTON = "button.pc_pin_product_pin"
UNPIN_BUTTON = "button.pc_pin_product_unpin"
PIN_CONFIRM_MS = 5000  # after one click, how long TikTok may take to turn "Ghim" into "Bỏ ghim" (never clicked twice)
PIN_CONFIRM_POLL_MS = 250
SCROLL_SETTLE_MS = 300  # the virtual list renders the cards of a new scroll position
MAX_SCROLL_STEPS = 40
CARD_NOT_FOUND = "Không thấy product card của sản phẩm trên LIVE Console (đã cuộn hết danh sách sản phẩm)"
PIN_NOT_CONFIRMED = f"Đã bấm Ghim nhưng TikTok chưa xác nhận sau {PIN_CONFIRM_MS // 1000} giây; không bấm lại"
UNPIN_NOT_CONFIRMED = f"Đã bấm Bỏ ghim nhưng TikTok chưa xác nhận sau {PIN_CONFIRM_MS // 1000} giây; không bấm lại"

# The Console's product list is virtual: only the cards near its visible part are in the page. This finds the list's
# scroll box (the nearest scrollable ancestor of a pin button) to read ({op: "get"}) or set ({op: "set", top}) its
# position; null when the list does not scroll. Scrolling only: nothing is clicked.
CARD_LIST_SCROLL_JS = r"""
({op, top}) => {
  const button = document.querySelector('button.pc_pin_product_pin, button.pc_pin_product_unpin');
  let box = button && button.parentElement;
  while (box && !(box.scrollHeight > box.clientHeight + 4 && /(auto|scroll)/.test(getComputedStyle(box).overflowY))) {
    box = box.parentElement;
  }
  if (!box) return null;
  if (op === 'set') box.scrollTop = top;
  return {ok: true, top: box.scrollTop, max: box.scrollHeight - box.clientHeight,
          step: Math.max(100, Math.floor(box.clientHeight * 0.8))};
}
"""

# Read the card that owns each control. The ordinal is only a short-lived DOM handle, never a product ID.
PIN_CARD_JS = r"""
(target) => {
  document.querySelectorAll('[data-sarae-pin-target]').forEach(e => e.removeAttribute('data-sarae-pin-target'));
  const buttons = [...document.querySelectorAll('button.pc_pin_product_pin, button.pc_pin_product_unpin')];
  const cards = [];
  for (const [index, button] of buttons.entries()) {
    let card = button;
    for (let depth = 0; depth < 9 && card.parentElement; depth++) {
      card = card.parentElement;
      if (card.querySelectorAll('button.pc_pin_product_pin, button.pc_pin_product_unpin').length !== 1) break;
      if (card.querySelector('img') && (card.innerText || '').trim()) break;
    }
    if (card.querySelectorAll('button.pc_pin_product_pin, button.pc_pin_product_unpin').length !== 1) continue;
    if (!card.dataset.saraeCardToken) card.dataset.saraeCardToken = crypto.randomUUID();
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const image = card.querySelector('img');
    const title = card.querySelector('.text-body-m-regular span, [data-testid*="product-name"], [class*="product_name"], [class*="product-name"]');
    // Only product-specific attributes/links are evidence of a TikTok Product ID.
    // Generic data-id, room IDs, seller IDs and numbers in visible text are not.
    const ids = [];
    for (const el of [card, ...card.querySelectorAll('[data-product-id], [data-productid], [data-tiktok-product-id], [href]')]) {
      for (const attr of el.attributes) {
        if (/^data-(?:tiktok-)?product-?id$/i.test(attr.name) && /^[0-9]{12,25}$/.test(attr.value)) {
          ids.push(attr.value);
        } else if (attr.name === 'href') {
          const match = attr.value.match(/(?:[?&](?:product_id|productId)=|\/(?:product|products)\/)([0-9]{12,25})(?:[&#/?]|$)/i);
          if (match) ids.push(match[1]);
        }
      }
    }
    cards.push({index, token: card.dataset.saraeCardToken,
      name: title?.textContent?.trim() || lines.filter(s => !/^(ghim|pin|bỏ ghim|unpin)$/i.test(s) && !/[₫$]/.test(s) && !/^còn:/i.test(s))
        .sort((a,b) => b.length-a.length)[0] || '',
      image_url: image?.currentSrc || image?.src || '',
      price_text: lines.find(s => /[₫$]/.test(s)) || '',
      product_ids: [...new Set(ids.filter(Boolean))],
      pinned: button.matches('button.pc_pin_product_unpin')});
  }
  if (target !== null && cards.some(c => c.token === target)) {
    const chosen = document.querySelector('[data-sarae-card-token="' + target + '"] button.pc_pin_product_pin, [data-sarae-card-token="' + target + '"] button.pc_pin_product_unpin');
    chosen?.setAttribute('data-sarae-pin-target', '1');
  }
  return cards;
}
"""

# Is the broadcast on air? Read only (never clicks). Only the Console's broadcast control bar counts: the row with the
# "Video" / "Kịch bản" switches, which holds a "Phát LIVE" button before LIVE and, while live, an icon-only end
# control (a clickable div holding svg.arco-icon-im_close_chat, a power glyph) instead. Elsewhere on the page the
# "Phát LIVE ngay" button (arco-icon-live) is always shown, so it never counts, and labels match exactly (never by
# prefix). Other labels in LIVE_LABELS are not verified. Both, neither, or several end icons mean UNKNOWN, and the
# timeline pins nothing until it is LIVE (fail closed).
LIVE_LABELS = {"live": ["end live", "kết thúc live", "kết thúc phát trực tiếp", "end stream", "stop live"],
               "offline": ["phát live", "go live", "bắt đầu live", "bắt đầu phát trực tiếp", "start live"]}
LIVE_STATE_JS = r"""
(labels) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // The control bar: the nearest ancestor (up to 6 levels) that also holds the Video / Kịch bản switches.
  const inControlBar = (el) => {
    for (let node = el.parentElement, i = 0; node && i < 6; node = node.parentElement, i++) {
      if (node.querySelector('[role="switch"]')) return true;
    }
    return false;
  };
  const found = {live: [], offline: []};
  const endIcons = [...document.querySelectorAll('div.cursor-pointer > svg.arco-icon-im_close_chat')]
    .filter((svg) => visible(svg.parentElement) && inControlBar(svg.parentElement));
  if (endIcons.length > 1) return {state: 'UNKNOWN', evidence: [`${endIcons.length} end LIVE icons`]};
  if (endIcons.length) found.live.push('icon: arco-icon-im_close_chat');
  for (const el of document.querySelectorAll('button, [role="button"], a')) {
    if (!visible(el) || el.querySelector('svg.arco-icon-live') || !inControlBar(el)) continue;
    const text = norm(el.innerText || el.getAttribute('aria-label'));
    if (!text || text.length > 60) continue;
    for (const key of ['live', 'offline']) {
      if (labels[key].includes(text)) found[key].push(text);
    }
  }
  const state = found.live.length && !found.offline.length ? 'LIVE'
              : found.offline.length && !found.live.length ? 'OFFLINE' : 'UNKNOWN';
  return {state, evidence: [...found.live, ...found.offline].slice(0, 5)};
}
"""

# Runs in the LIVE Console page (read only). A product row is the largest ancestor of a Ghim/Pin button that
# contains no other such button; its name is the longest text line that is not a button label, price or count.
SCAN_PRODUCTS_JS = r"""
() => {
  // TikTok's pre-LIVE dashboard has sortable order inputs, NOT Pin buttons.
  const container = [...document.querySelectorAll('input.pc_order_input, input[role="spinbutton"][data-tid="m4b_input_number"]')];
  const result = [];
  for (const input of container) {
    let root = input;
    for (let i=0; i<9 && root.parentElement; i++) {
      root = root.parentElement;
      if (root.querySelector('img') && root.querySelector('.text-body-m-regular span')) break;
    }
    const title = root.querySelector('.text-body-m-regular span');
    const img = root.querySelector('img');
    const name = (title?.textContent || '').trim();
    if (!name || !img) continue;
    const position = parseInt(input.value, 10);
    const text = (root.innerText || '');
    const priceText = text.match(/[\d.,]+\s*₫/)?.[0] || null;
    const stockText = text.match(/Còn:\s*[^\n]+/)?.[0] || null;
    result.push({
      name, position: Number.isFinite(position) ? position : null,
      image_url: img.currentSrc || img.src || null,
      price_text: priceText, stock_text: stockText
    });
  }
  const scroller = container[0] ? (() => {
    let p = container[0].parentElement;
    while (p && p!==document.body) {
      if (p.scrollHeight > p.clientHeight+30 && getComputedStyle(p).overflowY !== 'visible') return p;
      p=p.parentElement;
    }
    return null;
  })() : null;
  const scroll = scroller ? {top:scroller.scrollTop, height:scroller.scrollHeight, client:scroller.clientHeight} : null;
  if (scroller) scroller.scrollTop = Math.min(scroller.scrollTop + Math.max(100,scroller.clientHeight*0.85),scroller.scrollHeight);
  return {rows:result,scroll};
}
"""


class AccountMismatch(RuntimeError):
    """The LIVE Console is logged in with another TikTok account than the one expected."""


class LoginRequired(RuntimeError):
    """The console sent the controlled browser to a login or verification page."""


def _first_amount(text: str) -> str:
    """Digits of the first amount in a price text: "219.000₫359.000₫" -> "219000", "225000" -> "225000"."""
    match = re.search(r"\d[\d.,\s]*", text or "")
    return re.sub(r"\D", "", match.group(0)) if match else ""


def needs_login(url: str) -> bool:
    """True for TikTok sign-in / verification pages (the operator must log in again in plain Chrome)."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    # On the LIVE Console only the path counts (its query strings may carry arbitrary words).
    where = (parts.path if host in LIVE_ORIGINS else parts.path + "?" + parts.query).lower()
    if host in LOGIN_HOSTS or host.startswith("accounts.") or host.startswith("passport."):
        return True
    return any(marker in where for marker in LOGIN_MARKERS)


def _registry_chrome() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value:
                    return str(value).strip('"')
        except OSError:
            continue
    return None


def find_chrome(env: dict | None = None, exists: Callable[[str], bool] = os.path.isfile,
                registry: Callable[[], str | None] = _registry_chrome) -> str:
    """Installed Google Chrome: App Paths registry entry first, then the standard install folders."""
    env = os.environ if env is None else env
    candidates = [registry()]
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = env.get(var)
        if base:
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    for path in candidates:
        if path and exists(path):
            return path
    raise RuntimeError("Không tìm thấy Google Chrome trên máy; hãy cài Chrome trước")


def _check_url(url: str) -> str:
    u = urlsplit(url)
    if u.scheme != "https" or u.hostname not in LIVE_ORIGINS:
        raise ValueError("Chỉ mở TikTok LIVE Console trên tên miền được hỗ trợ")
    return url


class AutoPinService:
    LOGIN_CLOSE_SECONDS = 15.0

    def __init__(self, profile_dir: Path, *, chrome_finder: Callable[[], str] = find_chrome,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen, chat_sender=None):
        from sarae_agent.tiktok_chat_sender import ChatSender  # it imports needs_login from this module
        self.profile_dir = profile_dir
        self._find_chrome = chrome_finder
        self._popen = popen
        self._lock = threading.RLock()
        self._thread = None
        self._queue: queue.Queue = queue.Queue()
        self._context = None
        self._playwright = None
        self._page = None
        self._login_proc = None
        self._url = DEFAULT_URL
        self._items: list[str] = []
        self._found: list[dict] = []
        self._index = 0
        self._seconds = 30
        self._enabled = False
        self._last_error = ""
        self._last_pin = ""
        self._state = "idle"
        self._product_snapshot = None
        self._network_products = None
        # Product responses whose body is still being read (the read yields to Playwright while it waits).
        self._pending_product_reads = 0
        self._account_profile = None
        self._sync_capture_active = False
        self._observed_pages = set()
        self._chat = chat_sender or ChatSender()  # TikTok LIVE chat replies (docs/AI_AUTO_REPLY.md)
        self._video_auto_enabled = False
        self._video_session_id: int | None = None
        self._video_products: dict[int, dict] = {}
        self._video_revision = 0
        self._pin_state: dict = {"status": "IDLE", "product_id": None, "card_name": None, "action": None, "error": None}

    # --- status --------------------------------------------------------------------------
    def login_open(self) -> bool:
        proc = self._login_proc
        return proc is not None and proc.poll() is None

    def status(self):
        with self._lock:
            login_open = self.login_open()
            if self._state == "login" and not login_open:
                self._state = "logged_in"  # the operator closed the login Chrome: step 2 can start
            return {"state": self._state, "enabled": self._enabled, "interval_seconds": self._seconds,
                    "products": list(self._items), "current_index": self._index,
                    "last_pin": self._last_pin, "last_error": self._last_error,
                    "browser_open": self._context is not None, "login_open": login_open,
                    "found": len(self._found)}

    def pin_status(self) -> dict:
        with self._lock:
            return {**self._pin_state, "auto_enabled": self._video_auto_enabled,
                    "browser_open": self._context is not None}

    def set_video_auto(self, enabled: bool, products: dict[int, dict], session_id: int | None = None) -> dict:
        if enabled and self._enabled:
            self._request("stop")
        with self._lock:
            self._video_revision += 1
            self._video_auto_enabled = enabled
            self._video_session_id = session_id if enabled else None
            self._video_products = {key: dict(value) for key, value in products.items()} if enabled else {}
        return self.pin_status()

    def video_product(self, sarae_product_id: int, session_id: int | None = None) -> dict | None:
        with self._lock:
            if not self._video_auto_enabled or (session_id is not None and session_id != self._video_session_id):
                return None
            target = self._video_products.get(sarae_product_id)
            return dict(target) if target else None

    def pin_product(self, target: dict, current: Callable[[], bool] | None = None, guard=None) -> dict:
        return self._request("pin_product", target, current, guard)

    def unpin_product(self, expected_user_id: str | None = None,
                      current: Callable[[], bool] | None = None, guard=None) -> dict:
        return self._request("unpin_product", expected_user_id, current, guard)

    def get_pin_state(self, target: dict | None = None) -> dict:
        return self._request("get_pin_state", target)

    # Non-blocking forms for the timeline scheduler (sarae_agent/timeline): it runs on the playback watchdog.
    def submit_pin(self, target: dict, current: Callable[[], bool] | None = None, guard=None) -> Future:
        return self._submit("pin_product", target, current, guard)

    def submit_unpin(self, expected_user_id: str | None = None, current: Callable[[], bool] | None = None,
                     guard=None) -> Future:
        return self._submit("unpin_product", expected_user_id, current, guard)

    def submit_pin_state(self) -> Future:
        return self._submit("get_pin_state", None)

    def submit_live_state(self) -> Future:
        return self._submit("live_state")

    def queue_video_pin(self, sarae_product_id: int, current: Callable[[], bool], guard=None,
                        session_id: int | None = None) -> None:
        with self._lock:
            if not self._video_auto_enabled or (session_id is not None and session_id != self._video_session_id):
                return
            target = self._video_products.get(sarae_product_id)
            if not target:
                return
            target = dict(target)
            revision = self._video_revision
            self._pin_state = {"status": "PENDING", "product_id": target.get("tiktok_product_id"),
                               "action": "PIN_PRODUCT", "error": None}
            self._ensure_worker()
        def still_current() -> bool:
            with self._lock:
                fresh = self._video_auto_enabled and self._video_revision == revision
            return fresh and current()
        self._queue.put(("pin_product", (target, still_current, guard), None))

    @staticmethod
    def _matches(card: dict, target: dict) -> bool:
        product_id = target.get("tiktok_product_id")
        if product_id and product_id in card.get("product_ids", []):
            return True
        if product_id and card.get("product_ids"):
            return False
        if " ".join(str(card.get("name") or "").split()).casefold() != " ".join(str(target.get("name") or "").split()).casefold():
            return False  # spaces never decide ("back to school  - ST" == "back to school - ST")
        # Name alone is allowed only when it identifies one card; image and price narrow a duplicate.
        image = target.get("image_url")
        if image and card.get("image_url") and image.split("?")[0] != card["image_url"].split("?")[0]:
            return False
        price = target.get("price_text")
        if price and card.get("price_text") and _first_amount(price) != _first_amount(card["price_text"]):
            return False  # the selling price only: a struck-through original price may follow on the same line
        return True

    @classmethod
    def _matching_cards(cls, cards: list[dict], target: dict) -> list[dict]:
        product_id = target.get("tiktok_product_id")
        exact = [c for c in cards if product_id and product_id in c.get("product_ids", [])]
        return exact if exact else [c for c in cards if cls._matches(c, target)]

    def _resolved_target(self, target: dict) -> dict:
        if target.get("tiktok_product_id"):
            return target
        matches = self._snapshot_matches(target)
        if len(matches) == 1 and matches[0].get("product_id"):
            return {**target, "tiktok_product_id": str(matches[0]["product_id"])}
        return target

    def _snapshot_matches(self, target: dict) -> list[dict]:
        snapshot = self._product_snapshot or {}
        if not snapshot.get("complete"):
            return []
        matches = []
        for row in snapshot.get("products", []):
            candidate = {"name": row.get("name"), "image_url": row.get("image_url") or "",
                         "price_text": row.get("price_text") or "", "product_ids": []}
            if self._matches(candidate, target):
                matches.append(row)
        return matches

    def _console_page(self):
        page = self._page
        if self._context is None or page is None or page.is_closed():
            raise RuntimeError("LIVE Console chưa mở trên Chrome điều khiển")
        if needs_login(page.url):
            raise LoginRequired(RELOGIN)
        if urlsplit(page.url).hostname not in LIVE_ORIGINS:
            raise RuntimeError("Trang hiện tại không phải TikTok LIVE Console")
        return page

    def _cards(self, target: dict | None = None) -> tuple[object, list[dict]]:
        page = self._console_page()
        expected = (target or {}).get("expected_user_id")
        if expected and (self._account_profile or {}).get("user_id") != expected:
            raise AccountMismatch("Chưa xác minh đúng TikTok Account; hãy đồng bộ LIVE Console trước")
        return page, page.evaluate(PIN_CARD_JS, None)

    def _pin_state_on_worker(self, target: dict | None = None) -> dict:
        if target:
            target = self._resolved_target(target)
        try:
            _, cards = self._cards(target)
        except Exception as exc:
            with self._lock:
                self._pin_state = {"status": "FAILED", "product_id": self._pin_state.get("product_id"),
                                   "card_name": self._pin_state.get("card_name"), "action": "GET_PIN_STATE",
                                   "error": str(exc)[:300]}
            raise
        pinned = [c for c in cards if c.get("pinned")]
        candidates = self._matching_cards(cards, target) if target else []
        snapshot_ambiguous = bool(target and len(self._snapshot_matches(target)) > 1 and
            not any(target.get("tiktok_product_id") in c.get("product_ids", []) for c in candidates))
        product_id = pinned[0]["product_ids"][0] if len(pinned) == 1 and len(pinned[0].get("product_ids", [])) == 1 else None
        if len(pinned) == 1 and product_id is None:
            resolved = self._resolved_target({"name": pinned[0].get("name"),
                "image_url": pinned[0].get("image_url"), "price_text": pinned[0].get("price_text")})
            product_id = resolved.get("tiktok_product_id")
        if target is None:
            with self._lock:
                self._pin_state = {"status": "FAILED" if len(pinned) > 1 else "SUCCESS",
                                   "product_id": product_id,
                                   "card_name": pinned[0].get("name") if len(pinned) == 1 else None,
                                   "action": "GET_PIN_STATE",
                                   "error": "Nhiều product card đang ghim" if len(pinned) > 1 else None}
        return {"status": "FAILED" if len(pinned) > 1 else "SUCCESS", "pinned_product_id": product_id,
                "pinned_card": pinned[0] if len(pinned) == 1 else None,
                "candidates": candidates, "ambiguous": len(candidates) > 1 or snapshot_ambiguous,
                "resolved_product_id": target.get("tiktok_product_id") if target else None,
                "multiple_pins": len(pinned) > 1,
                # No pinned card in the page while part of the virtual list is not rendered: a pinned card may be
                # out of view, so "nothing pinned" is not known (the timeline then keeps what it knew).
                "pin_maybe_hidden": not pinned and self._card_list(self._page, "get") is not None}

    def _card_list(self, page, op: str, top: float | None = None) -> dict | None:
        """Position of the Console's scrolling product list (see CARD_LIST_SCROLL_JS); None when it does not scroll."""
        try:
            box = page.evaluate(CARD_LIST_SCROLL_JS, {"op": op, "top": top})
        except Exception:  # noqa: BLE001 - no list (or a page without it) is simply not scrollable
            return None
        return box if isinstance(box, dict) and box.get("ok") else None

    def _scroll_search(self, page, find: Callable[[list[dict]], list[dict]]) -> tuple[list[dict], list[dict], float | None]:
        """Scroll the virtual product list from the top, a page at a time, until find(cards) returns cards.
        Returns (cards, found, origin): origin is the position to scroll back to (None when the list did not scroll).
        Only scrolls: nothing is clicked here."""
        box = self._card_list(page, "get")
        if box is None:
            return [], [], None
        origin, top, cards = box["top"], 0.0, []
        for _ in range(MAX_SCROLL_STEPS):
            box = self._card_list(page, "set", top) or box
            page.wait_for_timeout(SCROLL_SETTLE_MS)
            cards = page.evaluate(PIN_CARD_JS, None)
            found = find(cards)
            if found:
                return cards, found, origin
            if top >= box["max"]:
                break
            top = min(top + box["step"], box["max"])
        return cards, [], origin

    def _restore_scroll(self, page, origin: float | None) -> None:
        if origin is not None and page is not None:
            self._card_list(page, "set", origin)

    def _wait_confirmed(self, page, done: Callable[[dict], bool], target: dict | None = None) -> tuple[dict, bool]:
        """After ONE click: read the Console until done(state) or PIN_CONFIRM_MS passed. Never clicks."""
        deadline = time.monotonic() + PIN_CONFIRM_MS / 1000
        while True:
            state = self._pin_state_on_worker(target)
            if done(state):
                return state, True
            if time.monotonic() >= deadline:
                return state, False
            page.wait_for_timeout(PIN_CONFIRM_POLL_MS)

    def _live_state_on_worker(self) -> dict:
        """LIVE / OFFLINE / UNKNOWN read from the controlled Console page; UNKNOWN when no Console is open."""
        try:
            page = self._console_page()
        except Exception as exc:  # noqa: BLE001 - no page means not confirmed, never an error for the caller
            return {"state": "UNKNOWN", "evidence": [], "error": str(exc)[:200]}
        result = page.evaluate(LIVE_STATE_JS, LIVE_LABELS) or {}
        state = result.get("state") if result.get("state") in ("LIVE", "OFFLINE") else "UNKNOWN"
        return {"state": state, "evidence": list(result.get("evidence") or [])[:5]}

    def _pin_on_worker(self, target: dict, current: Callable[[], bool] | None, guard=None) -> dict:
        product_id = target.get("tiktok_product_id")
        with self._lock:
            self._pin_state = {"status": "PENDING", "product_id": product_id, "action": "PIN_PRODUCT", "error": None}
        try:
            if not product_id and not target.get("confirmed_token") and len(self._snapshot_matches(target)) > 1:
                raise RuntimeError("Nhiều sản phẩm trùng thông tin; cần người dùng xác nhận card")
            target = self._resolved_target(target)
            product_id = target.get("tiktok_product_id")
            scrolled: list = []
            try:
                return self._pin_card(target, product_id, current, guard, scrolled)
            finally:
                if scrolled:
                    self._restore_scroll(*scrolled)  # back to where the operator was looking
        except Exception as exc:
            with self._lock:
                self._pin_state = {"status": "FAILED", "product_id": product_id, "action": "PIN_PRODUCT", "error": str(exc)[:300]}
            log.warning("TikTok pin product=%s result=FAILED at=%s: %s", product_id, time.time(), exc)
            raise

    def _pin_card(self, target: dict, product_id: str | None, current, guard, scrolled: list) -> dict:
        """Find the card (scrolling the virtual list when it is not rendered), click Ghim ONCE, wait for TikTok.
        `scrolled` receives (page, origin) when the list was scrolled, for the caller to scroll back."""
        for attempt in range(3):
            page, cards = self._cards(target)
            matches = self._matching_cards(cards, target)
            if not matches:
                cards, matches, origin = self._scroll_search(page, lambda found: self._matching_cards(found, target))
                if origin is not None and not scrolled:
                    scrolled[:] = [page, origin]
            selected = target.get("confirmed_token")
            if not selected and len(self._snapshot_matches(target)) > 1 and not any(
                    product_id in c.get("product_ids", []) for c in matches):
                raise RuntimeError("Nhiều sản phẩm trùng thông tin; cần người dùng xác nhận card")
            if selected is not None:
                matches = [c for c in matches if c.get("token") == selected]
            if len(matches) > 1:
                raise RuntimeError("Nhiều product card trùng thông tin; cần người dùng xác nhận card")
            if len(matches) == 1:
                card = matches[0]
                clicked = False
                with guard if guard is not None else nullcontext():
                    if current is not None and not current():
                        raise RuntimeError("Video đã chuyển tiếp; hủy lệnh ghim cũ")
                    if not card["pinned"]:
                        page.evaluate(PIN_CARD_JS, card["token"])
                        page.locator("[data-sarae-pin-target='1']").click(timeout=5000)
                        clicked = True
                # Outside the engine lock: TikTok may take a few seconds to show "Bỏ ghim".
                state, confirmed = self._wait_confirmed(
                    page, lambda s: (s.get("pinned_card") or {}).get("token") == card.get("token"), target)
                pinned = state.get("pinned_card")
                if confirmed:
                    result = {"status": "SUCCESS", "product_id": product_id, "card_name": pinned.get("name"), "card": pinned,
                              "action": "PIN_PRODUCT", "error": None}
                    with self._lock:
                        self._pin_state = result
                    log.info("TikTok pin product=%s card=%s result=SUCCESS at=%s", product_id, card["index"], time.time())
                    return result
                if clicked:
                    raise RuntimeError(PIN_NOT_CONFIRMED)  # never a second click: it would unpin again
            if attempt < 2:
                page.wait_for_timeout(500)
        raise RuntimeError(CARD_NOT_FOUND)

    def _unpin_on_worker(self, expected_user_id: str | None = None,
                         current: Callable[[], bool] | None = None, guard=None) -> dict:
        with self._lock:
            self._pin_state = {"status": "PENDING", "product_id": self._pin_state.get("product_id"),
                               "card_name": self._pin_state.get("card_name"), "action": "UNPIN_PRODUCT", "error": None}
        page, origin = None, None
        try:
            page, cards = self._cards({"expected_user_id": expected_user_id} if expected_user_id else None)
            pinned = [c for c in cards if c.get("pinned")]
            if not pinned:  # the pinned card may be out of the rendered part of the virtual list
                cards, pinned, origin = self._scroll_search(page, lambda found: [c for c in found if c.get("pinned")])
            if len(pinned) != 1:
                raise RuntimeError("Không xác định được duy nhất product card đang ghim")
            with guard if guard is not None else nullcontext():
                if current is not None and not current():
                    raise RuntimeError("Phiên LIVE đã kết thúc hoặc chuyển; hủy lệnh bỏ ghim cũ")
                page.evaluate(PIN_CARD_JS, pinned[0]["token"])
                page.locator("[data-sarae-pin-target='1']").click(timeout=5000)
            _, confirmed = self._wait_confirmed(page, lambda s: s["pinned_card"] is None)
            if not confirmed:
                raise RuntimeError(UNPIN_NOT_CONFIRMED)
            result = {"status": "SUCCESS", "product_id": None, "card_name": None,
                      "action": "UNPIN_PRODUCT", "error": None}
            with self._lock:
                self._pin_state = result
            log.info("TikTok unpin result=SUCCESS at=%s", time.time())
            return result
        except Exception as exc:
            with self._lock:
                self._pin_state = {"status": "FAILED", "product_id": self._pin_state.get("product_id"),
                                   "card_name": self._pin_state.get("card_name"), "action": "UNPIN_PRODUCT",
                                   "error": str(exc)[:300]}
            log.warning("TikTok unpin result=FAILED at=%s: %s", time.time(), exc)
            raise
        finally:
            self._restore_scroll(page, origin)  # back to where the operator was looking

    # --- step 1: login in plain Chrome ---------------------------------------------------
    def _profile_locked(self) -> bool:
        """Chrome keeps <profile>/lockfile open without write sharing while it runs on that profile."""
        lock = self.profile_dir / "lockfile"
        if not lock.exists():
            return False
        try:
            os.close(os.open(lock, os.O_RDWR))
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def login_browser(self, url: str = DEFAULT_URL):
        """Start the installed Chrome on the Auto Pin profile for a manual sign-in (no automation at all)."""
        _check_url(url)
        if self.login_open():
            raise RuntimeError("Chrome đăng nhập đang mở; hãy đăng nhập trong cửa sổ đó")
        if self._context is not None:
            self._request("disconnect")  # the profile cannot be open in Playwright and plain Chrome at once
        chrome = self._find_chrome()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._url = url
            self._login_proc = self._popen([chrome, f"--user-data-dir={self.profile_dir}", "--no-first-run",
                                            "--no-default-browser-check", "--new-window", url])
            self._enabled = False
            self._state = "login"
            self._last_error = ""
        log.info("Đã mở Chrome thường để đăng nhập TikTok (pid %s)", getattr(self._login_proc, "pid", "?"))
        return self.status()

    def _close_login_chrome(self) -> None:
        """Close the login Chrome gracefully (taskkill without /F = a normal window close, cookies flushed)."""
        proc = self._login_proc
        if proc is not None and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                proc.wait(timeout=self.LOGIN_CLOSE_SECONDS)
            except subprocess.TimeoutExpired:
                raise RuntimeError(LOGIN_CHROME_OPEN) from None
        deadline = time.monotonic() + self.LOGIN_CLOSE_SECONDS
        while self._profile_locked():  # helper processes may hold the profile a moment longer
            if time.monotonic() > deadline:
                raise RuntimeError(LOGIN_CHROME_OPEN)
            time.sleep(0.3)

    def finish_login(self):
        """Operator signed in: close the login Chrome, then let Playwright take the same profile."""
        self._close_login_chrome()
        return self._request("open", self._url)

    # --- step 2: Playwright control ------------------------------------------------------
    def _ensure_worker(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="auto-pin-browser", daemon=True)
        self._thread.start()

    def _submit(self, method: str, *args) -> Future:
        with self._lock:
            self._ensure_worker()
        result = Future()
        self._queue.put((method, args, result))
        return result

    def _request(self, method: str, *args):
        return self._submit(method, *args).result(timeout=60)

    def open_browser(self, url: str = DEFAULT_URL):
        return self._request("open", _check_url(url))

    def _drop_context(self) -> None:
        for close in (getattr(self._context, "close", None), getattr(self._playwright, "stop", None)):
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        self._context = None
        self._playwright = None
        self._page = None
        self._observed_pages.clear()

    def _landed(self) -> None:
        if self._page is not None and needs_login(self._page.url):
            self._state = "login_required"
            self._last_error = ""
            return
        self._state = "ready"
        self._last_error = ""

    def _capture_account_response(self, response):
        """Capture the logged-in broadcaster's verified account from the page's own XHR, at all times (the chat
        sender checks the account with it); product responses are still read only during a sync."""
        if urlsplit(response.url).path != "/api/v1/streamer_desktop/account_info/get":
            return
        if response.status != 200:
            return
        try:
            body = response.json()
            data = body.get("data") or {}
            uid = str(data.get("user_id") or "")
            if body.get("code") != 0 or not uid.isdigit():
                return
            avatar = (data.get("avatar") or {}).get("url_list") or []
            self._account_profile = {
                "user_id": uid,
                "tt_uid": str(data.get("tt_uid") or ""),
                "user_name": str(data.get("user_name") or ""),
                "nick_name": str(data.get("nick_name") or ""),
                "user_type": data.get("user_type"),
                "avatar_url": avatar[0] if avatar else "",
            }
            log.info("Verified TikTok LIVE account user_id=%s", uid)
        except Exception as exc:
            log.warning("Không đọc được TikTok account_info: %s", exc)

    def _capture_product_response(self,response):
        """Observe product responses only during an explicit manual sync."""
        if not self._sync_capture_active:
            return
        from urllib.parse import urlsplit
        if urlsplit(response.url).path!="/api/v1/streamer_desktop/live_product/list":
            return
        if response.status!=200:return
        self._pending_product_reads+=1
        try:
            body=response.json()
            if body.get("code")!=0:return
            raw=body.get("data",{}).get("products")
            if not isinstance(raw,list):return
            result=[]
            for x in raw:
                seller=str((x.get("seller_info") or {}).get("seller_id") or "")
                pid=str(x.get("product_id") or "")
                name=str(x.get("title") or "").strip()
                if not seller.isdigit() or not pid.isdigit() or not name:continue
                cover=(x.get("cover") or {}).get("url_list") or []
                category=x.get("category_info") or {}
                result.append({"seller_id":seller,"product_id":pid,"name":name,
                               "image_url":cover[0] if cover else None,
                               "price_text":x.get("format_available_price"),
                               "origin_price_text":x.get("format_origin_price"),
                               "stock_num":x.get("stock_num"),
                               "category_name":category.get("name")})
            if len(result)==len(raw):
                self._network_products=result
                log.info("TikTok Network captured %s products",len(result))
        except Exception as exc:
            log.warning("Không đọc được TikTok products response: %s",exc)
        finally:
            self._pending_product_reads-=1

    def _attach_response_listener(self,page):
        if id(page) not in self._observed_pages:
            page.on("response",self._capture_product_response)
            page.on("response",self._capture_account_response)
            self._observed_pages.add(id(page))

    def _open_on_worker(self, url):
        if self.login_open() or self._profile_locked():
            raise RuntimeError(LOGIN_CHROME_OPEN)
        self._url = url
        self._network_products = None
        self._sync_capture_active = False
        if self._context is not None:
            try:
                if self._page is None or self._page.is_closed():
                    pages = [p for p in self._context.pages if not p.is_closed()]
                    self._page = pages[0] if pages else self._context.new_page()
                self._attach_response_listener(self._page)
                self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                # A closed Chrome context cannot be reused.
                self._drop_context()
                raise RuntimeError("Chrome đã đóng hoặc trang bị ngắt khi điều hướng; hãy bấm lại Bắt đầu")
            self._landed()
            return self.status()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Thiếu playwright trong Agent Core") from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir), channel="chrome", headless=False,
                accept_downloads=False, viewport=None)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._attach_response_listener(self._page)
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            self._drop_context()
            self._last_error = str(exc)[:300]
            raise
        self._landed()
        return self.status()

    def scan_products(self, expected_user_id: str | None = None):
        """Read the products of the open LIVE Console (account_info + live_product/list).

        With `expected_user_id`, a Console logged in with another account raises AccountMismatch and changes
        nothing: the pin list and the snapshot keep what they had."""
        return self._submit("scan", expected_user_id).result(timeout=120)

    def _wait_for_console(self, page) -> bool:
        """Wait until the console's own account_info and product list responses were read (bodies included)."""
        waited = 0
        while waited < SYNC_WAIT_MS:
            if self._network_products is not None and self._account_profile is not None and not self._pending_product_reads:
                return True
            page.wait_for_timeout(SYNC_POLL_MS)
            waited += SYNC_POLL_MS
        # A body read that started just before the deadline still counts once it finishes.
        settle = 0
        while self._pending_product_reads and settle < SYNC_SETTLE_MS:
            page.wait_for_timeout(SYNC_POLL_MS)
            settle += SYNC_POLL_MS
        return self._network_products is not None and self._account_profile is not None

    def _scan_on_worker(self, expected_user_id: str | None = None) -> dict:
        """Read the console's own product response (or, on old consoles, visible DOM rows) — read-only."""
        page=self._page
        if self._context is None or page is None:
            raise RuntimeError("Chưa mở Chrome điều khiển: đăng nhập rồi bấm đồng bộ")
        if needs_login(page.url):
            self._state = "login_required"
            raise LoginRequired(RELOGIN)
        if urlsplit(page.url).hostname not in LIVE_ORIGINS:
            raise RuntimeError("Trang hiện tại không phải TikTok LIVE Console")
        if not page.url.startswith(DEFAULT_URL):
            raise RuntimeError("Hãy mở đúng trang TikTok LIVE Product Dashboard")
        # A fresh scan must not reuse a prior shop's response. Refresh once to trigger
        # the console's own authenticated XHR; never reconstruct its signed URL.
        self._network_products = None
        self._account_profile = None
        self._attach_response_listener(page)
        self._sync_capture_active = True
        try:
            # The console loads its cart a few seconds after the page itself; a slow answer gets one more reload.
            for attempt in range(SYNC_RELOADS):
                page.reload(wait_until="domcontentloaded", timeout=30000)
                if self._wait_for_console(page):
                    break
                log.info("TikTok chưa trả danh sách sản phẩm sau lần tải %s; tải lại trang", attempt + 1)
        finally:
            self._sync_capture_active = False
        if self._network_products is not None:
            if not self._account_profile:
                raise RuntimeError("Không xác minh được user_id TikTok hiện tại từ account_info/get; chưa đồng bộ sản phẩm")
            if expected_user_id is not None and self._account_profile.get("user_id") != expected_user_id:
                # Checked before anything is kept: the wrong account's cart never becomes the pin list.
                raise AccountMismatch("TikTok LIVE Console đang đăng nhập tài khoản khác với phiên LIVE")
            rows=list(self._network_products)
            with self._lock:
                self._found=[{"name":x["name"],"pinned":False} for x in rows]
                self._items=[x["name"] for x in rows[:MAX_PRODUCTS]]
                self._product_snapshot={"complete":True,"products":rows}
            return {**self.status(),"products_found":self._found,
                    "product_snapshot":self._product_snapshot, "account_profile":self._account_profile}
        # Old console revisions: retain read-only DOM diagnostics. The Cloud import
        # refuses DOM-only rows because they have no trustworthy TikTok IDs.
        found={}
        complete=False
        last=None
        stagnant=0
        for _ in range(150):
            part=page.evaluate(SCAN_PRODUCTS_JS)
            rows=part.get("rows",[])
            scroll=part.get("scroll")
            for row in rows:
                name=str(row.get("name") or "").strip()
                if not name:continue
                key=name.casefold()
                if key not in found:found[key]=row
            if not rows:break
            if not scroll:
                complete=True
                break
            signature=(round(scroll["top"]),len(found))
            stagnant=stagnant+1 if signature==last else 0
            last=signature
            if scroll["top"]+scroll["client"] >= scroll["height"]-5:
                complete=True
                break
            if stagnant>=4:break
            page.wait_for_timeout(120)
        rows=sorted(found.values(),key=lambda x:x.get("position") or 999999)
        if not rows:
            raise RuntimeError(CONSOLE_NOT_READY)
        if expected_user_id is not None:
            # Without account_info the logged-in account cannot be verified for a session sync.
            raise RuntimeError("Không đọc được account_info/get của TikTok; chưa xác minh được tài khoản để đồng bộ")
        with self._lock:
            self._found=[{"name":x["name"],"pinned":False} for x in rows]
            self._items=[x["name"] for x in rows[:MAX_PRODUCTS]]
            self._index=0
            # DOM rows have no TikTok IDs: diagnostics and pin names only, never a complete Cloud snapshot.
            self._product_snapshot={"complete":False,"dom_complete":complete and len(rows)<=1000,"products":rows}
        log.info("TikTok DOM scan: %s sản phẩm, complete=%s",len(rows),complete)
        return {**self.status(),"products_found":self._found,
                "product_snapshot":self._product_snapshot}

    def configure(self, products: list[str], interval_seconds: int):
        if not 5 <= interval_seconds <= 3600:
            raise ValueError("Chu kỳ phải từ 5 đến 3600 giây")
        cleaned = [v.strip() for v in products if v.strip()]
        if not cleaned or len(cleaned) > MAX_PRODUCTS:
            raise ValueError("Cần 1–100 mã hoặc tên sản phẩm")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Danh sách sản phẩm trùng nhau")
        with self._lock:
            self._items = cleaned
            self._seconds = interval_seconds
            self._index = 0
        return self.status()

    def _pin(self, term: str):
        page = self._page
        if page is None:
            raise RuntimeError("Chưa mở Chrome điều khiển")
        if needs_login(page.url):
            raise LoginRequired(RELOGIN)
        if page.url.startswith("chrome://"):
            raise RuntimeError("Chưa mở LIVE Console")
        if urlsplit(page.url).hostname not in LIVE_ORIGINS:
            raise RuntimeError("Trang hiện tại không phải TikTok LIVE Console")
        # Target a product row by its visible product name/ID and click its Pin button.
        # Avoid guessing coordinates and avoid interacting with challenge dialogs.
        row = page.get_by_text(term, exact=True).first
        if row.count() == 0:
            raise RuntimeError("Không tìm thấy sản phẩm: " + term)
        candidate = row.locator("xpath=ancestor::*[.//button][1]")
        button = candidate.get_by_role("button", name=re.compile(r"^(pin|ghim)$", re.I))
        if button.count() != 1:
            raise RuntimeError("Không xác định được duy nhất nút Ghim cho " + term)
        button.click(timeout=5000)
        # Re-check that the button changed state, rather than reporting blind success.
        unpin = candidate.get_by_role("button", name=re.compile(r"^(unpin|bỏ ghim|bỏ ghim sản phẩm)$", re.I))
        unpin.wait_for(state="visible", timeout=5000)
        return term

    def _run(self):
        next_pin = None
        while True:
            timeout = max(0, next_pin - time.monotonic()) if next_pin is not None else None
            try:
                method, args, future = self._queue.get(timeout=timeout)
            except queue.Empty:
                method, args, future = "tick", (), None
            try:
                if method == "open":
                    result = self._open_on_worker(*args)
                elif method == "scan":
                    result = self._scan_on_worker(*args)
                elif method == "chat_send":
                    result = self._chat_send_on_worker(*args)
                elif method == "chat_probe":
                    result = self._chat_probe_on_worker()
                elif method == "pin_product":
                    result = self._pin_on_worker(*args)
                elif method == "unpin_product":
                    result = self._unpin_on_worker(*args)
                elif method == "get_pin_state":
                    result = self._pin_state_on_worker(*args)
                elif method == "live_state":
                    result = self._live_state_on_worker()
                elif method == "start":
                    if self._video_auto_enabled:
                        raise RuntimeError("Đang bật tự động ghim theo video; hãy tắt trên Web trước")
                    if not self._items:
                        raise RuntimeError("Hãy bấm Đồng bộ sản phẩm trước khi tự động ghim")
                    if self._context is None:
                        self._open_on_worker(self._url)  # step 2 after the operator closed the login Chrome
                    self._enabled = True
                    self._state = "running"
                    next_pin = time.monotonic()
                    result = self.status()
                elif method == "stop":
                    self._enabled = False
                    self._state = "stopped"
                    next_pin = None
                    result = self.status()
                elif method == "disconnect":
                    self._enabled = False
                    next_pin = None
                    self._drop_context()
                    result = {"ok": True}
                elif method == "tick":
                    if not self._enabled or not self._items:
                        next_pin = None
                        continue
                    term = self._items[self._index]
                    try:
                        self._last_pin = self._pin(term)
                        self._last_error = ""
                        self._index = (self._index + 1) % len(self._items)
                        self._state = "running"
                        next_pin = time.monotonic() + self._seconds
                    except LoginRequired as exc:
                        self._last_error = str(exc)
                        self._state = "login_required"
                        self._enabled = False
                        next_pin = None
                        log.warning("Auto Pin dừng: TikTok chuyển sang trang đăng nhập / xác minh")
                    except Exception as exc:  # noqa: BLE001
                        self._last_error = str(exc)[:300]
                        self._state = "attention"
                        self._enabled = False
                        next_pin = None
                        log.warning("Auto Pin dừng: %s", exc)
                    continue
                elif method == "close":
                    self._enabled = False
                    self._drop_context()
                    result = {"ok": True}
                else:
                    raise ValueError("Unknown Auto Pin operation")
                if future:
                    future.set_result(result)
                if method == "close":
                    return
            except Exception as exc:  # noqa: BLE001
                if future:
                    future.set_exception(exc)

    # --- TikTok LIVE chat replies (Cloud lane "chat") ----------------------------------------
    def chat_send(self, payload: dict) -> dict:
        """Post one reply as a LIVE chat message on the open Console page (never opens a browser)."""
        from sarae_agent.tiktok_chat_sender import FAILED, ChatRequest
        request = ChatRequest.from_payload(payload, received_at=self._chat.outbox.now())
        job = {"lock": threading.Lock(), "started": False, "cancelled": False}
        future = self._submit("chat_send", request, job)
        try:
            return future.result(timeout=CHAT_WAIT_SECONDS)
        except FutureTimeout:
            with job["lock"]:
                if not job["started"]:
                    job["cancelled"] = True  # the worker skips it: nothing is sent after we said FAILED
            if not job["cancelled"]:
                return future.result(timeout=CHAT_WAIT_SECONDS)  # already typing: wait for the real outcome
            error = "Trình duyệt Auto Pin đang bận (đồng bộ / ghim); chưa gửi"
            log.info("chat_send reply=%s status=FAILED error=%s", request.reply_id, error)
            return {"status": FAILED, "mode": None, "error": error,
                    "steps": [{"step": "QUEUED", "ms": 0}, {"step": FAILED, "ms": CHAT_WAIT_SECONDS * 1000}]}

    def chat_probe(self) -> dict:
        """What the chat sender finds on the open Console (composer, list, reply controls...), nothing typed."""
        return self._request("chat_probe")

    def _chat_page(self):
        """(open LIVE Console page, None) or (None, reason). Never launches or navigates a browser."""
        page = self._page
        if self._context is not None and (page is None or page.is_closed()):
            try:
                pages = [p for p in self._context.pages
                         if not p.is_closed() and urlsplit(p.url).hostname in LIVE_ORIGINS]
            except Exception:  # noqa: BLE001 - Chrome was closed by hand
                pages = []
            page = self._page = pages[0] if pages else None
        if self._context is None or page is None:
            return None, "LIVE Console chưa mở"
        if needs_login(page.url):
            self._state = "login_required"
            return None, RELOGIN
        if urlsplit(page.url).hostname not in LIVE_ORIGINS:
            return None, "Trang hiện tại không phải TikTok LIVE Console"
        self._attach_response_listener(page)
        return page, None

    def _chat_send_on_worker(self, request, job) -> dict:
        from sarae_agent.tiktok_chat_sender import FAILED
        with job["lock"]:
            if job["cancelled"]:
                return {"status": FAILED, "mode": None, "error": "Đã huỷ vì chờ quá lâu", "steps": []}
            job["started"] = True
        page, error = self._chat_page()
        if page is None:
            result = {"status": FAILED, "mode": None, "error": error,
                      "steps": [{"step": "QUEUED", "ms": 0}, {"step": FAILED, "ms": 0, "detail": error}]}
        else:
            profile = self._account_profile or {}
            result = self._chat.send(page, request, profile.get("user_id"),
                                     account_names=(profile.get("nick_name") or "", profile.get("user_name") or ""))
        log.info("chat_send reply=%s session=%s mode=%s status=%s error=%s steps=%s", request.reply_id,
                 request.session_id, result.get("mode"), result.get("status"), result.get("error"),
                 result.get("steps"))
        return result

    def _chat_probe_on_worker(self) -> dict:
        page, error = self._chat_page()
        profile = self._account_profile or {}
        account = {"verified": bool(profile.get("user_id")), "user_id": profile.get("user_id"),
                   "user_name": profile.get("user_name")}
        if page is None:
            return {"ok": False, "error": error, "account": account, "groups": {}}
        return {**self._chat.probe(page), "account": account}

    def start(self):
        return self._request("start")

    def stop(self):
        return self._request("stop")

    def close(self):
        if self._thread and self._thread.is_alive():
            return self._request("close")
        return {"ok": True}

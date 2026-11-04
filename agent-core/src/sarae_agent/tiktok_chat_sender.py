"""Post an AI reply as a TikTok LIVE chat message from the open LIVE Console (docs/AI_AUTO_REPLY.md).

Runs on the Auto Pin browser worker (same Chrome profile and page, never a second browser):

    checks (fail closed) ─► DIRECT_REPLY ─► MENTION ─► NORMAL ─► type ─► Enter / Send ─► CONFIRMED | SUBMITTED

* ``ChatOutbox`` (pure, injectable clock): TTL, duplicates, pacing. Nothing is typed when it says no.
* ``ChatSender`` drives the page through Playwright locators only (no coordinates, no page scripts). Selectors
  are lists of candidates per element, roles/labels/text first, then attributes; ``tiktok_chat_selectors.json``
  in the Agent data folder overrides them (merged over the defaults) so they can be fixed without a release.
* Never touches CAPTCHA / verification / limits: a verification page or a "too fast" notice stops the sender.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sarae_agent.auto_pin import needs_login

log = logging.getLogger("sarae.chat")

CONFIRMED, SUBMITTED, FAILED = "CONFIRMED", "SUBMITTED", "FAILED"
EXPIRED, DUPLICATE, RATE_LIMITED = "EXPIRED", "DUPLICATE", "RATE_LIMITED"
DIRECT_REPLY, MENTION, NORMAL = "DIRECT_REPLY", "MENTION", "NORMAL"

SELECTORS_FILE = "tiktok_chat_selectors.json"
SAME_TEXT_WINDOW_SECONDS = 600.0  # same text to the same viewer once per 10 minutes
UNVERIFIED_ACCOUNT = "Chưa xác minh được TikTok Account: mở LIVE Console và bấm Đồng bộ một lần"
ACCOUNT_MISMATCH = "TikTok LIVE Console đang đăng nhập tài khoản khác với phiên LIVE"
CONSOLE_CLOSED = "LIVE Console chưa mở"
VERIFICATION_PAGE = "TikTok đang hiện trang đăng nhập / xác minh: hãy xử lý bằng tay rồi thử lại"
USERNAME = re.compile(r"^[A-Za-z0-9._]{1,64}$")
REPLY_ICON = "button:has(> svg.arco-icon-recover)"  # the ↩ of the LIVE Console's hover popup (never 📌 / …)
MENTION_TOKEN = re.compile(r"@([A-Za-z0-9._]+)")

_LABEL_CHAT = r"bình luận|trò chuyện|nói gì|nhập tin nhắn|comment|chat|say something|add a comment|type a message"
# LIVE Console (Seller Center, arco/m4b UI): the "Chat" panel is the nearest box around the "Chat" title that also
# holds a textarea; its composer is a placeholder-only textarea and Send is a <span> wrapping the publish icon.
_CHAT_PANEL = ("xpath=//p[normalize-space()='Chat' or normalize-space()='Trò chuyện' or normalize-space()='Bình luận']"
               "/ancestor::div[.//textarea][1]")
# Candidates are Playwright selectors, tried in order; the first one that finds a visible element wins.
DEFAULT_SELECTORS: dict[str, list[str]] = {
    "composer": [
        f"role=textbox[name=/{_LABEL_CHAT}/i]",
        f"{_CHAT_PANEL}//textarea",
        "[data-e2e*='comment-input' i]", "[data-e2e*='chat-input' i]",
        "[class*='chat' i] textarea", "[class*='comment' i] textarea",
        "[class*='chat' i] [contenteditable='true']", "[class*='comment' i] [contenteditable='true']",
    ],
    "send_button": [
        "role=button[name=/^\\s*(gửi|send|đăng|post)\\s*$/i]",
        "[data-e2e*='send' i]", "button[class*='send' i]",
        f"{_CHAT_PANEL}//span[./span/*[local-name()='svg' and contains(@class, 'arco-icon-publish')]]"
        "[not(contains(@class, 'cursor-not-allowed'))]",
    ],
    "chat_list": [
        "[data-e2e*='chat-list' i]", "[data-e2e*='comment-list' i]", f"{_CHAT_PANEL}//*[@role='list']",
        "role=log", f"role=list[name=/{_LABEL_CHAT}/i]",
        "[class*='chat-list' i]", "[class*='comment-list' i]", "[class*='chatList' i]", "[class*='commentList' i]",
    ],
    "message_row": [
        "[data-e2e*='chat-message' i]", "[data-e2e*='comment-item' i]",
        "role=listitem", ".arco-list-item",
        # LIVE Console comment: clickable box = [avatar + name] line, then the text line ("just joined" rows differ)
        "div.cursor-pointer:not([type]):has(> div > [data-tid='m4b_avatar']):has(> div.text-neutral-text1)",
        "[class*='message-item' i]", "[class*='comment-item' i]", "[class*='chat-item' i]",
    ],
    "row_text": [
        "[data-e2e*='message-content' i]", "[data-e2e*='comment-content' i]", "[data-e2e*='content' i]",
        ":scope > div.text-neutral-text1",
        "[class*='content' i]", "[class*='comment-text' i]", "[class*='message-text' i]",
    ],
    "row_author": [
        "[data-e2e*='nickname' i]", "[data-e2e*='username' i]", "[data-e2e*='author' i]",
        "div:has(> [data-tid='m4b_avatar']) > div.truncate",
        "[class*='nickname' i]", "[class*='user-name' i]", "[class*='username' i]", "[class*='author' i]",
    ],
    "reply_control": [
        "role=button[name=/^\\s*(trả lời|reply)\\s*$/i]",
        # LIVE Console: hover popup [reply ↩ | pin 📌 | more …]; only the ↩ icon (arco "recover") is Reply
        "button:has(> svg.arco-icon-recover)",  # == REPLY_ICON
        "text=/^\\s*(Trả lời|Reply)\\s*$/i", "[data-e2e*='reply' i]", "[class*='reply' i]",
    ],
    "mention_list": [
        "role=listbox", "[data-e2e*='mention' i]", "[class*='mention' i]", "[class*='suggest' i]",
    ],
    "mention_item": [
        "role=option", "[data-e2e*='mention-item' i]", "[class*='mention-item' i]", "[class*='suggest-item' i]",
        "li",
    ],
    "own_marker": [
        "[data-e2e*='self' i]", "[data-e2e*='anchor' i]", "[data-e2e*='host' i]",
        "[class*='is-self' i]", "[class*='isSelf' i]", "[class*='anchor' i]",
        "text=/^\\s*(Bạn|Chủ phòng|Người phát|You|Host|Streamer)\\s*$/i",
    ],
    "chat_disabled": [
        "text=/^\\s*(bình luận|trò chuyện|chat)( trực tiếp| live)? (đã bị tắt|đã tắt|bị tắt|đã bị vô hiệu hóa)\\.?\\s*$/i",
        "text=/^\\s*(comments?|chat) (are |is |has been )?(turned off|disabled|closed)\\.?\\s*$/i",
        "[data-e2e*='chat-disabled' i]", "[data-e2e*='comment-disabled' i]",
    ],
    "rate_notice": [
        "text=/quá nhanh|quá thường xuyên|chậm lại|thử lại sau|too fast|too frequent|slow down|try again later/i",
    ],
    "verification": [
        "iframe[src*='captcha' i]", "[id*='captcha' i]", "[class*='captcha' i]",
        "text=/xác minh bạn là người|kéo thanh trượt|verify you are human|drag the slider/i",
    ],
}


def normalize(text: str | None) -> str:
    return " ".join(str(text or "").split()).casefold()


def load_selectors(path: Path | None) -> dict[str, list[str]]:
    """Defaults merged with the override file (group -> candidate list). An invalid file is logged and ignored."""
    merged = {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
    if path is None or not Path(path).is_file():
        return merged
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("phải là object {nhóm: [selector, ...]}")
        for group, value in data.items():
            if group not in DEFAULT_SELECTORS:
                raise ValueError(f"nhóm không hỗ trợ: {group}")
            if not isinstance(value, list) or not value or not all(isinstance(s, str) and s.strip() for s in value):
                raise ValueError(f"{group} phải là danh sách selector không rỗng")
        for group, value in data.items():
            merged[group] = [s.strip() for s in value]
    except (OSError, ValueError) as exc:
        log.warning("Bỏ qua %s (dùng selector mặc định): %s", path, exc)
        return {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
    return merged


# --- request + outbox ------------------------------------------------------------------------
@dataclass
class ChatTarget:
    msg_id: str | None = None
    user_id: str | None = None
    username: str | None = None
    nickname: str | None = None
    comment: str | None = None

    @property
    def viewer_key(self) -> str:
        return str(self.user_id or "") or normalize(self.username)


@dataclass
class ChatRequest:
    reply_id: int
    session_id: int
    expected_user_id: str
    ttl_seconds: float
    text: str
    target: ChatTarget = field(default_factory=ChatTarget)
    min_interval_seconds: float = 8.0
    max_per_minute: int = 5
    received_at: float | None = None  # outbox clock when the Agent received the command

    @classmethod
    def from_payload(cls, payload: dict, received_at: float | None = None) -> "ChatRequest":
        target = ChatTarget(**{k: (None if v is None else str(v)) for k, v in (payload.get("target") or {}).items()})
        return cls(reply_id=int(payload["reply_id"]), session_id=int(payload["session_id"]),
                   expected_user_id=str(payload["expected_user_id"]), ttl_seconds=float(payload["ttl_seconds"]),
                   text=str(payload["text"]), target=target,
                   min_interval_seconds=float(payload.get("min_interval_seconds") or 8.0),
                   max_per_minute=int(payload.get("max_per_minute") or 5), received_at=received_at)


class ChatOutbox:
    """Duplicate guard, pacing and TTL. ``check`` decides, ``record`` notes a message that was submitted."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._msg_ids: dict[str, float] = {}
        self._texts: dict[tuple[str, str], float] = {}
        self._sent: deque[float] = deque()

    def now(self) -> float:
        return self._clock()

    def expired(self, request: ChatRequest) -> bool:
        return request.received_at is not None and self._clock() - request.received_at > request.ttl_seconds

    def check(self, request: ChatRequest) -> tuple[str, str] | None:
        now = self._clock()
        with self._lock:
            self._prune(now)
            if self.expired(request):
                return EXPIRED, f"Câu trả lời đã quá {request.ttl_seconds:g} giây, không gửi nữa"
            msg_id = request.target.msg_id
            if msg_id and msg_id in self._msg_ids:
                return DUPLICATE, "Bình luận này đã được trả lời"
            key = (request.target.viewer_key, normalize(request.text))
            if key[0] and key in self._texts:
                return DUPLICATE, "Đã gửi câu này cho người xem này trong 10 phút qua"
            if self._sent and now - self._sent[-1] < request.min_interval_seconds:
                return RATE_LIMITED, f"Chưa đủ {request.min_interval_seconds:g} giây từ tin nhắn trước"
            if sum(1 for t in self._sent if now - t < 60) >= request.max_per_minute:
                return RATE_LIMITED, f"Đã đủ {request.max_per_minute} tin nhắn trong 1 phút"
        return None

    def record(self, request: ChatRequest) -> None:
        now = self._clock()
        with self._lock:
            if request.target.msg_id:
                self._msg_ids[request.target.msg_id] = now
            if request.target.viewer_key:
                self._texts[(request.target.viewer_key, normalize(request.text))] = now
            self._sent.append(now)

    def _prune(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= 60:
            self._sent.popleft()
        for key in [k for k, t in self._texts.items() if now - t >= SAME_TEXT_WINDOW_SECONDS]:
            del self._texts[key]
        for key in [k for k, t in self._msg_ids.items() if now - t >= 86400]:
            del self._msg_ids[key]


# --- pure matching rules -------------------------------------------------------------------------
def mention_tokens(text: str) -> set[str]:
    return {m.casefold() for m in MENTION_TOKEN.findall(text or "")}


def author_matches(author: str, target: ChatTarget) -> bool:
    """The row's author is the viewer: nickname, username or @username (alone it never identifies the viewer)."""
    shown = normalize(author)
    if not shown:
        return False
    username = normalize(target.username)
    if username and (shown in (username, "@" + username) or username in mention_tokens(author)):
        return True
    return bool(target.nickname) and shown == normalize(target.nickname)


def pick_direct_row(rows: list[tuple[str, str]], target: ChatTarget) -> int | str:
    """Index of the ONE row (author, text) answering the target comment, else the reason it cannot be used."""
    if not target.comment:
        return "không có nội dung bình luận gốc"
    hits = [i for i, (author, text) in enumerate(rows)
            if normalize(text) == normalize(target.comment) and author_matches(author, target)]
    return hits[0] if len(hits) == 1 else f"{len(hits)} dòng khớp bình luận gốc"


def pick_mention(items: list[str], username: str) -> int | str:
    """Index of the suggestion whose @username equals the viewer's exactly, else the reason."""
    want = username.casefold()
    hits = [i for i, text in enumerate(items) if want in mention_tokens(text)]
    return hits[0] if len(hits) == 1 else f"{len(hits)} gợi ý @{username} khớp chính xác"


# --- the sender ----------------------------------------------------------------------------------
class _Run:
    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._start = clock()
        self.steps: list[dict] = []
        self.mode: str | None = None

    def step(self, name: str, action: str | None = None, detail: str | None = None) -> None:
        item = {"step": name, "ms": int((self._clock() - self._start) * 1000)}
        if action:
            item["action"] = action
        if detail:
            item["detail"] = detail[:300]
        self.steps.append(item)

    def result(self, status: str, error: str | None = None, action: str | None = None, *,
               sent: bool = False) -> dict:
        """``mode`` is null when nothing was sent."""
        self.step(status, action=action, detail=error)
        return {"status": status, "mode": self.mode if sent else None, "error": error, "steps": self.steps}


class _Stop(Exception):
    """Fail closed with this message."""


class ChatSender:
    WAIT_MS = {"mention": 2500, "clear": 1200, "confirm": 4000, "poll": 100}
    MAX_ROWS = 60

    def __init__(self, selectors_path: Path | None = None, *, outbox: ChatOutbox | None = None,
                 clock: Callable[[], float] = time.monotonic, wait_ms: dict | None = None):
        self.outbox = outbox or ChatOutbox(clock)
        self._clock = clock
        self._path = Path(selectors_path) if selectors_path else None
        self._loaded: tuple[float | None, dict] = (None, load_selectors(None))
        self.wait_ms = {**self.WAIT_MS, **(wait_ms or {})}

    @property
    def selectors(self) -> dict[str, list[str]]:
        """Re-read the override file when it changed, so a fixed selector applies without a restart."""
        try:
            mtime = self._path.stat().st_mtime if self._path and self._path.is_file() else None
        except OSError:
            mtime = None
        if mtime != self._loaded[0]:
            self._loaded = (mtime, load_selectors(self._path if mtime is not None else None))
        return self._loaded[1]

    # --- locator helpers ---------------------------------------------------------------------
    def _find(self, root, group: str):
        """(visible locator, selector) of the first candidate that finds something, else (None, None)."""
        for sel in self.selectors[group]:
            try:
                loc = root.locator(sel).filter(visible=True)
                if loc.count():
                    return loc, sel
            except Exception as exc:  # noqa: BLE001 - a bad candidate from the override file
                log.debug("Selector %s (%s) lỗi: %s", group, sel, exc)
        return None, None

    def _flagged(self, page, group: str, exclude: list) -> str | None:
        """Text of a visible ``group`` element outside the chat list / composer / suggestions, if any."""
        for sel in self.selectors[group]:
            try:
                loc = page.locator(sel).filter(visible=True)
                for i in range(min(loc.count(), 10)):
                    el = loc.nth(i)
                    if any(ex is not None and el.and_(ex.locator("xpath=descendant-or-self::*")).count()
                           for ex in exclude):
                        continue  # a viewer comment or our own draft, not a TikTok notice
                    return f"{sel}: {el.inner_text(timeout=1000).strip()[:200]}"
            except Exception as exc:  # noqa: BLE001
                log.debug("Selector %s (%s) lỗi: %s", group, sel, exc)
        return None

    @staticmethod
    def _value(loc) -> str:
        try:
            return loc.input_value(timeout=1000)
        except Exception:  # noqa: BLE001 - contenteditable composer
            return loc.inner_text(timeout=1000)

    def _wait(self, page, ms: int, predicate: Callable[[], bool]) -> bool:
        deadline = self._clock() + ms / 1000
        while True:
            if predicate():
                return True
            if self._clock() >= deadline:
                return False
            page.wait_for_timeout(self.wait_ms["poll"])

    def _first_text(self, row, group: str) -> str:
        loc, _ = self._find(row, group)
        return loc.first.inner_text(timeout=1000) if loc is not None else ""

    def _rows(self, chat_list):
        if chat_list is None:
            return None
        rows, _ = self._find(chat_list, "message_row")
        return rows

    def _composer(self, page):
        loc, sel = self._find(page, "composer")
        if loc is None:
            raise _Stop("Không tìm thấy ô bình luận trên LIVE Console")
        count = loc.count()
        if count != 1:
            raise _Stop(f"Tìm thấy {count} ô bình luận ({sel}); không gửi khi không chắc ô nào")
        return loc, sel

    def _clear(self, composer) -> None:
        composer.fill("", timeout=2000)
        if normalize(self._value(composer)):
            raise _Stop("Không xoá được bản nháp trong ô bình luận")

    # --- send ----------------------------------------------------------------------------------
    def send(self, page, request: ChatRequest, account_user_id: str | None, *,
             account_names: tuple[str, ...] = ()) -> dict:
        run = _Run(self._clock)
        run.step("QUEUED")
        verdict = self.outbox.check(request)
        if verdict:
            return run.result(*verdict)
        typed = submitted = False
        composer = None
        try:
            if page is None or page.is_closed():
                raise _Stop(CONSOLE_CLOSED)
            if needs_login(page.url):
                raise _Stop(VERIFICATION_PAGE)
            if not account_user_id:
                raise _Stop(UNVERIFIED_ACCOUNT)
            if str(account_user_id) != str(request.expected_user_id):
                raise _Stop(ACCOUNT_MISMATCH)
            if self._flagged(page, "verification", []):
                raise _Stop(VERIFICATION_PAGE)
            composer, csel = self._composer(page)
            chat_list, _ = self._find(page, "chat_list")
            exclude = [chat_list, composer]
            disabled = self._flagged(page, "chat_disabled", exclude)
            if disabled or composer.is_disabled() or not composer.is_editable():
                raise _Stop("Bình luận LIVE đang bị tắt" + (f" ({disabled})" if disabled else ""))
            notice = self._flagged(page, "rate_notice", exclude)
            if notice:
                raise _Stop("TikTok đang báo giới hạn, không gửi: " + notice)
            if normalize(self._value(composer)):
                raise _Stop("Ô bình luận đang có nội dung chưa gửi (người vận hành đang gõ?); không ghi đè")
            rows = self._rows(chat_list)
            before = rows.filter(has_text=request.text.strip()).count() if rows is not None else 0
            run.step("CHECKED", detail=f"composer {csel}; {before} dòng trùng nội dung trước khi gửi")

            # Mode, in order: DIRECT_REPLY -> MENTION -> NORMAL.
            typed = True
            run.step("TYPING", action=f"composer: {csel}")
            if self._direct_reply(page, chat_list, request, run):
                composer, csel = self._composer(page)  # the Reply control may re-render the composer
                self._append(composer, request.text)
                run.mode = DIRECT_REPLY
            elif self._mention(page, composer, request, run):
                run.mode = MENTION
            else:
                self._type_plain(composer, request.text)
                run.mode = NORMAL
            if self.outbox.expired(request):
                self._clear(composer)
                typed = False
                return run.result(EXPIRED, f"Câu trả lời đã quá {request.ttl_seconds:g} giây, không gửi nữa")

            submitted = True
            self.outbox.record(request)
            action = self._submit(page, composer, chat_list, request.text, before)
            run.step("SUBMITTED", action=action)
            return self._confirm(page, composer, chat_list, request.text, before, account_names, run)
        except _Stop as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the real Playwright error goes to the log/result
            error = f"{type(exc).__name__}: {exc}"[:300]
        if typed and composer is not None:
            try:
                self._clear(composer)  # never leave our draft behind (nor resend it later)
            except Exception:  # noqa: BLE001
                pass
        return run.result(FAILED, error, sent=submitted)

    def _direct_reply(self, page, chat_list, request: ChatRequest, run: _Run) -> bool:
        target = request.target
        rows = self._rows(chat_list)
        if rows is None or not target.comment:
            run.step("DIRECT_REPLY_SKIPPED", detail="không có danh sách chat" if rows is None
                     else "không có bình luận gốc")
            return False
        candidates = rows.filter(has_text=target.comment.strip())
        found = [candidates.nth(i) for i in range(min(candidates.count(), self.MAX_ROWS))]
        choice = pick_direct_row([(self._first_text(r, "row_author"), self._first_text(r, "row_text"))
                                  for r in found], target)
        if isinstance(choice, str):
            run.step("DIRECT_REPLY_SKIPPED", detail=choice)
            return False
        row = found[choice]
        # The LIVE Console shows its [↩ | 📌 | …] popup on hover but renders it OUTSIDE the row: an ↩ icon may be
        # used page-wide only if none was visible before the hover and exactly one is visible after it.
        icons = page.locator(REPLY_ICON).filter(visible=True)
        try:
            self._composer(page)[0].hover(timeout=1500)  # the pointer may still rest on the last replied row
        except Exception:  # noqa: BLE001
            pass
        self._wait(page, 500, lambda: not icons.count())
        icons_before = icons.count()
        try:
            row.hover(timeout=1500)  # Reply controls often appear on hover
        except Exception:  # noqa: BLE001
            pass
        hit: list = [None, None]

        def shown() -> bool:
            hit[0], hit[1] = self._find(row, "reply_control")
            if hit[0] is None and icons_before == 0 and icons.count():
                hit[0], hit[1] = icons, f"{REPLY_ICON} (popup)"
            return hit[0] is not None
        self._wait(page, self.wait_ms["mention"], shown)
        reply, sel = hit
        if reply is None or reply.count() != 1:
            run.step("DIRECT_REPLY_SKIPPED", detail=f"dòng không có đúng một nút Trả lời "
                     f"({0 if reply is None else reply.count()} nút; {icons_before} nút ↩ hiện sẵn trước khi rê chuột)")
            return False
        try:
            reply.click(timeout=2000)
        except Exception as exc:  # noqa: BLE001 - e.g. the popup closed before the click: fall back to a mention
            run.step("DIRECT_REPLY_SKIPPED", detail=f"không bấm được nút Trả lời {sel}: {type(exc).__name__}")
            return False
        # The Console pre-fills "@username " after Reply: any pre-filled text must name exactly this viewer.
        composer, _ = self._composer(page)
        self._wait(page, self.wait_ms["mention"], lambda: bool(normalize(self._value(composer))))
        prefill = self._value(composer)
        if normalize(prefill) and mention_tokens(prefill) != {normalize(request.target.username)}:
            self._clear(composer)
            run.step("DIRECT_REPLY_SKIPPED", detail=f"ô bình luận sau Trả lời là {prefill.strip()[:60]!r}, "
                                                    f"không phải @{request.target.username}")
            return False
        run.step("REPLY_SELECTED", action=f"reply: {sel}", detail=prefill.strip()[:60] or None)
        return True

    def _mention(self, page, composer, request: ChatRequest, run: _Run) -> bool:
        username = request.target.username or ""
        if not USERNAME.fullmatch(username):
            run.step("MENTION_SKIPPED", detail="không có @username hợp lệ")
            return False
        composer.click(timeout=2000)
        self._clear(composer)
        composer.press_sequentially("@" + username, delay=20, timeout=5000)
        found: list = [None, None]

        def suggestions() -> bool:
            found[0], found[1] = self._find(page, "mention_list")
            return found[0] is not None
        reason = None
        if not self._wait(page, self.wait_ms["mention"], suggestions):
            reason = "không hiện danh sách gợi ý @"
        else:
            items, isel = self._find(found[0].first, "mention_item")
            texts = [items.nth(i).inner_text(timeout=1000) for i in range(min(items.count(), 20))] if items else []
            choice = pick_mention(texts, username)
            if isinstance(choice, str):
                reason = choice
            else:
                items.nth(choice).click(timeout=2000)
                run.step("MENTION_PICKED", action=f"suggestion: {found[1]} >> {isel}")
                self._append(composer, request.text)
                return True
        self._clear(composer)  # a typed "@name" without a picked suggestion is not a mention
        run.step("MENTION_SKIPPED", detail=reason + "; đã xoá bản nháp")
        return False

    def _append(self, composer, text: str) -> None:
        composer.click(timeout=2000)
        composer.press("End")
        current = self._value(composer)
        composer.press_sequentially(text if not current or current.endswith((" ", " ")) else " " + text,
                                    timeout=15000)
        if normalize(text) not in normalize(self._value(composer)):
            raise _Stop("Ô bình luận không nhận đủ nội dung đã gõ")

    def _type_plain(self, composer, text: str) -> None:
        composer.click(timeout=2000)
        composer.fill(text, timeout=5000)
        if normalize(self._value(composer)) != normalize(text):
            self._clear(composer)
            composer.press_sequentially(text, timeout=15000)
        if normalize(self._value(composer)) != normalize(text):
            raise _Stop("Ô bình luận không nhận đủ nội dung đã gõ")

    def _submit(self, page, composer, chat_list, text: str, before: int) -> str:
        composer.press("Enter")
        if self._wait(page, self.wait_ms["clear"], lambda: not normalize(self._value(composer))):
            return "press Enter"
        rows = self._rows(chat_list)
        if rows is not None and rows.filter(has_text=text.strip()).count() > before:
            return "press Enter (composer kept the text)"  # it was sent: clicking Send would post it twice
        self._stop_on_notice(page, chat_list, composer)  # refused: never try a second time
        button, sel = self._find(page, "send_button")
        if button is None or button.count() != 1 or not button.is_enabled():
            raise _Stop("Enter không gửi được và không tìm thấy đúng một nút Gửi đang bật")
        button.click(timeout=2000)
        return f"press Enter, then click send: {sel}"

    def _stop_on_notice(self, page, chat_list, composer) -> None:
        notice = self._flagged(page, "rate_notice", [chat_list, composer])
        if notice:
            raise _Stop("TikTok báo giới hạn sau khi gửi: " + notice)

    def _confirm(self, page, composer, chat_list, text: str, before: int, account_names, run: _Run) -> dict:
        names = {normalize(n) for n in account_names if n}
        names |= {"@" + n for n in names}
        outcome: list = [None]

        def seen() -> bool:
            self._stop_on_notice(page, chat_list, composer)
            rows = self._rows(chat_list)
            mine = rows.filter(has_text=text.strip()) if rows is not None else None
            count = mine.count() if mine is not None else 0
            if count <= before:
                return False
            row = mine.nth(count - 1)
            marker, _ = self._find(row, "own_marker")
            if marker is not None or normalize(self._first_text(row, "row_author")) in names - {""}:
                outcome[0] = (CONFIRMED, None, "own message seen in chat list")
                return True
            if not normalize(self._value(composer)):
                outcome[0] = (CONFIRMED, None, "composer cleared and message seen in chat list")
                return True
            return False

        if self._wait(page, self.wait_ms["confirm"], seen):
            status, error, action = outcome[0]
            return run.result(status, error, action=action, sent=True)
        return run.result(SUBMITTED, None, action="message not seen in chat list", sent=True)

    # --- probe ---------------------------------------------------------------------------------
    def probe(self, page) -> dict:
        """What every selector group finds on the page (total and visible), without typing anything."""
        if page is None or page.is_closed():
            return {"ok": False, "error": CONSOLE_CLOSED, "groups": {}}
        chat_list, _ = self._find(page, "chat_list")
        rows = self._rows(chat_list)
        mention_list, _ = self._find(page, "mention_list")
        scope = {"message_row": chat_list, "row_text": rows, "row_author": rows, "reply_control": rows,
                 "own_marker": rows, "mention_item": mention_list}
        groups = {}
        for group, candidates in self.selectors.items():
            root = scope.get(group, page)
            report = []
            for sel in candidates:
                entry = {"selector": sel, "count": 0, "visible": 0}
                if root is not None:
                    try:
                        entry["count"] = root.locator(sel).count()
                        entry["visible"] = root.locator(sel).filter(visible=True).count()
                    except Exception as exc:  # noqa: BLE001
                        entry["error"] = str(exc)[:200]
                report.append(entry)
            winner = next((e for e in report if e["visible"]), None)
            groups[group] = {"selector": winner["selector"] if winner else None,
                             "visible": winner["visible"] if winner else 0, "candidates": report}
        return {"ok": True, "url": page.url, "login_page": needs_login(page.url),
                "composers": groups["composer"]["visible"], "groups": groups}

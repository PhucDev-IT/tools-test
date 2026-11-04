"""Read-only TikTok LIVE Console observer PoC.

Only account_info/get and live_product/list have observed response contracts.
Pin and LIVE evidence must come from a separately verified parser. Neither
product rows nor OBS state prove the TikTok pin or LIVE state.
"""
from __future__ import annotations

import logging
import time
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal
from urllib.parse import urlsplit

log = logging.getLogger("sarae.tiktok_observer")
ACCOUNT = "/api/v1/streamer_desktop/account_info/get"
CART = "/api/v1/streamer_desktop/live_product/list"
HOSTS = frozenset({"shop.tiktok.com", "livecenter.tiktok.com", "seller-vn.tiktok.com",
                   "seller.tiktokglobalshop.com", "seller-us.tiktok.com"})
UNKNOWN = "UNKNOWN"


def identifier(value: object) -> str | None:
    s = str(value) if value is not None else ""
    return s if s.isdigit() and int(s) > 0 else None


@dataclass(frozen=True)
class PinEvidence:
    state: Literal["PINNED", "UNPINNED"]
    product_id: str
    seller_id: str | None
    source: str
    confirmed: bool
    command_id: str | None = None


@dataclass(frozen=True)
class LiveEvidence:
    state: Literal["LIVE", "OFFLINE", "ENDED"]
    source: str
    confirmed: bool


class TikTokLiveObserver:
    """State reducer and JSON event source. Browser use remains read only."""

    def __init__(self, emit: Callable[[dict], None], *, clock: Callable[[], float] = time.monotonic):
        self.emit, self.clock = emit, clock
        self.account_id: str | None = None
        self.cart: tuple[tuple[str, str | None], ...] | None = None
        self.pin: tuple[str, str | None] | None | str = UNKNOWN
        self.live_state = UNKNOWN
        self.connected = False
        self._account_verified = False
        self._pages: set[int] = set()
        self._intents: list[tuple[str, str, float, str]] = []

    def _event(self, kind: str, source: str, confidence: str, **fields: object) -> None:
        event = {"type": kind, "timestamp": datetime.now(timezone.utc).isoformat(),
                 "account_id": self.account_id, "source": source, "confidence": confidence, **fields}
        self.emit(event)
        log.info("TikTok observer event=%s account_id=%s source=%s", kind, self.account_id, source)

    def unknown(self, reason: str) -> None:
        changed = self.pin != UNKNOWN or self.live_state != UNKNOWN or self.cart is not None
        self.pin, self.live_state, self.cart = UNKNOWN, UNKNOWN, None
        self._account_verified = False
        self._intents.clear()
        if changed:
            self._event("OBSERVER_UNKNOWN", "console_lifecycle", "high",
                        reason=reason, pin_state=UNKNOWN, live_state=UNKNOWN)

    def disconnect(self) -> None:
        self.unknown("disconnected")
        self.connected, self.account_id = False, None

    def observe_account(self, body: object) -> None:
        if not isinstance(body, dict) or body.get("code") != 0:
            return
        data = body.get("data")
        uid = identifier(data.get("user_id")) if isinstance(data, dict) else None
        if uid is None:
            return
        previous = self.account_id
        if uid != previous:
            self.unknown("account_changed")
            self.account_id = uid
            self._event("ACCOUNT_CHANGED" if previous else "ACCOUNT_VERIFIED",
                        "account_info/get.data.user_id", "high", previous_account_id=previous)
        self.connected = True
        self._account_verified = True

    def observe_cart(self, body: object) -> None:
        if not self._account_verified or not isinstance(body, dict) or body.get("code") != 0:
            return
        data = body.get("data")
        rows = data.get("products") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return
        products = []
        for row in rows:
            if not isinstance(row, dict) or not (pid := identifier(row.get("product_id"))):
                return
            seller = row.get("seller_info")
            sid = identifier(seller.get("seller_id")) if isinstance(seller, dict) else None
            products.append((pid, sid))
        snapshot = tuple(sorted(set(products), key=lambda x: (x[0], x[1] or "")))
        if snapshot == self.cart:
            return
        previous, self.cart = self.cart, snapshot
        old, new = set(previous or ()), set(snapshot)
        rows_of = lambda values: [{"product_id": p, "seller_id": s} for p, s in values]
        self._event("CART_SNAPSHOT" if previous is None else "CART_CHANGED",
                    "live_product/list", "high", products=rows_of(snapshot),
                    added=rows_of(sorted(new - old, key=str)),
                    removed=rows_of(sorted(old - new, key=str)))

    def register_agent_pin(self, action: Literal["PINNED", "UNPINNED"], product_id: str,
                           *, ttl_seconds: float = 10) -> str:
        pid = identifier(product_id)
        if action not in ("PINNED", "UNPINNED") or not pid or ttl_seconds <= 0:
            raise ValueError("Invalid Agent pin intent")
        command_id = uuid4().hex
        self._intents.append((action, pid, self.clock() + ttl_seconds, command_id))
        return command_id

    def observe_pin(self, evidence: PinEvidence) -> None:
        # No production parser exists yet. A caller must verify explicit Console state.
        if not self._account_verified or not evidence.confirmed or evidence.source not in (
                "network_verified", "dom_verified"):
            return
        pid = identifier(evidence.product_id)
        if not pid or evidence.state not in ("PINNED", "UNPINNED"):
            return
        sid = identifier(evidence.seller_id)
        if evidence.state == "PINNED":
            matches = [item for item in self.cart or () if item[0] == pid]
            if sid is None and len(matches) == 1:
                sid = matches[0][1]
            if sid is None and len(matches) > 1:
                return
            next_pin: tuple[str, str | None] | None = (pid, sid)
        else:
            if not isinstance(self.pin, tuple) or self.pin[0] != pid:
                return
            sid = sid or self.pin[1]
            next_pin = None
        if self.pin == next_pin:
            return
        self._intents = [i for i in self._intents if i[2] >= self.clock()]
        pending = [i for i in self._intents if i[0] == evidence.state and i[1] == pid]
        match = next((i for i in pending if i[3] == evidence.command_id), None) if evidence.command_id else None
        if match:
            self._intents.remove(match)
        self.pin = next_pin
        self._event("PIN_CHANGED", evidence.source, "high", action=evidence.state,
                    product_id=pid, seller_id=sid,
                    actor="AGENT" if match else "UNKNOWN" if pending else "EXTERNAL")

    def observe_live(self, evidence: LiveEvidence) -> None:
        if not self._account_verified or not evidence.confirmed or evidence.source != "network_verified":
            return
        if evidence.state not in ("LIVE", "OFFLINE", "ENDED") or evidence.state == self.live_state:
            return
        previous, self.live_state = self.live_state, evidence.state
        self._event("LIVE_STATE_CHANGED", evidence.source, "high",
                    previous_state=previous, live_state=evidence.state)
        if previous == "LIVE" and evidence.state == "ENDED":
            self._event("TIKTOK_LIVE_ENDED", evidence.source, "high", live_state="ENDED")

    def observe_response(self, response: object) -> None:
        parts = urlsplit(getattr(response, "url", ""))
        if parts.scheme != "https" or parts.hostname not in HOSTS or getattr(response, "status", None) != 200:
            return
        if parts.path not in (ACCOUNT, CART):
            return
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            log.warning("TikTok observer could not decode %s", parts.path)
            return
        if parts.path == ACCOUNT:
            self.observe_account(body)
        else:
            self.observe_cart(body)

    def attach_page(self, page: object) -> None:
        if id(page) in self._pages:
            return
        self._pages.add(id(page))
        page.on("response", self.observe_response)
        page.on("framenavigated", lambda frame: self.unknown("navigation") if frame == page.main_frame else None)
        page.on("close", lambda: self.disconnect())

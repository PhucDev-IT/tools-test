"""Contract tests. Pin and LIVE evidence below is synthetic, not a captured TikTok API."""
import json
from pathlib import Path

from sarae_agent.tiktok_observer import LiveEvidence, PinEvidence, TikTokLiveObserver, UNKNOWN

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"tiktok_observer_{name}.json").read_text())


class Response:
    def __init__(self, path, body, *, host="shop.tiktok.com", status=200):
        self.url = f"https://{host}{path}?msToken=dynamic&X-Bogus=dynamic"
        self.status, self.body = status, body

    def json(self):
        return self.body


class Page:
    def __init__(self):
        self.handlers = {}
        self.main_frame = object()

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)

    def fire(self, name, *args):
        for callback in self.handlers.get(name, []):
            callback(*args)


def feed(observer, data):
    observer.observe_response(Response("/api/v1/streamer_desktop/account_info/get", data["account"]))
    observer.observe_response(Response("/api/v1/streamer_desktop/live_product/list", data["cart"]))


def test_seller_and_affiliate_account_identity_and_cart_changes():
    events = []
    observer = TikTokLiveObserver(events.append)
    seller, affiliate = fixture("seller"), fixture("affiliate")
    feed(observer, seller)
    assert observer.account_id == seller["account"]["data"]["user_id"]
    assert events[-1]["type"] == "CART_SNAPSHOT"
    assert events[-1]["products"][0] == {"product_id": "101", "seller_id": "900"}
    feed(observer, seller)
    assert len(events) == 2  # duplicate response is not an event
    changed = json.loads(json.dumps(seller["cart"]))
    changed["data"]["products"].pop()
    observer.observe_cart(changed)
    assert events[-1]["type"] == "CART_CHANGED"
    assert events[-1]["removed"] == [{"product_id": "102", "seller_id": "900"}]
    feed(observer, affiliate)
    assert observer.account_id == affiliate["account"]["data"]["user_id"]
    assert events[-2]["type"] == "ACCOUNT_CHANGED"
    assert {p["seller_id"] for p in events[-1]["products"]} == {"901", "902"}
    assert all(e["account_id"] not in {"900", "901", "902"} for e in events)
    assert all("timestamp" in e and "confidence" in e and "source" in e for e in events)


def test_manual_pin_unpin_agent_correlation_and_ambiguity():
    events = []
    observer = TikTokLiveObserver(events.append, clock=lambda: 100)
    feed(observer, fixture("affiliate"))
    observer.observe_pin(PinEvidence("PINNED", "201", None, "network_verified", True))
    assert events[-1]["actor"] == "EXTERNAL"
    assert events[-1]["seller_id"] == "901"
    observer.observe_pin(PinEvidence("PINNED", "201", None, "network_verified", True))
    assert len([e for e in events if e["type"] == "PIN_CHANGED"]) == 1
    observer.observe_pin(PinEvidence("UNPINNED", "201", None, "network_verified", True))
    assert events[-1]["action"] == "UNPINNED"
    command_id = observer.register_agent_pin("PINNED", "202")
    observer.observe_pin(PinEvidence("PINNED", "202", "902", "network_verified", True, command_id))
    assert events[-1]["actor"] == "AGENT"
    observer.observe_pin(PinEvidence("UNPINNED", "202", "902", "network_verified", True))
    observer.register_agent_pin("PINNED", "202")
    observer.observe_pin(PinEvidence("PINNED", "202", "902", "network_verified", True))
    assert events[-1]["actor"] == "UNKNOWN"  # temporal overlap cannot prove actor


def test_unknown_reload_disconnect_and_account_epoch():
    events = []
    observer = TikTokLiveObserver(events.append)
    feed(observer, fixture("seller"))
    observer.observe_pin(PinEvidence("PINNED", "101", "900", "network_verified", True))
    observer.unknown("navigation")
    assert observer.pin == observer.live_state == UNKNOWN
    assert observer.cart is None
    count = len(events)
    observer.observe_cart(fixture("seller")["cart"])
    assert len(events) == count  # no cart under a stale account identity
    observer.observe_account(fixture("seller")["account"])
    observer.observe_cart(fixture("seller")["cart"])
    observer.disconnect()
    assert observer.account_id is None and observer.live_state == UNKNOWN
    assert events[-1]["reason"] == "disconnected"


def test_obs_is_independent_and_end_needs_explicit_live_evidence():
    events = []
    observer = TikTokLiveObserver(events.append)
    feed(observer, fixture("seller"))
    obs_state = "STOPPED"
    assert observer.live_state == UNKNOWN and obs_state == "STOPPED"
    observer.observe_live(LiveEvidence("LIVE", "network_verified", False))
    assert observer.live_state == UNKNOWN
    observer.observe_live(LiveEvidence("LIVE", "network_verified", True))
    assert observer.live_state == "LIVE"
    observer.unknown("navigation")
    assert not any(e["type"] == "TIKTOK_LIVE_ENDED" for e in events)
    observer.observe_account(fixture("seller")["account"])
    observer.observe_live(LiveEvidence("LIVE", "network_verified", True))
    observer.observe_live(LiveEvidence("ENDED", "network_verified", True))
    assert events[-1]["type"] == "TIKTOK_LIVE_ENDED"


def test_untrusted_responses_and_incomplete_cart_do_not_emit():
    events = []
    observer = TikTokLiveObserver(events.append)
    data = fixture("seller")
    observer.observe_response(Response("/api/v1/streamer_desktop/account_info/get", data["account"], host="evil.example"))
    observer.observe_response(Response("/api/v1/streamer_desktop/account_info/get", data["account"], status=403))
    assert not events
    observer.observe_account(data["account"])
    observer.observe_cart({"code": 0, "data": {"products": [{"title": "missing id"}]}})
    observer.observe_pin(PinEvidence("PINNED", "101", "900", "dom_guess", True))
    assert [e["type"] for e in events] == ["ACCOUNT_VERIFIED"]


def test_page_listeners_are_passive_and_reload_invalidates_state():
    events = []
    observer = TikTokLiveObserver(events.append)
    page = Page()
    observer.attach_page(page)
    observer.attach_page(page)
    assert len(page.handlers["response"]) == 1
    data = fixture("seller")
    page.fire("response", Response("/api/v1/streamer_desktop/account_info/get", data["account"]))
    page.fire("response", Response("/api/v1/streamer_desktop/live_product/list", data["cart"]))
    page.fire("framenavigated", object())
    assert observer.cart is not None  # subframe navigation is irrelevant
    page.fire("framenavigated", page.main_frame)
    assert observer.cart is None and observer.pin == UNKNOWN
    page.fire("close")
    assert observer.account_id is None and not observer.connected

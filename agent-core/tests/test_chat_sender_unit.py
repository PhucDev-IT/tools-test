"""TikTok LIVE chat sender without a browser: outbox (duplicates, pacing, TTL), matching rules, selector config,
fail-closed checks, and the AutoPinService entry points (docs/AI_AUTO_REPLY.md)."""
import json
import tempfile
from pathlib import Path

import pytest

from sarae_agent.auto_pin import AutoPinService
from sarae_agent.tiktok_chat_sender import (
    DEFAULT_SELECTORS, UNVERIFIED_ACCOUNT, ChatOutbox, ChatRequest, ChatSender, ChatTarget, author_matches,
    load_selectors, pick_direct_row, pick_mention)

ACCOUNT = "6970000000000000001"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def req(msg_id="m1", user_id="6812", username="lan.nguyen", text="Size M nha", ttl=45, received_at=None,
        interval=8, per_minute=5, **target):
    return ChatRequest(reply_id=1, session_id=3, expected_user_id=ACCOUNT, ttl_seconds=ttl, text=text,
                       target=ChatTarget(msg_id=msg_id, user_id=user_id, username=username, **target),
                       min_interval_seconds=interval, max_per_minute=per_minute, received_at=received_at)


# --- outbox ----------------------------------------------------------------------------------------
def test_same_source_comment_is_answered_once():
    clock = Clock()
    box = ChatOutbox(clock)
    assert box.check(req()) is None
    box.record(req())
    clock.t += 3600
    assert box.check(req(text="another answer"))[0] == "DUPLICATE"


def test_same_text_to_same_viewer_once_per_ten_minutes():
    clock = Clock()
    box = ChatOutbox(clock)
    box.record(req(msg_id="m1", text="Size  M nha"))
    clock.t += 30
    assert box.check(req(msg_id="m2", text="size m NHA"))[0] == "DUPLICATE"  # normalized text, same user_id
    assert box.check(req(msg_id="m3", user_id=None, username="hoa", text="Size M nha")) is None  # other viewer
    clock.t += 600
    assert box.check(req(msg_id="m4", text="Size M nha")) is None


def test_viewer_key_falls_back_to_username():
    clock = Clock()
    box = ChatOutbox(clock)
    box.record(req(msg_id="a", user_id=None, username="Lan.Nguyen"))
    clock.t += 60
    assert box.check(req(msg_id="b", user_id=None, username="lan.nguyen"))[0] == "DUPLICATE"


def test_min_interval_and_max_per_minute():
    clock = Clock()
    box = ChatOutbox(clock)
    box.record(req(msg_id="1", text="a"))
    clock.t += 5
    status, message = box.check(req(msg_id="2", text="b"))
    assert status == "RATE_LIMITED" and "8" in message
    clock.t += 3
    assert box.check(req(msg_id="2", text="b")) is None
    for i in range(4):  # 5 messages in the same minute
        box.record(req(msg_id=f"x{i}", text=f"t{i}"))
        clock.t += 8
    status, message = box.check(req(msg_id="y", text="y"))
    assert status == "RATE_LIMITED" and "1 phút" in message
    assert box.check(req(msg_id="y", text="y", per_minute=10)) is None  # pacing comes with the request
    clock.t += 60
    assert box.check(req(msg_id="y", text="y")) is None


def test_ttl_from_receipt():
    clock = Clock()
    box = ChatOutbox(clock)
    fresh = req(received_at=clock(), ttl=45)
    clock.t += 45
    assert box.check(fresh) is None
    clock.t += 0.5
    assert box.check(fresh)[0] == "EXPIRED"
    assert box.check(req(received_at=None)) is None  # no receipt time: TTL not applicable


# --- matching rules --------------------------------------------------------------------------------
def test_direct_reply_needs_exactly_one_row_with_text_and_author():
    target = ChatTarget(username="lan.nguyen", nickname="Lan", comment="55kg mặc size gì")
    assert pick_direct_row([("Hoa", "55kg mặc size gì"), ("Lan", " 55kg  mặc size gì ")], target) == 1
    assert pick_direct_row([("@lan.nguyen", "55kg mặc size gì")], target) == 0
    assert isinstance(pick_direct_row([("Lan", "55kg mặc size gì")] * 2, target), str)  # several -> next mode
    assert isinstance(pick_direct_row([("Lan", "khác")], target), str)
    assert isinstance(pick_direct_row([("Lan", "x")], ChatTarget(nickname="Lan")), str)  # no comment


def test_author_match_rules():
    target = ChatTarget(username="lan.nguyen", nickname="Lan")
    assert author_matches("Lan", target) and author_matches("@lan.nguyen", target)
    assert author_matches("Lan Shop @lan.nguyen", target)
    assert not author_matches("Lan Anh", target) and not author_matches("", target)


def test_mention_pick_is_exact_username():
    items = ["Lan @lan.nguyen", "Lan 2 @lan.nguyen2", "Hoa @hoa.tran"]
    assert pick_mention(items, "lan.nguyen") == 0
    assert pick_mention(items, "LAN.NGUYEN2") == 1
    assert isinstance(pick_mention(items, "lan.ng"), str)
    assert isinstance(pick_mention(["Lan"], "lan"), str)  # display name alone is not a match


# --- selectors -------------------------------------------------------------------------------------
def test_selectors_override_merges_over_defaults(tmp_path):
    path = tmp_path / "tiktok_chat_selectors.json"
    path.write_text(json.dumps({"composer": ["#my-box"]}), encoding="utf-8")
    merged = load_selectors(path)
    assert merged["composer"] == ["#my-box"] and merged["send_button"] == DEFAULT_SELECTORS["send_button"]
    sender = ChatSender(path)
    assert sender.selectors["composer"] == ["#my-box"]


@pytest.mark.parametrize("content", ["not json", "[]", '{"composer": []}', '{"composer": "x"}', '{"nope": ["x"]}'])
def test_invalid_selector_file_uses_defaults(tmp_path, content):
    path = tmp_path / "tiktok_chat_selectors.json"
    path.write_text(content, encoding="utf-8")
    assert load_selectors(path) == DEFAULT_SELECTORS


def test_defaults_prefer_roles_and_labels():
    for group in ("composer", "send_button", "reply_control"):
        assert DEFAULT_SELECTORS[group][0].startswith("role=")
    assert any("Trả lời" in s for s in DEFAULT_SELECTORS["reply_control"])
    assert any("gửi" in s for s in DEFAULT_SELECTORS["send_button"])


# --- fail closed before any typing -----------------------------------------------------------------
class UntouchablePage:
    """A page whose content must not be read: the request is refused before."""

    def __init__(self, url="https://shop.tiktok.com/streamer/live/product/dashboard", closed=False):
        self.url, self.closed = url, closed

    def is_closed(self):
        return self.closed

    def locator(self, *_args, **_kwargs):
        raise AssertionError("page touched")


@pytest.mark.parametrize("page,account,error", [
    (None, ACCOUNT, "LIVE Console chưa mở"),
    (UntouchablePage(closed=True), ACCOUNT, "LIVE Console chưa mở"),
    (UntouchablePage("https://www.tiktok.com/login"), ACCOUNT, "đăng nhập / xác minh"),
    (UntouchablePage(), None, UNVERIFIED_ACCOUNT),
    (UntouchablePage(), "42", "tài khoản khác"),
])
def test_refused_before_reading_the_page(page, account, error):
    clock = Clock()
    sender = ChatSender(outbox=ChatOutbox(clock), clock=clock)
    result = sender.send(page, req(), account)
    assert result["status"] == "FAILED" and result["mode"] is None and error in result["error"]
    assert [s["step"] for s in result["steps"]] == ["QUEUED", "FAILED"]
    assert sender.outbox.check(req()) is None  # nothing recorded: a later attempt is still allowed


def test_outbox_verdicts_come_first():
    clock = Clock()
    sender = ChatSender(outbox=ChatOutbox(clock), clock=clock)
    sender.outbox.record(req())
    result = sender.send(UntouchablePage(), req(), ACCOUNT)
    assert result == {"status": "DUPLICATE", "mode": None, "error": result["error"],
                      "steps": [{"step": "QUEUED", "ms": 0}, {"step": "DUPLICATE", "ms": 0, "detail": result["error"]}]}
    expired = sender.send(UntouchablePage(), req(msg_id="z", text="z", received_at=clock() - 100), ACCOUNT)
    assert expired["status"] == "EXPIRED"


def test_request_from_contract_payload():
    payload = {"reply_id": 12, "session_id": 3, "expected_user_id": "6970", "ttl_seconds": 45,
               "text": "55kg lấy size M vừa đẹp nha",
               "target": {"msg_id": "7301", "user_id": "6812", "username": "lan.nguyen", "nickname": "Lan",
                          "comment": "55kg mặc size gì"}}
    r = ChatRequest.from_payload(payload, received_at=5.0)
    assert (r.reply_id, r.target.username, r.min_interval_seconds, r.max_per_minute, r.received_at) == \
        (12, "lan.nguyen", 8.0, 5, 5.0)


# --- AutoPinService entry points ---------------------------------------------------------------------
class FakeResponse:
    def __init__(self, user_id):
        self.url = "https://shop.tiktok.com/api/v1/streamer_desktop/account_info/get"
        self.status = 200
        self._body = {"code": 0, "data": {"user_id": user_id, "user_name": "sarae.shop", "nick_name": "Sarae"}}

    def json(self):
        return self._body


@pytest.fixture()
def pin():
    with tempfile.TemporaryDirectory() as tmp:
        service = AutoPinService(Path(tmp) / "profile")
        yield service
        service.close()


def contract_payload(**extra):
    return {"reply_id": 12, "session_id": 3, "expected_user_id": ACCOUNT, "ttl_seconds": 45, "text": "Size M nha",
            "target": {"msg_id": "7301", "username": "lan.nguyen", "comment": "55kg mặc size gì"}, **extra}


def test_chat_send_without_console_fails_and_never_opens_chrome(pin):
    result = pin.chat_send(contract_payload())
    assert result["status"] == "FAILED" and result["error"] == "LIVE Console chưa mở" and result["mode"] is None
    assert pin._context is None and pin._playwright is None
    probe = pin.chat_probe()
    assert probe["ok"] is False and probe["account"]["verified"] is False


def test_account_is_captured_outside_a_sync(pin):
    assert not pin._sync_capture_active
    pin._capture_account_response(FakeResponse(ACCOUNT))
    assert pin._account_profile["user_id"] == ACCOUNT and pin._account_profile["nick_name"] == "Sarae"


def test_chat_send_passes_the_verified_account_to_the_sender(pin):
    calls = []

    class Page:
        url = "https://shop.tiktok.com/streamer/live/product/dashboard"

        def is_closed(self):
            return False

        def on(self, *_):
            pass

    class Sender:
        outbox = ChatOutbox()

        def send(self, page, request, account_user_id, account_names=()):
            calls.append((request.reply_id, account_user_id, account_names))
            return {"status": "CONFIRMED", "mode": "NORMAL", "error": None, "steps": []}

    pin._chat = Sender()
    pin._context, pin._page = object(), Page()
    pin._capture_account_response(FakeResponse(ACCOUNT))
    try:
        assert pin.chat_send(contract_payload())["status"] == "CONFIRMED"
    finally:
        pin._context = pin._page = None
    assert calls == [(12, ACCOUNT, ("Sarae", "sarae.shop"))]

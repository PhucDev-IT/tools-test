"""ChatSender against local HTML imitations of the LIVE Console chat (tests/fixtures/tiktok_chat), real Chrome."""
from pathlib import Path

import pytest

from sarae_agent.auto_pin import find_chrome
from sarae_agent.tiktok_chat_sender import ChatOutbox, ChatRequest, ChatSender, ChatTarget

FIXTURES = Path(__file__).parent / "fixtures" / "tiktok_chat"
ACCOUNT = "6970000000000000001"
TEXT = "55kg lấy size M vừa đẹp nha"


@pytest.fixture(scope="module")
def browser():
    try:
        chrome = find_chrome()
    except RuntimeError:
        pytest.skip("Google Chrome is not installed")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=chrome)
        yield b
        b.close()


@pytest.fixture()
def open_console(browser):
    pages = []

    def go(variant="console"):
        page = browser.new_page()
        page.goto((FIXTURES / f"{variant}.html").as_uri())
        pages.append(page)
        return page
    yield go
    for page in pages:
        page.close()


def request(username="lan.nguyen", msg_id="m1", text=TEXT, **target):
    fields = {"msg_id": msg_id, "user_id": "6812", "username": username, "nickname": "Lan",
              "comment": "55kg mặc size gì", **target}
    return ChatRequest(reply_id=1, session_id=3, expected_user_id=ACCOUNT, ttl_seconds=45, text=text,
                       target=ChatTarget(**fields), min_interval_seconds=0, max_per_minute=20, received_at=None)


def sender():
    return ChatSender(outbox=ChatOutbox(), wait_ms={"mention": 1200, "clear": 600, "confirm": 1500})


def sent(page):
    return page.evaluate("window.SENT")


def actions(result):
    return [s.get("action") for s in result["steps"] if s.get("action")]


def test_direct_reply_when_exactly_one_matching_row(open_console):
    page = open_console()
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "DIRECT_REPLY", result
    assert sent(page) == [{"text": TEXT, "replyTo": "Lan", "mention": None}]
    assert [s["step"] for s in result["steps"]][-2:] == ["SUBMITTED", "CONFIRMED"]
    assert any(a.startswith("reply: ") for a in actions(result)) and "press Enter" in actions(result)
    assert all(isinstance(s["ms"], int) for s in result["steps"])


def test_duplicate_rows_fall_back_to_mention(open_console):
    page = open_console("duplicate_rows")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "MENTION", result
    assert sent(page) == [{"text": "@lan.nguyen " + TEXT, "replyTo": None, "mention": "lan.nguyen"}]
    skipped = next(s for s in result["steps"] if s["step"] == "DIRECT_REPLY_SKIPPED")
    assert "2 dòng" in skipped["detail"]


def test_no_reply_button_uses_mention(open_console):
    page = open_console("no_reply")
    result = sender().send(page, request(), ACCOUNT)
    assert result["mode"] == "MENTION" and sent(page)[0]["mention"] == "lan.nguyen"


def test_no_exact_suggestion_clears_draft_and_sends_normal(open_console):
    page = open_console("no_reply")
    # "lan.ng" lists lan.nguyen and lan.nguyen2, neither is exactly the viewer.
    result = sender().send(page, request(username="lan.ng"), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "NORMAL", result
    assert sent(page) == [{"text": TEXT, "replyTo": None, "mention": None}]  # no "@lan.ng" left in the message


def test_no_mention_popup_sends_normal(open_console):
    page = open_console("no_mention")
    result = sender().send(page, request(comment=None), ACCOUNT)
    assert result["mode"] == "NORMAL" and sent(page)[0]["text"] == TEXT


def test_two_composers_fail_without_typing(open_console):
    page = open_console("two_composers")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "FAILED" and result["mode"] is None and "2 ô bình luận" in result["error"]
    assert sent(page) == [] and page.locator("textarea").first.input_value() == ""


def test_chat_disabled_fails(open_console):
    page = open_console("disabled")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "FAILED" and "bị tắt" in result["error"] and sent(page) == []


def test_slow_down_notice_stops_the_sender(open_console):
    page = open_console("notice")
    s = sender()
    result = s.send(page, request(), ACCOUNT)
    assert result["status"] == "FAILED" and "quá nhanh" in result["error"], result
    assert page.evaluate("window.SUBMIT_COUNT") == 1  # no Send click after the refusal, no retry loop
    assert page.locator("textarea").input_value() == ""  # our draft is not left behind
    again = s.send(page, request(msg_id="m2", text="khác"), ACCOUNT)
    assert again["status"] == "FAILED" and "giới hạn" in again["error"]  # notice still shown: nothing typed
    assert page.evaluate("window.SUBMIT_COUNT") == 1


def test_viewer_comments_never_count_as_notices(open_console):
    # The chat list holds "quá nhanh" and "bình luận đã bị tắt à?" as viewer comments.
    page = open_console()
    assert sender().send(page, request(), ACCOUNT)["status"] == "CONFIRMED"


def test_operator_draft_is_never_overwritten(open_console):
    page = open_console()
    page.locator("textarea").fill("đang gõ dở")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "FAILED" and page.locator("textarea").input_value() == "đang gõ dở"
    assert sent(page) == []


def test_submitted_when_message_never_appears(open_console):
    page = open_console("no_echo")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "SUBMITTED" and result["mode"] == "DIRECT_REPLY" and result["error"] is None
    assert len(sent(page)) == 1


def test_send_button_when_enter_does_nothing(open_console):
    page = open_console("enter_noop")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED", result
    assert any(a.startswith("press Enter, then click send: ") for a in actions(result))
    assert len(sent(page)) == 1


def test_composer_keeping_text_is_not_sent_twice(open_console):
    page = open_console("keep_text")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and len(sent(page)) == 1, result
    assert "press Enter (composer kept the text)" in actions(result)


@pytest.mark.parametrize("account", [None, "1234"])
def test_unknown_or_other_account_fails_without_typing(open_console, account):
    page = open_console()
    result = sender().send(page, request(), account)
    assert result["status"] == "FAILED" and result["mode"] is None
    assert sent(page) == [] and page.locator("textarea").input_value() == ""
    assert not any(s["step"] == "TYPING" for s in result["steps"])


def test_probe_reports_counts_without_typing(open_console):
    page = open_console()
    report = sender().probe(page)
    groups = report["groups"]
    assert report["ok"] and report["composers"] == 1
    assert groups["chat_list"]["visible"] == 1 and groups["message_row"]["visible"] == 3
    assert groups["reply_control"]["visible"] == 3 and groups["mention_list"]["visible"] == 0
    assert groups["send_button"]["selector"].startswith("role=button")
    assert sent(page) == [] and page.locator("textarea").input_value() == ""


def test_seller_console_panel_is_found_and_other_textareas_ignored(open_console):
    page = open_console("seller_console")
    report = sender().probe(page)
    groups = report["groups"]
    assert report["composers"] == 1 and groups["composer"]["selector"].startswith("xpath=//p")
    assert groups["chat_list"]["visible"] == 1 and groups["message_row"]["visible"] == 0
    assert groups["send_button"]["visible"] == 0  # "cursor-not-allowed" while the box is empty
    page.locator(".m4b-input-textarea-mini").fill("hi")
    assert sender().probe(page)["groups"]["send_button"]["visible"] == 1


def test_seller_console_sends_normal_message(open_console):
    page = open_console("seller_console")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "NORMAL", result
    assert sent(page) == [TEXT] and page.locator(".product-search textarea").input_value() == ""


def test_seller_console_reads_real_comment_rows(open_console):
    page = open_console("seller_console")
    page.goto((FIXTURES / "seller_console.html").as_uri() + "?rows=1")
    s = sender()
    report = s.probe(page)
    assert report["groups"]["message_row"]["visible"] == 2  # the "just joined" row is not a comment
    chat_list, _ = s._find(page, "chat_list")
    rows = s._rows(chat_list)
    assert [(s._first_text(rows.nth(i), "row_author"), s._first_text(rows.nth(i), "row_text"))
            for i in range(rows.count())] == [("Lan", "55kg mặc size gì"), ("Hoa", "ship bao lâu vậy shop")]
    result = s.send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "DIRECT_REPLY", result
    assert sent(page) == ["@lan.nguyen " + TEXT] and s._rows(chat_list).count() == 3
    # the popup is outside the row, as on the real Console
    assert "reply: button:has(> svg.arco-icon-recover) (popup)" in actions(result)
    assert page.evaluate("window.FORBIDDEN") == []  # pin / more never clicked


def test_seller_console_reply_icon_inside_the_row(open_console):
    page = open_console("seller_console")
    page.goto((FIXTURES / "seller_console.html").as_uri() + "?rows=1&popup=inside")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "DIRECT_REPLY", result
    assert "reply: button:has(> svg.arco-icon-recover)" in actions(result)
    assert sent(page) == ["@lan.nguyen " + TEXT] and page.evaluate("window.FORBIDDEN") == []


def test_seller_console_second_reply_after_pointer_rests_on_a_row(open_console):
    page = open_console("seller_console")
    page.goto((FIXTURES / "seller_console.html").as_uri() + "?rows=1")
    page.locator(".pl-32", has_text="ship bao lâu").hover()  # the pointer rests on another viewer's row
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "DIRECT_REPLY", result
    assert sent(page) == ["@lan.nguyen " + TEXT] and page.evaluate("window.FORBIDDEN") == []


def test_seller_console_popup_that_never_closes_is_not_trusted(open_console):
    page = open_console("seller_console")
    page.goto((FIXTURES / "seller_console.html").as_uri() + "?rows=1")
    page.evaluate("""() => {  // a ↩ that is always visible (not the hover popup) must not be clicked
        const b = document.createElement('div');
        b.innerHTML = '<button class="stuck"><svg class="arco-icon arco-icon-recover" width="16" height="16"></svg></button>';
        b.querySelector('button').onclick = () => window.FORBIDDEN.push('stuck');
        document.body.prepend(b);
    }""")
    result = sender().send(page, request(), ACCOUNT)
    assert result["mode"] != "DIRECT_REPLY", result
    assert any("hiện sẵn" in (s.get("detail") or "") for s in result["steps"] if s["step"] == "DIRECT_REPLY_SKIPPED")
    assert page.evaluate("window.FORBIDDEN") == []


def test_seller_console_reply_prefilled_for_someone_else_is_not_used(open_console):
    page = open_console("seller_console")
    page.goto((FIXTURES / "seller_console.html").as_uri() + "?rows=1&prefill=other")
    result = sender().send(page, request(), ACCOUNT)
    assert result["status"] == "CONFIRMED" and result["mode"] == "NORMAL", result
    skipped = [s["detail"] for s in result["steps"] if s["step"] == "DIRECT_REPLY_SKIPPED"]
    assert skipped and "someone.else" in skipped[0]
    assert sent(page) == [TEXT] and page.evaluate("window.FORBIDDEN") == []

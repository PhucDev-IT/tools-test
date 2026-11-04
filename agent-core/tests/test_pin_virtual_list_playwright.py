"""Pinning on the LIVE Console's virtual product list (only cards near the visible part exist), in real Chrome.

Seen on a real LIVE: the card to pin was not rendered, so it was "not found" while the operator could pin it by hand.
The DOM worker methods are called on the test thread (Playwright pages belong to the thread that made them).
"""
import tempfile
from pathlib import Path

import pytest

from sarae_agent import auto_pin
from sarae_agent.auto_pin import AutoPinService, find_chrome

FIXTURE = Path(__file__).parent / "fixtures" / "tiktok_pin" / "virtual_list.html"
CONSOLE = "https://shop.tiktok.com/streamer/live/product/dashboard"
# Single spaces where the card has two ("back to school  - ST"): spaces never decide a match.
FAR = {"name": "Áo sơ mi Phiên bản dài tay đai eo tiểu thư,sơ mi back to school - ST Design - SKU 10 somi hot sơ minu Nữ Women",
       "image_url": "https://img.example/p5.jpg", "price_text": "224000"}


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
def console(browser, monkeypatch):
    monkeypatch.setattr(auto_pin, "PIN_CONFIRM_MS", 4000)
    temp = tempfile.TemporaryDirectory()
    service = AutoPinService(Path(temp.name) / "profile")
    page = browser.new_page()
    page.route(CONSOLE + "*", lambda route: route.fulfill(content_type="text/html; charset=utf-8",
                                                          body=FIXTURE.read_text(encoding="utf-8")))

    def open_(hash_=""):
        page.goto(CONSOLE + hash_)
        service._context, service._page = object(), page
        return service, page
    yield open_
    service._context = None
    service.close()
    page.close()
    temp.cleanup()


def scroll_top(page) -> int:
    return page.evaluate("document.getElementById('list').scrollTop")


def test_card_out_of_view_is_found_by_scrolling_pinned_once_and_the_list_scrolls_back(console):
    service, page = console()
    page.evaluate("document.getElementById('list').scrollTop = 120")  # where the operator was looking
    rendered = [c["name"] for c in page.evaluate(auto_pin.PIN_CARD_JS, None)]
    assert FAR["name"] not in [" ".join(n.split()) for n in rendered], "the far card is not in the page"
    result = service._pin_on_worker(FAR, None)
    assert result["status"] == "SUCCESS" and result["card"]["index"] is not None
    assert page.evaluate("window.CLICKS") == [5], "one click, on the right card, after TikTok answered late"
    assert page.evaluate("window.DOUBLE_CLICKS") == 0
    assert scroll_top(page) == 120, "the list is back where the operator was looking"


def test_not_confirmed_in_time_is_reported_and_never_clicked_again(console, monkeypatch):
    monkeypatch.setattr(auto_pin, "PIN_CONFIRM_MS", 500)
    service, page = console()
    page.evaluate("window.PIN_DELAY_MS = 3000")
    with pytest.raises(RuntimeError, match="chưa xác nhận"):
        service._pin_on_worker(FAR, None)
    assert page.evaluate("window.CLICKS") == [5] and page.evaluate("window.DOUBLE_CLICKS") == 0


def test_product_not_in_the_list_says_card_not_found(console):
    service, page = console()
    with pytest.raises(RuntimeError, match="Không thấy product card"):
        service._pin_on_worker({"name": "Sản phẩm không có trong giỏ LIVE", "price_text": "100000"}, None)
    assert page.evaluate("window.CLICKS") == []
    assert scroll_top(page) == 0


def test_unpin_finds_the_pinned_card_out_of_view(console):
    service, page = console("#pinned-last")
    assert service._pin_state_on_worker()["pin_maybe_hidden"] is True, "nothing pinned in view, list scrolls"
    result = service._unpin_on_worker()
    assert result["status"] == "SUCCESS"
    assert page.evaluate("window.CLICKS") == [7] and scroll_top(page) == 0

"""LIVE_STATE_JS against elements copied from the real Seller LIVE Console, in real Chrome (read only, never clicks)."""
import pytest

from sarae_agent.auto_pin import LIVE_LABELS, LIVE_STATE_JS, find_chrome

# The broadcast control bar ("Video" / "Kịch bản" switches), copied from the Console (svg paths shortened).
# Before LIVE it holds the "Phát LIVE" button; while live the icon-only end control replaces it.
SWITCHES = ('<div class="flex flex-row items-center"><span class="text-body-s-medium">Video</span>'
            '<div class="m4b-switch-container" data-tid="m4b_switch"><button role="switch" aria-checked="true" '
            'class="arco-switch arco-switch-small m4b-switch ml-6" type="button"><div class="arco-switch-dot"></div>'
            '</button></div></div><div class="mx-8 flex items-center"><div role="separator" class="arco-divider"></div></div>'
            '<div><div class="flex flex-row gap-6 justify-end"><div class="text-body-s-medium">Kịch bản</div>'
            '<div class="m4b-switch-container" data-tid="m4b_switch"><button role="switch" aria-checked="false" '
            'class="arco-switch arco-switch-small m4b-switch" type="button"><div class="arco-switch-dot"></div></button>'
            '</div></div></div><div class="flex-1"></div>')
GO_LIVE = ('<div class="flex flex-row items-center"><div class="flex items-center gap-4"><button data-tid="m4b_button" '
           'class="arco-btn arco-btn-primary arco-btn-size-default arco-btn-shape-square m4b-button text-body-s-medium" '
           'type="button" style="border-radius: 4px; min-width: 75px; height: 24px;"><span>Phát LIVE</span></button>'
           '<div class="m4b-dropdown w-24 h-24" data-tid="m4b_dropdown"><div style="width: 24px; height: 24px;">'
           '<svg class="arco-icon arco-icon-down " width="16" height="16" viewBox="0 0 16 16"><path d="M3 5h10L8 11z">'
           '</path></svg></div></div></div></div>')
END_LIVE = ('<div class="text-neutral-white cursor-pointer ml-6 rounded-4 w-24 h-24 flex items-center justify-center">'
            '<svg class="arco-icon arco-icon-im_close_chat " width="1em" height="1em" viewBox="0 0 16 16" font-size="16">'
            '<path d="M7.6 1.3h.7v5h-.7z"></path></svg></div>')
# Always shown elsewhere on the Console, before and during LIVE: must never count.
GO_LIVE_NOW = ('<div class="side-panel"><button data-tid="m4b_button" class="arco-btn arco-btn-primary '
               'arco-btn-status-danger m4b-button w-full" type="button"><svg class="arco-icon arco-icon-live " '
               'width="1em" height="1em" viewBox="0 0 16 16"><path d="M1 1h14v14H1z"></path></svg>'
               '<span>Phát LIVE ngay</span></button></div>')


def bar(control: str) -> str:
    return f'<div class="flex flex-row items-center w-full">{SWITCHES}{control}</div>'


@pytest.fixture(scope="module")
def page():
    try:
        chrome = find_chrome()
    except RuntimeError:
        pytest.skip("Google Chrome is not installed")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=chrome)
        yield browser.new_page()
        browser.close()


@pytest.mark.parametrize("html, state", [
    (bar(GO_LIVE) + GO_LIVE_NOW, "OFFLINE"),
    (bar(END_LIVE) + GO_LIVE_NOW, "LIVE"),  # the real page while live (seen as UNKNOWN before this fix)
    (bar(END_LIVE), "LIVE"),
    (bar(GO_LIVE + f'<div style="display: none">{END_LIVE}</div>'), "OFFLINE"),
    (bar(GO_LIVE + END_LIVE), "UNKNOWN"),
    (bar(END_LIVE + END_LIVE), "UNKNOWN"),
    (GO_LIVE_NOW, "UNKNOWN"),  # "Phát LIVE ngay" alone says nothing
    (END_LIVE, "UNKNOWN"),  # an end icon outside the control bar does not count
    ("<p>Console is loading</p>", "UNKNOWN"),
])
def test_live_state_from_console_controls(page, html, state):
    page.set_content(f"<body onclick=\"window.CLICKED = true\">{html}</body>")
    result = page.evaluate(LIVE_STATE_JS, LIVE_LABELS)
    assert result["state"] == state, result
    assert page.evaluate("window.CLICKED") is None  # reading the state never clicks anything

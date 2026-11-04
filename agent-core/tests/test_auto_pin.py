"""Auto Pin: validation, the plain-Chrome login step and the hand-over to Playwright, without launching Chrome."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sarae_agent import auto_pin
from sarae_agent.auto_pin import AutoPinService, find_chrome, needs_login
from sarae_agent.cloud.commands import CommandRouter

CONSOLE = "https://shop.tiktok.com/streamer/live/product/dashboard"


class FakeProc:
    def __init__(self, args):
        self.args = args
        self.pid = 4242
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        if self.alive:
            raise auto_pin.subprocess.TimeoutExpired(self.args, timeout)
        return 0


class FakeResponse:
    def __init__(self, path, body, status=200):
        self.url, self.status, self._body = "https://shop.tiktok.com" + path, status, body

    def json(self):
        return self._body


def console_responses(user_id="7494647113328657914", products=(("9", "1", "Son môi Đỏ"), ("9", "2", "Đồng hồ S9"))):
    """What the LIVE Console's own XHRs return after a reload: the logged-in account and the LIVE cart."""
    return [FakeResponse("/api/v1/streamer_desktop/account_info/get",
                         {"code": 0, "data": {"user_id": user_id, "user_name": "sarae.fashion3", "user_type": 1}}),
            FakeResponse("/api/v1/streamer_desktop/live_product/list",
                         {"code": 0, "data": {"products": [{"product_id": pid, "title": name, "seller_info": {"seller_id": seller}}
                                                           for seller, pid, name in products]}})]


class FakePage:
    def __init__(self, url, rows=None, responses=()):
        self.url = url
        self.rows = rows or []
        self.scripts = []
        self.handlers = []
        self.responses = list(responses)

    def is_closed(self):
        return False

    def on(self, event, handler):
        self.handlers.append(handler)

    def reload(self, **_options):
        self.reloads = getattr(self, "reloads", 0) + 1
        self.waits = 0
        if not getattr(self, "late_after", None) and self.reloads >= getattr(self, "answer_on_reload", 1):
            self._deliver()

    def _deliver(self):
        for response in self.responses:
            for handler in self.handlers:
                handler(response)

    def wait_for_timeout(self, _ms):
        # A console that answers its cart only after `late_after` polls (TikTok is often slower than the page).
        self.waits = getattr(self, "waits", 0) + 1
        if getattr(self, "late_after", None) and self.waits == self.late_after:
            self._deliver()

    def evaluate(self, script):
        self.scripts.append(script)
        return {"rows": self.rows, "scroll": None}


class FakePinPage:
    """A small DOM fixture: stable card tokens survive the Pin/Unpin button transition."""
    url = CONSOLE

    def __init__(self, cards):
        self.cards = cards
        self.selected = None
        self.clicks = 0

    def is_closed(self):
        return False

    def evaluate(self, _script, token):
        if token is not None:
            self.selected = token
        return [dict(card) for card in self.cards]

    def locator(self, _selector):
        return self

    def click(self, **_kwargs):
        self.clicks += 1
        selected = next(card for card in self.cards if card["token"] == self.selected)
        selected["pinned"] = not selected["pinned"]
        if selected["pinned"]:
            for card in self.cards:
                if card is not selected:
                    card["pinned"] = False

    def wait_for_timeout(self, _ms):
        pass


def pin_card(index, name, *, product_id=None, image="https://img.example/a.jpg", price="199.000 ₫", pinned=False):
    return {"index": index, "token": f"00000000-0000-0000-0000-{index:012d}", "name": name,
            "image_url": image, "price_text": price,
            "product_ids": [product_id] if product_id else [], "pinned": pinned}


class AutoPinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pin = AutoPinService(Path(self.temp.name) / "profile")

    def tearDown(self):
        self.pin.close()
        self.temp.cleanup()

    def test_only_tiktok_live_console_hosts(self):
        for url in ("http://livecenter.tiktok.com/", "https://evil.example/",
                    "https://livecenter.tiktok.com.attacker.example/"):
            with self.assertRaises(ValueError):
                self.pin.open_browser(url)
            with self.assertRaises(ValueError):
                self.pin.login_browser(url)

    def test_configure_products_and_interval(self):
        s = self.pin.configure(["SKU-A", " SKU-B "], 15)
        self.assertEqual(s["products"], ["SKU-A", "SKU-B"])
        self.assertEqual(s["interval_seconds"], 15)
        with self.assertRaises(ValueError):
            self.pin.configure(["SKU-A", "SKU-A"], 15)
        with self.assertRaises(ValueError):
            self.pin.configure(["SKU-A"], 1)

    def test_start_requires_products(self):
        with self.assertRaises(RuntimeError):
            self.pin.start()
        self.assertFalse(self.pin.status()["enabled"])
        self.assertEqual(self.pin.stop()["state"], "stopped")

    def test_pin_exact_id_then_verify_and_unpin(self):
        page = FakePinPage([pin_card(0, "Trùng tên", product_id="1111111111111111111"),
                            pin_card(1, "Trùng tên", product_id="2222222222222222222")])
        self.pin._context, self.pin._page = object(), page
        target = {"tiktok_product_id": "2222222222222222222", "name": "Trùng tên"}
        result = self.pin.pin_product(target)
        self.assertEqual(result["card"]["index"], 1)
        self.assertEqual(self.pin.get_pin_state()["pinned_product_id"], "2222222222222222222")
        self.assertEqual(self.pin.unpin_product()["status"], "SUCCESS")
        self.assertIsNone(self.pin.get_pin_state()["pinned_card"])
        self.assertEqual(page.clicks, 2)
        self.pin._context = None

    def test_no_id_uses_unique_name_image_price_and_duplicate_requires_confirmation(self):
        cards = [pin_card(0, "Son đỏ"), pin_card(1, "Son đỏ")]
        page = FakePinPage(cards)
        self.pin._context, self.pin._page = object(), page
        target = {"name": "Son đỏ", "image_url": "https://img.example/a.jpg", "price_text": "199000"}
        self.assertTrue(self.pin.get_pin_state(target)["ambiguous"])
        with self.assertRaisesRegex(RuntimeError, "cần người dùng xác nhận"):
            self.pin.pin_product(target)
        self.assertEqual(page.clicks, 0)
        target["confirmed_token"] = cards[1]["token"]
        self.assertEqual(self.pin.pin_product(target)["card"]["index"], 1)
        self.pin._context = None

    def test_stale_video_never_clicks(self):
        page = FakePinPage([pin_card(0, "B")])
        self.pin._context, self.pin._page = object(), page
        with self.assertRaisesRegex(RuntimeError, "Video đã chuyển tiếp"):
            self.pin.pin_product({"name": "B"}, current=lambda: False)
        self.assertEqual(page.clicks, 0)
        self.pin._context = None

    def test_snapshot_resolves_missing_id_without_manual_entry(self):
        page = FakePinPage([pin_card(0, "Son đỏ")])
        self.pin._context, self.pin._page = object(), page
        self.pin._product_snapshot = {"complete": True, "products": [{"product_id": "1732299744789890081",
            "name": "Son đỏ", "image_url": "https://img.example/a.jpg", "price_text": "199.000 ₫"}]}
        result = self.pin.pin_product({"name": "Son đỏ", "image_url": "https://img.example/a.jpg",
                                       "price_text": "199000"})
        self.assertEqual(result["product_id"], "1732299744789890081")
        self.pin._context = None

    def test_duplicate_snapshot_needs_confirmation_even_if_only_one_card_is_visible(self):
        card = pin_card(0, "Son đỏ")
        page = FakePinPage([card])
        self.pin._context, self.pin._page = object(), page
        self.pin._product_snapshot = {"complete": True, "products": [
            {"product_id": "1732299744789890081", "name": "Son đỏ", "image_url": card["image_url"],
             "price_text": card["price_text"]},
            {"product_id": "1732299744789890082", "name": "Son đỏ", "image_url": card["image_url"],
             "price_text": card["price_text"]},
        ]}
        target = {"name": "Son đỏ", "image_url": card["image_url"], "price_text": card["price_text"]}
        with self.assertRaisesRegex(RuntimeError, "cần người dùng xác nhận"):
            self.pin.pin_product(target)
        self.assertEqual(page.clicks, 0)
        self.assertEqual(self.pin.pin_status()["status"], "FAILED")
        result = self.pin.pin_product({**target, "confirmed_token": card["token"]})
        self.assertEqual(result["status"], "SUCCESS")
        self.assertIsNone(result["product_id"])
        self.pin._context = None

    def test_known_id_cannot_guess_between_identical_cards_without_dom_ids(self):
        card = pin_card(0, "Son đỏ")
        page = FakePinPage([card])
        self.pin._context, self.pin._page = object(), page
        self.pin._product_snapshot = {"complete": True, "products": [
            {"product_id": pid, "name": "Son đỏ", "image_url": card["image_url"],
             "price_text": card["price_text"]} for pid in ("1732299744789890081", "1732299744789890082")
        ]}
        target = {"tiktok_product_id": "1732299744789890081", "name": "Son đỏ",
                  "image_url": card["image_url"], "price_text": card["price_text"]}
        self.assertTrue(self.pin.get_pin_state(target)["ambiguous"])
        with self.assertRaisesRegex(RuntimeError, "cần người dùng xác nhận"):
            self.pin.pin_product(target)
        self.assertEqual(page.clicks, 0)
        self.assertEqual(self.pin.pin_product({**target, "confirmed_token": card["token"]})["status"], "SUCCESS")
        self.pin._context = None

    def test_auto_pin_queue_rejects_an_older_configuration(self):
        self.pin._ensure_worker = lambda: None
        self.pin.set_video_auto(True, {7: {"name": "Son đỏ"}}, session_id=4)
        self.pin.queue_video_pin(7, lambda: True, session_id=4)
        _, (_, current, _), _ = self.pin._queue.get_nowait()
        self.assertTrue(current())
        self.pin.set_video_auto(False, {})
        self.pin.set_video_auto(True, {7: {"name": "Son đỏ"}}, session_id=4)
        self.assertFalse(current())

    def test_cloud_command_router_reaches_dom_worker(self):
        page = FakePinPage([pin_card(0, "Son đỏ")])
        self.pin._context, self.pin._page = object(), page
        self.pin._account_profile = {"user_id": "7494647113328657914"}
        playback = mock.Mock()
        playback.lock, playback.active, playback.session_id = threading.RLock(), True, 4
        router = CommandRouter(playback=playback, monitor=mock.Mock(), voice=mock.Mock(), auto_pin=self.pin)
        target = {"name": "Son đỏ", "expected_user_id": "7494647113328657914"}
        self.assertEqual(router.execute("PIN_PRODUCT", {"session_id": 4, "target": target})["status"], "SUCCESS")
        self.assertTrue(router.execute("GET_PIN_STATE", {})["pinned_card"])
        self.assertEqual(router.execute("UNPIN_PRODUCT", {"session_id": 4,
            "expected_user_id": "7494647113328657914"})["status"], "SUCCESS")
        self.assertEqual(page.clicks, 2)
        playback.active = False
        with self.assertRaisesRegex(RuntimeError, "Video đã chuyển tiếp"):
            router.execute("PIN_PRODUCT", {"session_id": 4, "target": target})
        page.cards[0]["pinned"] = True
        with self.assertRaisesRegex(RuntimeError, "Phiên LIVE đã kết thúc"):
            router.execute("UNPIN_PRODUCT", {"session_id": 4,
                "expected_user_id": "7494647113328657914"})
        self.assertEqual(page.clicks, 2)
        self.pin._context = None


class ChromePathTests(unittest.TestCase):
    ENV = {"PROGRAMFILES": "C:/Program Files", "PROGRAMFILES(X86)": "C:/Program Files (x86)",
           "LOCALAPPDATA": "C:/Users/me/AppData/Local"}

    @staticmethod
    def install(base):
        return str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")

    def test_registry_first(self):
        registered = "D:/Apps/Chrome/chrome.exe"
        self.assertEqual(find_chrome(self.ENV, exists=lambda p: True, registry=lambda: registered), registered)

    def test_standard_folders_when_registry_missing_or_stale(self):
        x86 = self.install("C:/Program Files (x86)")
        local = self.install("C:/Users/me/AppData/Local")
        found = lambda target: (lambda p: str(Path(p)) == str(Path(target)))  # noqa: E731
        self.assertEqual(Path(find_chrome(self.ENV, exists=found(x86), registry=lambda: None)), Path(x86))
        self.assertEqual(Path(find_chrome(self.ENV, exists=found(local), registry=lambda: "D:/gone/chrome.exe")),
                         Path(local))
        with self.assertRaises(RuntimeError):
            find_chrome({}, exists=lambda p: False, registry=lambda: None)


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.launched = []

        def popen(args):
            proc = FakeProc(args)
            self.launched.append(proc)
            return proc

        self.pin = AutoPinService(Path(self.temp.name) / "profile", chrome_finder=lambda: "C:/Chrome/chrome.exe",
                                  popen=popen)
        self.pin.LOGIN_CLOSE_SECONDS = 0.2

    def tearDown(self):
        self.pin.close()
        self.temp.cleanup()

    def test_login_starts_plain_chrome_on_the_shared_profile(self):
        status = self.pin.login_browser(CONSOLE)
        args = self.launched[0].args
        self.assertEqual(args[0], "C:/Chrome/chrome.exe")
        self.assertIn(f"--user-data-dir={self.pin.profile_dir}", args)
        self.assertEqual(args[-1], CONSOLE)
        self.assertFalse(any("automation" in a or "remote-debugging" in a for a in args))
        self.assertEqual((status["state"], status["login_open"]), ("login", True))
        with self.assertRaises(RuntimeError):
            self.pin.login_browser(CONSOLE)  # one login window at a time
        self.launched[0].alive = False  # the operator closed Chrome after signing in
        self.assertEqual(self.pin.status()["state"], "logged_in")

    def test_playwright_refused_while_login_chrome_holds_profile(self):
        self.pin.login_browser(CONSOLE)
        with mock.patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(RuntimeError) as exc:
                self.pin.open_browser(CONSOLE)
            self.assertIn("Chrome đăng nhập vẫn đang mở", str(exc.exception))
            self.pin.configure(["SKU-A"], 20)
            with self.assertRaises(RuntimeError) as exc:
                self.pin.start()
            self.assertIn("Chrome đăng nhập vẫn đang mở", str(exc.exception))
        self.assertIsNone(self.pin._context)
        self.launched[0].alive = False
        with mock.patch.object(AutoPinService, "_profile_locked", return_value=True):  # a helper still holds it
            with self.assertRaises(RuntimeError) as exc:
                self.pin.open_browser(CONSOLE)
            self.assertIn("Chrome đăng nhập vẫn đang mở", str(exc.exception))

    def test_finish_login_closes_gracefully_or_asks_the_operator(self):
        self.pin.login_browser(CONSOLE)
        with mock.patch.object(auto_pin.subprocess, "run") as taskkill:
            with self.assertRaises(RuntimeError) as exc:
                self.pin.finish_login()  # this fake Chrome never exits
        command = taskkill.call_args.args[0]
        self.assertEqual(command[:3], ["taskkill", "/PID", "4242"])
        self.assertNotIn("/F", command)  # a normal window close, so Chrome saves the session
        self.assertIn("Chrome đăng nhập vẫn đang mở", str(exc.exception))

    def test_redirect_to_login_stops_auto_pin(self):
        self.pin.configure(["SKU-A"], 20)
        self.pin._context = object()  # Playwright already attached
        self.pin._page = FakePage("https://www.tiktok.com/login?redirect_url=" + CONSOLE)
        self.assertTrue(self.pin.start()["enabled"])
        deadline = time.monotonic() + 5
        while self.pin.status()["enabled"] and time.monotonic() < deadline:
            time.sleep(0.02)
        status = self.pin.status()
        self.assertEqual((status["enabled"], status["state"]), (False, "login_required"))
        self.assertIn("đăng nhập lại", status["last_error"])
        self.pin._context = None

    def test_scan_reads_account_and_cart_from_the_console_responses(self):
        self.pin._context = object()
        self.pin._page = FakePage(CONSOLE, responses=console_responses())
        result = self.pin.scan_products()
        self.assertEqual(result["account_profile"]["user_id"], "7494647113328657914", "account_info data.user_id, not seller_id")
        self.assertEqual(result["product_snapshot"]["complete"], True)
        self.assertEqual([(p["seller_id"], p["product_id"]) for p in result["product_snapshot"]["products"]], [("9", "1"), ("9", "2")])
        self.assertEqual(result["products"], ["Son môi Đỏ", "Đồng hồ S9"])
        self.assertEqual(self.pin._page.scripts, [], "no DOM script when the console's own response is available")
        # Old console without the product response: DOM rows are diagnostics only, never a complete snapshot.
        self.pin._page = FakePage(CONSOLE, rows=[{"name": "Váy", "pinned": False}])
        self.assertFalse(self.pin.scan_products()["product_snapshot"]["complete"])
        self.pin._page = FakePage("https://www.tiktok.com/login")
        with self.assertRaises(RuntimeError):
            self.pin.scan_products()
        self.assertEqual(self.pin.status()["state"], "login_required")
        self.pin._context = None

    def test_scan_waits_for_a_slow_console_and_reloads_once_if_needed(self):
        self.pin._context = object()
        slow = FakePage(CONSOLE, responses=console_responses())
        slow.late_after = 40  # 8 s after the page loaded: the old 5 s wait gave up here
        self.pin._page = slow
        self.assertTrue(self.pin.scan_products()["product_snapshot"]["complete"])
        silent_first = FakePage(CONSOLE, responses=console_responses())
        silent_first.answer_on_reload = 2
        self.pin._page = silent_first
        self.assertTrue(self.pin.scan_products()["product_snapshot"]["complete"])
        self.assertEqual(silent_first.reloads, 2)
        never = FakePage(CONSOLE)
        self.pin._page = never
        with self.assertRaises(RuntimeError) as raised:
            self.pin.scan_products()
        self.assertIn("TikTok chưa trả danh sách sản phẩm", str(raised.exception))
        self.pin._context = None

    def test_scan_for_another_account_keeps_the_previous_list(self):
        self.pin._context = object()
        self.pin._page = FakePage(CONSOLE, responses=console_responses())
        self.pin.scan_products("7494647113328657914")
        self.pin._page = FakePage(CONSOLE, responses=console_responses(user_id="7495962653689350865", products=(("8", "5", "Khác"),)))
        with self.assertRaises(auto_pin.AccountMismatch):
            self.pin.scan_products("7494647113328657914")
        self.assertEqual(self.pin.status()["products"], ["Son môi Đỏ", "Đồng hồ S9"], "the wrong account's cart is not kept")
        self.pin._context = None

    def test_start_needs_a_manual_sync_first(self):
        """Products found on the page are never pinned wholesale without the user's explicit sync."""
        self.pin._context = object()
        self.pin._page = FakePage(CONSOLE, [{"name": "A", "pinned": False}, {"name": "B", "pinned": False}])
        with self.assertRaises(RuntimeError):
            self.pin.start()
        self.pin.configure(["A", "B"], 15)
        with mock.patch.object(AutoPinService, "_pin", side_effect=lambda term: term):
            status = self.pin.start()
            self.assertEqual((status["enabled"], status["products"]), (True, ["A", "B"]))
            self.pin.stop()
        self.pin._context = None

    def test_scan_needs_the_controlled_chrome(self):
        with self.assertRaises(RuntimeError) as exc:
            self.pin.scan_products()
        self.assertIn("Chưa mở Chrome điều khiển", str(exc.exception))

    def test_login_and_verify_urls(self):
        for url in ("https://www.tiktok.com/login", "https://seller-vn.tiktok.com/account/login?x=1",
                    "https://shop.tiktok.com/verify/identity", "https://accounts.tiktok.com/x"):
            self.assertTrue(needs_login(url), url)
        for url in (CONSOLE, CONSOLE + "?from=login_page", "https://livecenter.tiktok.com/live/1"):
            self.assertFalse(needs_login(url), url)


if __name__ == "__main__":
    unittest.main()


class CardMatchTests(unittest.TestCase):
    def test_spaces_and_a_struck_through_price_never_decide(self):
        card = pin_card(0, "Áo sơ mi back to school  - ST Design", price="219.000₫359.000₫")
        target = {"name": "Áo sơ mi back to school - ST Design", "image_url": "https://img.example/a.jpg",
                  "price_text": "219000"}
        self.assertTrue(AutoPinService._matches(card, target))
        self.assertFalse(AutoPinService._matches(card, {**target, "price_text": "359000"}))

"""Single Video Timeline: pins scheduled by the OBS playback position (sarae_agent/timeline), dry-run executors only.

The scenarios of the refactor spec (Phase 6): continuous 60-minute video, several products, seek, pause/resume,
loop, LIVE not confirmed, pin API failures, two markers at the same time, a timeline changed during
LIVE, a product without pin data, and LIVE stopped with an action on its way.
"""
import unittest
from concurrent.futures import Future
from types import SimpleNamespace

from sarae_agent.cloud.commands import CommandRouter
from sarae_agent.errors import AgentError
from sarae_agent.timeline.executors import ConsoleLiveGate, ConsolePinExecutor, DryRunPinExecutor
from sarae_agent.timeline.scheduler import TimelineScheduler
from test_playback_engine import ENDED, EngineTestBase, PLAYING

MIN = 60_000


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Gate:
    def __init__(self, live=True):
        self.live = live

    def is_live(self):
        return self.live


class ManualExecutor:
    """Records submissions; the test decides when and how each one finishes."""
    dry_run = True

    def __init__(self):
        self.calls = []

    def submit(self, action, target, current, done):
        self.calls.append(SimpleNamespace(action=action, target=target, current=current, done=done))

    def finish(self, ok=True, error=None):
        self.calls[-1].done(ok, error)


def product(pid):
    return {"sarae_product_id": pid, "tiktok_product_id": str(9000 + pid), "name": f"SP {pid}", "image_url": "",
            "price_text": "", "expected_user_id": ""}


def timeline(markers, version=1, products=(1, 2, 3)):
    return {"version": version, "expected_user_id": "777",
            "markers": [{"id": i + 1, "seq": 0, **m} for i, m in enumerate(markers)],
            "products": [product(p) for p in products]}


PLAN = [{"at_ms": 1 * MIN, "action": "PIN_PRODUCT", "product_id": 1},
        {"at_ms": 10 * MIN, "action": "PIN_PRODUCT", "product_id": 2},
        {"at_ms": 30 * MIN, "action": "UNPIN_PRODUCT", "product_id": None},
        {"at_ms": 40 * MIN, "action": "NO_ACTION", "product_id": None},
        {"at_ms": 45 * MIN, "action": "PIN_PRODUCT", "product_id": 3}]


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = Gate()
        self.executor = DryRunPinExecutor()
        self.events = []
        self.scheduler = TimelineScheduler(self.executor, self.gate, self.events.append, clock=self.clock)
        self.scheduler.load(7, timeline(PLAN))

    def play(self, start_ms, end_ms, step_ms=5_000, iteration=0, session_id=7):
        for position in range(start_ms, end_ms + 1, step_ms):
            self.clock.now += step_ms / 1000
            self.scheduler.observe(session_id, position, iteration)

    def actions(self):
        return [(a, (t or {}).get("tiktok_product_id")) for a, t in self.executor.calls]

    def kinds(self):
        return [e["type"] for e in self.events]

    def test_continuous_hour_pins_each_marker_once(self):
        self.play(0, 60 * MIN)
        self.assertEqual(self.actions(), [("PIN_PRODUCT", "9001"), ("PIN_PRODUCT", "9002"), ("UNPIN_PRODUCT", None),
                                          ("PIN_PRODUCT", "9003")])
        self.assertEqual(self.kinds().count("PIN_APPLIED"), 4)
        status = self.scheduler.status()
        self.assertEqual((status["desired_product_id"], status["actual_product_id"], status["dry_run"]), (3, 3, True))

    def test_polling_the_same_position_and_pause_never_pin_twice(self):
        for _ in range(20):
            self.scheduler.observe(7, 2 * MIN, 0)
        self.assertEqual(len(self.executor.calls), 1)

    def test_seek_applies_only_the_state_at_the_new_position(self):
        self.play(0, 2 * MIN)
        self.scheduler.observe(7, 46 * MIN, 0)  # forward over 10, 30 and 40 min: not replayed
        self.assertEqual(self.actions()[1:], [("PIN_PRODUCT", "9003")])
        self.scheduler.observe(7, 35 * MIN, 0)  # backward into the unpinned stretch
        self.scheduler.observe(7, 15 * MIN, 0)  # backward into product 2
        self.assertEqual(self.actions()[2:], [("UNPIN_PRODUCT", None), ("PIN_PRODUCT", "9002")])

    def test_loop_runs_the_schedule_again_and_keeps_the_pin_until_the_first_marker(self):
        self.play(0, 60 * MIN)
        self.play(0, 15 * MIN, iteration=1)
        self.assertEqual(self.actions()[4:], [("PIN_PRODUCT", "9001"), ("PIN_PRODUCT", "9002")],
                         "product 3 stays pinned from 0 to the first marker")

    def test_each_video_has_its_markers_and_the_pin_carries_over_to_the_next_one(self):
        a, b = "a" * 32, "b" * 32
        self.scheduler.load(8, timeline([{"media_id": a, "at_ms": 1 * MIN, "action": "PIN_PRODUCT", "product_id": 1},
                                          {"media_id": a, "at_ms": 5 * MIN, "action": "PIN_PRODUCT", "product_id": 2},
                                          {"media_id": b, "at_ms": 3 * MIN, "action": "PIN_PRODUCT", "product_id": 3},
                                          {"media_id": b, "at_ms": 4 * MIN, "action": "UNPIN_PRODUCT", "product_id": None}]))

        def play(media_id, iteration, end_ms):
            for position in range(0, end_ms + 1, 5_000):
                self.clock.now += 5
                self.scheduler.observe(8, position, iteration, media_id=media_id)

        play(a, 0, 6 * MIN)
        play(b, 1, 2 * MIN)
        self.assertEqual(self.actions(), [("PIN_PRODUCT", "9001"), ("PIN_PRODUCT", "9002")],
                         "video A's markers never apply to B; product 2 stays pinned into B")
        self.assertEqual(self.scheduler.status()["media_id"], b)
        play(b, 1, 5 * MIN)
        play(a, 2, 30_000)
        self.assertEqual(self.actions()[2:], [("PIN_PRODUCT", "9003"), ("UNPIN_PRODUCT", None)],
                         "B unpinned at its end: A starts unpinned until its first marker")
        play(b, 3, 30_000)
        self.scheduler.observe(8, 3 * MIN, 3, media_id=b)  # a seek inside B: its own state at the new position
        self.assertEqual(self.actions()[4:], [("PIN_PRODUCT", "9003")])

    def test_nothing_is_pinned_until_live_is_confirmed(self):
        self.gate.live = None
        self.play(0, 5 * MIN)
        self.gate.live = False
        self.play(5 * MIN, 12 * MIN)
        self.assertEqual(self.executor.calls, [])
        self.gate.live = True
        self.scheduler.observe(7, 12 * MIN, 0)
        self.assertEqual(self.actions(), [("PIN_PRODUCT", "9002")], "on air: the pin in force now, not the backlog")

    def test_failures_back_off_then_stop_until_the_next_marker(self):
        executor = ManualExecutor()
        scheduler = TimelineScheduler(executor, self.gate, self.events.append, clock=self.clock,
                                      retry_seconds=(3, 10), max_attempts=3)
        scheduler.load(7, timeline(PLAN))
        scheduler.observe(7, 2 * MIN, 0)
        executor.finish(False, "TikTok không phản hồi")
        scheduler.observe(7, 2 * MIN + 1000, 0)
        self.assertEqual(len(executor.calls), 1, "waits for the back-off")
        for _ in range(2):
            self.clock.now += 11
            scheduler.observe(7, 2 * MIN + 2000, 0)
            executor.finish(False, "TikTok không phản hồi")
        self.clock.now += 600
        for position in range(3 * MIN, 9 * MIN, 1000):
            scheduler.observe(7, position, 0)
        self.assertEqual(len(executor.calls), 3, "no infinite retries")
        failed = [e["detail"] for e in self.events if e["type"] == "PIN_FAILED"]
        self.assertEqual([f["final"] for f in failed], [False, False, True])
        scheduler.observe(7, 10 * MIN, 0)
        self.assertEqual(executor.calls[-1].target["tiktok_product_id"], "9002", "the next marker is tried again")

    def test_an_ack_that_never_comes_times_out_and_the_late_one_starts_nothing(self):
        executor = ManualExecutor()
        scheduler = TimelineScheduler(executor, self.gate, self.events.append, clock=self.clock, ack_timeout=30,
                                      retry_seconds=(1,))
        scheduler.load(7, timeline(PLAN))
        scheduler.observe(7, 2 * MIN, 0)
        first = executor.calls[0]
        self.clock.now += 31
        scheduler.observe(7, 2 * MIN + 31_000, 0)
        self.assertFalse(scheduler.status()["actual_known"])
        self.assertFalse(first.current(), "the timed-out attempt may not click any more")
        self.clock.now += 2
        scheduler.observe(7, 2 * MIN + 33_000, 0)
        self.assertEqual(len(executor.calls), 2)
        first.done(True, None)
        self.assertIsNotNone(scheduler.status()["in_flight"], "a late ack does not end the new attempt")
        executor.finish(True)
        self.assertEqual(scheduler.status()["actual_product_id"], 1)

    def test_stopping_live_cancels_the_action_on_its_way(self):
        executor = ManualExecutor()
        scheduler = TimelineScheduler(executor, self.gate, self.events.append, clock=self.clock)
        scheduler.load(7, timeline(PLAN))
        scheduler.observe(7, 2 * MIN, 0)
        scheduler.clear("stopped")
        self.assertFalse(executor.calls[0].current())
        executor.finish(True)
        scheduler.observe(7, 12 * MIN, 0)
        self.assertEqual(len(executor.calls), 1, "nothing runs after stop")
        self.assertEqual(scheduler.status()["session_id"], None)
        self.assertIn("TIMELINE_STOPPED", [e["type"] for e in self.events])

    def test_other_session_positions_are_ignored(self):
        self.play(0, 20 * MIN, session_id=8)
        self.assertEqual(self.executor.calls, [])

    def test_new_version_during_live_keeps_what_is_pinned(self):
        self.play(0, 12 * MIN)
        self.scheduler.load(7, timeline([{"at_ms": 0, "action": "PIN_PRODUCT", "product_id": 2},
                                         {"at_ms": 20 * MIN, "action": "PIN_PRODUCT", "product_id": 1}], version=2))
        self.scheduler.observe(7, 12 * MIN + 5000, 0)
        self.assertEqual(len(self.executor.calls), 2, "product 2 already pinned: no click")
        self.play(12 * MIN + 10_000, 21 * MIN)
        self.assertEqual(self.actions()[-1], ("PIN_PRODUCT", "9001"))
        self.assertEqual(self.scheduler.status()["version"], 2)

    def test_same_time_markers_the_later_one_wins_once(self):
        self.scheduler.load(7, timeline([{"at_ms": MIN, "action": "PIN_PRODUCT", "product_id": 1},
                                         {"at_ms": MIN, "action": "PIN_PRODUCT", "product_id": 2, "seq": 1}]))
        self.play(0, 3 * MIN)
        self.assertEqual(self.actions(), [("PIN_PRODUCT", "9002")])

    def test_product_without_pin_data_fails_once_and_never_loops(self):
        self.scheduler.load(7, timeline([{"at_ms": 0, "action": "PIN_PRODUCT", "product_id": 5}]))
        self.play(0, 10 * MIN)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual([e["detail"]["final"] for e in self.events if e["type"] == "PIN_FAILED"], [True])

    def test_never_unpins_blindly_when_nothing_is_known(self):
        self.play(0, 50 * 1000)  # before the first marker
        self.assertEqual(self.executor.calls, [])

    def test_no_timeline_plays_without_pins(self):
        self.scheduler.load(7, None)
        self.play(0, 60 * MIN, step_ms=60_000)
        self.assertEqual(self.executor.calls, [])


class EngineTimelineTests(EngineTestBase):
    """PlaybackEngine 'video' sessions feeding the scheduler with the OBS cursor."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.executor = DryRunPinExecutor()
        self.timeline_events = []
        self.scheduler = TimelineScheduler(self.executor, Gate(), self.timeline_events.append, clock=self.clock)
        self.engine.on_position = lambda p: self.scheduler.observe(p["session_id"], p["position_ms"], p["iteration"])
        self.main = self.files["pl1"]
        self.obs.duration["Sarae Video"] = 60 * MIN

    def run_video(self, minutes, step_ms=5_000, start_ms=0):
        """OBS plays the file: the cursor advances, the watchdog ticks; ENDED at the end of the hour."""
        position, elapsed = start_ms, 0
        while elapsed < minutes * MIN:
            position += step_ms
            elapsed += step_ms
            self.clock.now += step_ms / 1000
            if position >= 60 * MIN:
                self.obs.media_state["Sarae Video"], self.obs.cursor["Sarae Video"] = ENDED, 60 * MIN
                self.engine.tick()
                position -= 60 * MIN
                self.obs.cursor["Sarae Video"] = position
                continue
            self.obs.media_state["Sarae Video"], self.obs.cursor["Sarae Video"] = PLAYING, position
            self.engine.tick()

    def test_an_hour_on_loop_without_switching_files(self):
        self.engine.video_play(7, self.main, loop=True)
        self.scheduler.load(7, timeline(PLAN))
        self.run_video(130)  # two full loops and ten minutes
        files = {s.get("local_file") for s in self.obs.settings.values()}
        self.assertEqual(len(files), 1, "one main video only")
        self.assertEqual(self.engine.loop_iteration, 2)
        self.assertEqual(self.engine.status()["loop_iteration"], 2)
        pins = [(a, (t or {}).get("tiktok_product_id")) for a, t in self.executor.calls]
        one_loop = [("PIN_PRODUCT", "9001"), ("PIN_PRODUCT", "9002"), ("UNPIN_PRODUCT", None), ("PIN_PRODUCT", "9003")]
        again = [("PIN_PRODUCT", "9001"), ("PIN_PRODUCT", "9002")]  # product 3 stays pinned until the first marker
        self.assertEqual(pins, one_loop + again + one_loop[2:] + again)

    def test_pause_and_seek_through_the_engine(self):
        self.engine.video_play(7, self.main)
        self.scheduler.load(7, timeline(PLAN))
        self.run_video(2)
        self.engine.pause()
        for _ in range(10):
            self.engine.tick()  # paused: the watchdog does not even read OBS
        self.engine.resume()
        self.engine.seek(46 * MIN)
        self.obs.cursor["Sarae Video"] = 46 * MIN
        self.engine.tick()
        self.assertEqual([(t or {}).get("tiktok_product_id") for _, t in self.executor.calls], ["9001", "9003"])

    def test_without_loop_the_session_ends_with_the_video(self):
        self.engine.video_play(7, self.main, loop=False)
        self.obs.media_state["Sarae Video"] = ENDED
        self.engine.tick()
        self.assertFalse(self.engine.active)
        self.assertEqual(self.events[-1]["type"], "PLAYBACK_ENDED")

    def test_start_is_idempotent_and_exclusive(self):
        first = self.engine.video_play(7, self.main)
        again = self.engine.video_play(7, self.main, loop=False)
        self.assertTrue(again["unchanged"])
        self.assertFalse(self.engine.loop)
        self.assertEqual(self.obs.actions.count(("Sarae Video", "RESTART")), 1, "a retried start never restarts")
        with self.assertRaises(Exception):
            self.engine.video_play(8, self.main)
        with self.assertRaises(Exception):
            self.engine.product_play(7, 1, [self.files["p1a"]])
        self.assertTrue(first["ok"])

    def test_start_position_and_missing_file(self):
        self.engine.video_play(7, self.main, start_ms=90_000)
        self.assertEqual(self.obs.cursor_calls[-1], ("Sarae Video", 90_000))
        self.engine.stop()
        with self.assertRaises(Exception):
            self.engine.video_play(7, "f" * 32)
        with self.assertRaises(Exception):
            self.engine.video_play(7, media_ids=[self.main, "f" * 32])

    def playing(self):
        return next(mid for mid, path in self.paths.items() if str(path) == self.obs.settings["Sarae Video"]["local_file"])

    def end_video(self):
        self.obs.media_state["Sarae Video"] = ENDED
        self.engine.tick()

    def test_several_videos_play_one_after_another_and_report_which_one(self):
        positions = []
        self.engine.on_position = positions.append
        first, second = self.files["pl1"], self.files["pl2"]
        self.engine.video_play(7, media_ids=[first, second], loop=True)
        order = [self.playing()]
        for _ in range(3):
            self.end_video()
            order.append(self.playing())
        self.assertEqual(order, [first, second, first, second])
        self.assertEqual(self.engine.loop_iteration, 3)
        self.obs.media_state["Sarae Video"], self.obs.cursor["Sarae Video"] = PLAYING, 5_000
        self.engine.tick()
        self.assertEqual((positions[-1]["media_id"], positions[-1]["position_ms"], positions[-1]["iteration"]),
                         (second, 5_000, 3))
        again = self.engine.video_play(7, media_ids=[second, first], loop=False)
        self.assertTrue(again["unchanged"], "a retried start never restarts")
        self.end_video()
        self.assertFalse(self.engine.active, "no loop: the session ends after the last video")

    def test_resume_starts_at_a_given_video_and_position(self):
        first, second = self.files["pl1"], self.files["pl2"]
        self.engine.video_play(7, second, media_ids=[first, second], start_ms=30_000)
        self.assertEqual((self.playing(), self.obs.cursor_calls[-1]), (second, ("Sarae Video", 30_000)))

    def test_random_orders_never_play_the_same_video_twice_in_a_row(self):
        ids = self.ids("pl1", "pl2", "p1a")
        for order in ("random", "shuffle"):
            self.engine.video_play(7, media_ids=ids, video_order=order, loop=True)
            played = [self.playing()]
            for _ in range(11):
                self.end_video()
                played.append(self.playing())
            self.assertTrue(all(a != b for a, b in zip(played, played[1:])), order)
            if order == "shuffle":
                self.assertEqual([set(played[i:i + 3]) for i in (0, 3, 6, 9)], [set(ids)] * 4, "every video once per round")
            self.engine.stop()

    def test_random_without_loop_plays_each_video_once(self):
        ids = self.ids("pl1", "pl2", "p1a")
        self.engine.video_play(7, media_ids=ids, video_order="random", loop=False)
        played = [self.playing()]
        while self.engine.active:
            self.end_video()
            if self.engine.active:
                played.append(self.playing())
        self.assertEqual(sorted(played), sorted(ids))


class RouterTimelineTests(EngineTestBase):
    def setUp(self):
        super().setUp()
        self.executor = DryRunPinExecutor()
        self.scheduler = TimelineScheduler(self.executor, Gate(), clock=Clock())
        self.router = CommandRouter(playback=self.engine, monitor=None, voice=None, timeline=self.scheduler)
        self.main = self.files["pl1"]

    def test_video_play_loads_the_timeline_and_stop_clears_it(self):
        payload = {"session_id": 7, "media_id": self.main, "loop": True, "timeline": timeline(PLAN)}
        result = self.router.execute("video_play", payload)
        self.assertEqual((result["timeline"]["session_id"], result["timeline"]["version"]), (7, 1))
        changed = self.router.execute("timeline_set", {"session_id": 7, "timeline": timeline(PLAN[:1], version=2)})
        self.assertEqual((changed["version"], changed["markers"]), (2, 1))
        self.router.execute("playback_stop", {})
        self.assertIsNone(self.scheduler.status()["session_id"])
        with self.assertRaises(AgentError):
            self.router.execute("timeline_set", {"session_id": 7, "timeline": None})

    def test_video_play_of_several_videos(self):
        ids = self.ids("pl1", "pl2")
        result = self.router.execute("video_play", {"session_id": 7, "media_ids": ids, "video_order": "shuffle",
                                                    "timeline": timeline([{"media_id": ids[1], "at_ms": 0,
                                                                            "action": "PIN_PRODUCT", "product_id": 1}])})
        self.assertIn(result["media_id"], ids)
        self.assertEqual((self.engine.video_order, sorted(self.engine.ids)), ("shuffle", sorted(ids)))
        for bad in ({"session_id": 7}, {"session_id": 7, "media_ids": [ids[0], ids[0]]},
                    {"session_id": 7, "media_ids": ids[:1], "media_id": ids[1]},
                    {"session_id": 7, "media_ids": ids, "video_order": "backwards"}):
            with self.assertRaises(AgentError):
                self.router.execute("video_play", bad)

    def test_overlay_check_is_routed_to_the_overlay_service(self):
        calls = []
        router = CommandRouter(playback=self.engine, monitor=None, voice=None, timeline=self.scheduler,
                               overlay=SimpleNamespace(check=lambda sid: calls.append(sid) or {"ok": True}))
        self.assertEqual(router.execute("overlay_check", {"session_id": 7}), {"ok": True})
        self.assertEqual(calls, [7])
        with self.assertRaises(AgentError):
            router.execute("overlay_check", {"session_id": 0})
        with self.assertRaises(AgentError):
            CommandRouter(playback=self.engine, monitor=None, voice=None).execute("overlay_check", {"session_id": 7})

    def test_overlay_preview_is_routed_to_the_overlay_service(self):
        calls = []
        router = CommandRouter(playback=self.engine, monitor=None, voice=None, timeline=self.scheduler,
                               overlay=SimpleNamespace(preview_on_obs=lambda *a, **k: calls.append((a, k)) or {"preview": True}))
        router.execute("overlay_preview", {"session_id": 7, "timeline_id": 3, "media_id": "a" * 32, "position_ms": 1500, "playing": True,
                                           "iteration": 2})
        router.execute("overlay_preview", {"session_id": 7, "stop": True})
        self.assertEqual(calls, [((7, 3, "a" * 32, 1500, True, 120, False), {"iteration": 2}),
                                 ((7, None, None, 0, False, 120, True), {"iteration": 0})])
        with self.assertRaises(AgentError):
            router.execute("overlay_preview", {"session_id": 7, "media_id": "../x"})
        with self.assertRaises(AgentError):
            router.execute("overlay_preview", {"session_id": 7, "iteration": -1})

    def test_payloads_are_validated(self):
        bad = {"session_id": 7, "media_id": self.main,
               "timeline": {"version": 1, "markers": [{"id": 1, "at_ms": 0, "action": "SWITCH_VIDEO"}]}}
        with self.assertRaises(AgentError):
            self.router.execute("video_play", bad)
        with self.assertRaises(AgentError):
            self.router.execute("video_play", {"session_id": 7, "media_id": "not-an-id"})


class FakeAutoPin:
    def __init__(self):
        self.futures = []

    def _future(self, kind, *args):
        future = Future()
        self.futures.append((kind, args, future))
        return future

    def submit_pin(self, target, current, guard):
        return self._future("pin", target)

    def submit_unpin(self, expected_user_id, current, guard):
        return self._future("unpin", expected_user_id)

    def submit_live_state(self):
        return self._future("live")


class ConsoleAdapterTests(unittest.TestCase):
    def test_live_gate_reads_the_console_without_blocking_and_expires(self):
        clock, auto_pin, changes = Clock(), FakeAutoPin(), []
        gate = ConsoleLiveGate(auto_pin, clock=clock, refresh_seconds=5, max_age=20,
                               on_change=lambda s, e: changes.append(s))
        self.assertIsNone(gate.is_live(), "unknown until the Console answered")
        auto_pin.futures[-1][2].set_result({"state": "LIVE", "evidence": ["kết thúc live"]})
        self.assertTrue(gate.is_live())
        self.assertEqual(len(auto_pin.futures), 1, "no new read before refresh_seconds")
        clock.now += 6
        gate.is_live()
        auto_pin.futures[-1][2].set_result({"state": "OFFLINE"})
        self.assertFalse(gate.is_live())
        clock.now += 30  # the next read is still pending: the old answer is too old to trust
        auto_pin.futures[-1][2].set_exception(RuntimeError("page closed")) if not auto_pin.futures[-1][2].done() else None
        self.assertIsNone(gate.is_live())
        self.assertEqual(changes[:2], ["LIVE", "OFFLINE"])

    def test_console_executor_reports_failures(self):
        auto_pin, results = FakeAutoPin(), []
        executor = ConsolePinExecutor(auto_pin)
        executor.submit("PIN_PRODUCT", product(1), lambda: True, lambda ok, err: results.append((ok, err)))
        executor.submit("UNPIN_PRODUCT", {"expected_user_id": "777"}, lambda: True, lambda ok, err: results.append((ok, err)))
        auto_pin.futures[0][2].set_exception(RuntimeError("Không tìm thấy card"))
        auto_pin.futures[1][2].set_result({"status": "SUCCESS"})
        self.assertEqual(results, [(False, "Không tìm thấy card"), (True, None)])
        self.assertEqual(auto_pin.futures[1][1], ("777",))


if __name__ == "__main__":
    unittest.main()

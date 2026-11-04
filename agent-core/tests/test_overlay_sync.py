"""LIVE overlays in step with the real playback (Phase 4): the PlaybackEngine on a fake OBS feeds the pin scheduler
and the overlay service exactly as AgentRuntime wires them. Normal playback, pause / resume, seek both ways,
video A → B → A, loop, random / shuffle, a new timeline version, renderer reload / timeout, a missing image, OBS
reconnect, and no duplicate pin."""
import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace

from sarae_agent.overlay.cache import OverlayCache
from sarae_agent.overlay.service import OverlayService, shown_at, snapshot_problem
from sarae_agent.runtime import AgentRuntime
from sarae_agent.timeline.executors import DryRunPinExecutor
from sarae_agent.timeline.scheduler import TimelineScheduler
from test_overlay import PNG, FakeConfig, FakeObs as FakeObsScene, overlay
from test_playback_engine import ENDED, PLAYING, EngineTestBase
from test_timeline_scheduler import Clock, Gate, timeline

PAUSED = "OBS_MEDIA_STATE_PAUSED"


class OverlaySyncTests(EngineTestBase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.executor = DryRunPinExecutor()
        self.scheduler = TimelineScheduler(self.executor, Gate(), lambda e: None, clock=self.clock)
        self.scene_obs = FakeObsScene()
        self.a, self.b = self.files["pl1"], self.files["pl2"]
        self.snapshots = {7: self.snap("r1", 1)}
        self.service = OverlayService(FakeConfig(), self.scene_obs, lambda: True, lambda: ["Sarae Video"],
                                      lambda sid: self.snapshots.get(sid), lambda url: PNG, OverlayCache(Path(tempfile.mkdtemp())),
                                      clock=self.clock, server=SimpleNamespace(port=17891, start=lambda p: 17891, stop=lambda: None),
                                      playback_state=lambda: {"active": self.engine.active, "paused": self.engine.paused,
                                                              "session_id": self.engine.session_id})
        # AgentRuntime._on_position: pins first, then overlays.
        runtime = SimpleNamespace(timeline=self.scheduler, overlay=self.service)
        self.engine.on_position = lambda p: AgentRuntime._on_position(runtime, p)

    def snap(self, revision, version, extra=()):
        overlays = [overlay(1, self.a, 0, 5_000), overlay(2, self.a, 10_000, 5_000), overlay(3, self.b, 2_000, 4_000),
                    overlay(4, self.a, 20_000, 5_000, enabled=False), *extra]
        return {"revision": revision, "version": version, "timeline_id": version, "overlays": overlays, "products": {},
                "assets": {"x" * 32: {"url": "/api/live/overlay-assets/x/file", "sha256": hashlib.sha256(PNG).hexdigest()}}}

    # ----- driving -----
    def playing(self):
        return next(mid for mid, path in self.paths.items() if str(path) == self.obs.settings["Sarae Video"]["local_file"])

    def at(self, position, state=PLAYING):
        """OBS shows `position` of the loaded file; the watchdog ticks; the overlay service ticks."""
        self.clock.now += 1
        self.obs.media_state["Sarae Video"], self.obs.cursor["Sarae Video"] = state, position
        self.engine.tick()
        self.service.renderer_state()
        self.service.tick()
        return self.service.active_ids()

    def start(self, **kwargs):
        self.engine.video_play(7, media_ids=kwargs.pop("media_ids", [self.a, self.b]), loop=True, **kwargs)
        self.scheduler.load(7, timeline([{"media_id": self.a, "at_ms": 0, "action": "PIN_PRODUCT", "product_id": 1},
                                         {"media_id": self.a, "at_ms": 10_000, "action": "PIN_PRODUCT", "product_id": 2},
                                         {"media_id": self.b, "at_ms": 0, "action": "PIN_PRODUCT", "product_id": 3}]))
        self.service.tick()
        self.service.renderer_ready({"revision": self.service.renderer_state()["revision"], "ok": True})

    def end_video(self):
        self.clock.now += 1
        self.obs.media_state["Sarae Video"] = ENDED
        self.engine.tick()

    def shown(self):
        return self.scene_obs.ours()[0]["sceneItemEnabled"]

    def pins(self):
        return [(t or {}).get("tiktok_product_id") for _, t in self.executor.calls]

    # ----- scenarios -----
    def test_normal_playback_follows_start_and_end_of_each_overlay(self):
        self.start()
        expected = {0: [1], 4_999: [1], 5_000: [], 9_999: [], 10_000: [2], 14_999: [2], 15_000: [], 21_000: []}
        for position, ids in expected.items():
            self.assertEqual(self.at(position), ids, position)
        self.assertTrue(self.shown())
        self.assertEqual(self.pins(), ["9001", "9002"])

    def test_pause_holds_the_state_and_resume_continues(self):
        self.start()
        self.at(11_000)
        self.engine.pause()
        for _ in range(20):  # 20 s paused: the engine does not report, the overlay holds
            self.clock.now += 1
            self.engine.tick()
            self.service.renderer_state()
            self.service.tick()
        self.assertEqual(self.service.active_ids(), [2])
        self.assertTrue(self.shown())
        clock = self.service.renderer_state()["clock"]
        self.assertEqual((clock["position_ms"], clock["playing"]), (11_000, False), "no running on while paused")
        self.engine.resume()
        self.assertTrue(self.service.renderer_state()["clock"]["playing"])
        self.assertEqual(self.at(15_500), [])
        self.assertEqual(self.pins(), ["9002"], "joined at 11 s: only the pin in force there, once, pause included")

    def test_seek_forward_and_back_recomputes_without_replaying(self):
        self.start()
        self.at(1_000)
        self.engine.seek(12_000)  # reported at once, before any tick
        self.assertEqual(self.service.active_ids(), [2])
        self.engine.seek(2_000)
        self.assertEqual(self.service.active_ids(), [1])
        self.engine.seek(7_000)
        self.assertEqual(self.service.active_ids(), [])
        self.assertEqual(self.pins(), ["9001", "9002", "9001"], "the pin at the new position only, nothing in between")

    def test_video_a_then_b_then_a_shows_only_the_overlays_of_the_video_playing(self):
        self.start(media_ids=[self.a, self.b])
        self.at(1_000)
        self.assertEqual(self.service.active_ids(), [1])
        self.end_video()
        self.assertEqual((self.playing(), self.service.clock["media_id"], self.service.clock["position_ms"]), (self.b, self.b, 0),
                         "the switch is reported when the new video starts")
        self.assertEqual(self.service.active_ids(), [], "A's overlay at 0-5 s is gone at once")
        self.assertEqual(self.at(3_000), [3])
        self.end_video()
        self.assertEqual((self.playing(), self.service.active_ids()), (self.a, [1]))
        self.assertEqual(self.service.clock["iteration"], 2)
        self.assertNotIn(("Sarae Video", "STOP"), self.obs.actions, "the main video is never stopped by the overlay")

    def test_loop_shows_the_overlays_again_on_the_next_iteration(self):
        self.engine.video_play(7, media_ids=[self.a], loop=True)
        self.service.tick()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        seen = []
        for iteration in range(3):
            for position in (1_000, 6_000, 11_000):
                seen.append((self.service.clock and self.service.clock["iteration"], tuple(self.at(position))))
            self.end_video()
        self.assertEqual([ids for _, ids in seen], [(1,), (), (2,)] * 3)
        self.assertEqual([it for it, _ in seen[::3]], [0, 1, 2])

    def test_random_and_shuffle_always_follow_the_video_reported(self):
        ids = [self.a, self.b, self.files["p1a"]]
        for order in ("random", "shuffle"):
            self.engine.video_play(7, media_ids=ids, video_order=order, loop=True)
            self.service.tick()
            self.service.renderer_ready({"revision": "r1", "ok": True})
            for _ in range(8):
                self.at(3_000)
                playing = self.playing()
                self.assertEqual(self.service.clock["media_id"], playing)
                expected = {self.a: [1], self.b: [3]}.get(playing, [])
                self.assertEqual(self.service.active_ids(), expected, order)
                self.end_video()
            self.engine.stop()

    def test_a_new_version_applies_after_ready_without_touching_playback_or_pins(self):
        self.start()
        self.at(11_000)
        actions, pins = list(self.obs.actions), self.pins()
        self.snapshots[7] = self.snap("r2", 2, extra=[overlay(9, self.a, 10_000, 5_000, z=5)])
        self.service.request_sync()
        self.service.tick()
        self.assertEqual((self.service.renderer_state()["revision"], self.service.active_ids()), ("r2", [2]),
                         "the old revision is shown until the renderer is ready")
        self.service.renderer_ready({"revision": "r2", "ok": True})
        self.assertEqual(self.at(11_500), [2, 9])
        self.assertEqual((self.obs.actions, self.pins()), (actions, pins), "no restart, no pin")
        self.assertTrue(self.shown())

    def test_renderer_not_ready_or_timeout_keeps_the_previous_version(self):
        self.start()
        self.at(1_000)
        self.snapshots[7] = self.snap("r2", 2)
        self.service.request_sync()
        self.service.tick()
        self.service.renderer_ready({"revision": "r2", "ok": False, "missing": ["/api/overlay-files/x.png"]})
        self.assertEqual((self.service.status()["revision"], self.service.renderer_state()["revision"]), ("r1", "r1"))
        self.snapshots[7] = self.snap("r3", 3)
        self.service.request_sync()
        self.service.tick()
        for _ in range(25):
            self.at(1_500)
        self.assertEqual((self.service.status()["revision"], self.service.status()["error"]["code"]), ("r1", "RENDERER_TIMEOUT"))
        self.assertTrue(self.shown(), "the previous version stays on screen")

    def test_renderer_reload_reads_the_current_state_and_nothing_past(self):
        self.start()
        self.at(1_000)
        self.at(12_000)
        state = self.service.renderer_state({"revision": "r1", "seq": 1})  # a reloaded page asks once
        self.assertEqual((state["revision"], state["clock"]["position_ms"], state["show"]), ("r1", 12_000, True))
        self.assertEqual(self.service.active_ids(), [2], "only the overlay of the current position, 1 is not replayed")
        self.assertEqual(self.service.status()["renderer_ack"], {"revision": "r1", "seq": 1})

    def test_obs_reconnect_shows_the_state_of_the_current_position(self):
        self.start()
        self.at(1_000)
        self.scene_obs.connected = False
        self.at(2_000)
        self.scene_obs.connected = True
        self.assertEqual(self.at(11_000), [2])
        self.assertTrue(self.shown())

    def test_overlay_failure_never_stops_pins(self):
        broken = SimpleNamespace(observe=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("renderer broke")))
        runtime = SimpleNamespace(timeline=self.scheduler, overlay=broken)
        self.engine.on_position = lambda p: AgentRuntime._on_position(runtime, p)
        self.start()
        self.at(1_000)
        self.at(11_000)
        self.assertEqual(self.pins(), ["9001", "9002"])


class LimitTests(EngineTestBase):
    def test_shown_at_matches_the_web_rule(self):
        overlays = [overlay(1, "m", 1000, 1000, z=2), overlay(2, "m", 1500, 1000, z=1), overlay(3, "m", 0, 5000, enabled=False),
                    overlay(4, "other", 0, 5000)]
        self.assertEqual([o["id"] for o in shown_at(overlays, "m", 1500)], [2, 1], "drawing order by z_index")
        self.assertEqual([o["id"] for o in shown_at(overlays, "m", 2000)], [2], "end is exclusive")
        self.assertEqual(shown_at(overlays, "m", 999), [])

    def test_snapshot_limits(self):
        self.assertIsNone(snapshot_problem({"overlays": [overlay(1)]}))
        self.assertIsNotNone(snapshot_problem({"overlays": [overlay(i) for i in range(501)]}))
        slow = overlay(1)
        slow["animation_in"]["duration_ms"] = 9000
        self.assertIsNotNone(snapshot_problem({"overlays": [slow]}))
        broken = overlay(1)
        broken["duration_ms"] = 0
        self.assertIsNotNone(snapshot_problem({"overlays": [broken]}))
        crowded = [overlay(i, "m", 0, 1000) for i in range(5)]
        self.assertIn("cùng lúc", snapshot_problem({"overlays": crowded}))
        self.assertIsNone(snapshot_problem({"overlays": [overlay(i, "m", i * 1000, 1000) for i in range(5)]}), "one after another")

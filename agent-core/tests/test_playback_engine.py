"""PlaybackEngine against a fake obsws ReqClient.

Ported with the engine from the Cloud repository's backend/tests/test_playback_engine.py; keep both in step.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sarae_agent.playback import engine as playback_engine
from sarae_agent.playback.engine import PlaybackEngine, PlaybackError

ENDED = "OBS_MEDIA_STATE_ENDED"
PLAYING = "OBS_MEDIA_STATE_PLAYING"


class FakeObs:
    """Shared OBS state; every engine.client() call returns this object."""

    def __init__(self, scene_items=("Sarae Video",)):
        self.scene = "LIVE"
        self.scene_items = {name: i + 1 for i, name in enumerate(scene_items)}
        self.enabled = {}
        self.muted = {}
        self.mute_history = []
        self.settings = {}
        self.media_state = {}
        self.cursor = {}
        self.duration = {}
        self.actions = []
        self.transforms = {}
        self.cursor_calls = []
        self.fail = False
        self.base_client = SimpleNamespace(ws=SimpleNamespace(close=lambda: None))

    def _check(self):
        if self.fail:
            raise ConnectionError("OBS closed")

    def get_version(self):
        self._check()
        return SimpleNamespace(obs_version="30.1.0")

    def get_current_program_scene(self):
        self._check()
        return SimpleNamespace(current_program_scene_name=self.scene)

    def get_scene_item_id(self, scene, name):
        if name not in self.scene_items:
            raise KeyError(name)
        return SimpleNamespace(scene_item_id=self.scene_items[name])

    def get_scene_item_list(self, scene):
        return SimpleNamespace(scene_items=[{"sourceName": n} for n in self.scene_items])

    def get_video_settings(self):
        return SimpleNamespace(base_width=1080, base_height=1920)

    def set_scene_item_transform(self, scene, item_id, transform):
        self.transforms[item_id] = transform

    def get_input_list(self):
        names = list(self.scene_items) + ["Mic"]
        return SimpleNamespace(inputs=[{"inputName": n, "inputKind": "dshow_input" if n == "Camera" else
                                 "ffmpeg_source" if n != "Mic" else "wasapi_input_capture"}
                                       for n in names])

    def get_media_input_status(self, name):
        self._check()
        return SimpleNamespace(media_state=self.media_state.get(name, PLAYING),
                               media_cursor=self.cursor.get(name, 0), media_duration=self.duration.get(name, 60000))

    def set_input_settings(self, name, settings, overlay=True):
        self._check()
        self.settings.setdefault(name, {}).update(settings)

    def get_input_settings(self, name):
        return SimpleNamespace(input_settings=dict(self.settings.get(name, {})))

    def trigger_media_input_action(self, name, action):
        self._check()
        self.actions.append((name, action.rsplit("_", 1)[-1]))
        if action.endswith("RESTART"):
            self.media_state[name] = PLAYING
            self.cursor[name] = 0
        elif action.endswith("PAUSE"):
            self.media_state[name] = "OBS_MEDIA_STATE_PAUSED"
        elif action.endswith("PLAY"):
            self.media_state[name] = PLAYING

    def set_input_mute(self, name, muted):
        self.muted[name] = muted
        self.mute_history.append(dict(self.muted))

    def get_input_mute(self, name):
        return SimpleNamespace(input_muted=self.muted.get(name, False))

    def set_scene_item_enabled(self, scene, item_id, enabled):
        self.enabled[item_id] = enabled

    def set_media_input_cursor(self, name, ms):
        self.cursor_calls.append((name, ms))


class EngineTestBase(unittest.TestCase):
    dual = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        self.files = {}
        for name in ("p1a", "p1b", "p1c", "p2a", "p2b", "pl1", "pl2"):
            path = root / f"{name}.mp4"
            path.write_bytes(b"00")
            self.files[name] = hashlib.md5(str(path).casefold().encode()).hexdigest()  # 32-hex like Media Library ids
        self.paths = {mid: root / f"{name}.mp4" for name, mid in self.files.items()}
        self.obs = FakeObs(("Sarae Video A", "Sarae Video B") if self.dual else ("Sarae Video",))
        self.events = []
        config_file = root / "obs_config.json"
        if self.dual:
            playback_engine.save_obs_config(playback_engine.ObsConfig(dual_mode=True), config_file)
        self.engine = PlaybackEngine(resolve=self.resolve, on_event=self.events.append,
                                     config_file=config_file, client_factory=lambda cfg: self.obs)
        sleep = mock.patch.object(playback_engine.time, "sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        media_probe = mock.patch.object(playback_engine, "probe_media", return_value=SimpleNamespace(rotation=0))
        media_probe.start()
        self.addCleanup(media_probe.stop)
        media_file = mock.patch.object(playback_engine, "obs_file", side_effect=lambda path, info, canvas=None: path)
        media_file.start()
        self.addCleanup(media_file.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, media_id):
        path = self.paths.get(media_id)
        if path is None:
            raise ValueError("Video không tồn tại trên máy Agent")
        return path

    def ids(self, *names):
        return [self.files[n] for n in names]

    def path(self, name):
        return str(self.paths[self.files[name]].resolve())

    def end_current(self):
        self.obs.media_state[self.engine.active_source()] = ENDED
        self.engine.tick()


class SingleSourceTests(EngineTestBase):
    def test_camera_fallback_and_return_keep_session(self):
        self.obs.scene_items["Camera"] = 2
        self.obs.settings["Camera"] = {"video_device_id": "device-1"}
        self.engine.config = self.engine.config.model_copy(update={"camera_input": "Camera"})
        self.engine.store.save(self.engine.config)
        self.engine.start_playlist(self.ids("pl1", "pl2"))
        session = self.engine.session_started_at
        self.assertEqual(self.engine.use_camera()["source_mode"], "camera")
        self.assertTrue(self.engine.active)
        self.assertFalse(self.obs.enabled[1])
        self.assertTrue(self.obs.enabled[2])
        self.assertTrue(self.obs.muted["Sarae Video"])
        self.obs.media_state["Sarae Video"] = ENDED
        self.engine.tick()
        self.assertEqual(self.engine.index, 0)
        self.assertEqual(self.engine.use_video()["source_mode"], "video")
        self.assertTrue(self.obs.enabled[1])
        self.assertFalse(self.obs.enabled[2])
        self.assertFalse(self.obs.muted["Sarae Video"])
        self.assertEqual(self.engine.session_started_at, session)

    def test_camera_missing_keeps_video_visible(self):
        self.engine.config = self.engine.config.model_copy(update={"camera_input": "Missing"})
        self.engine.store.save(self.engine.config)
        self.engine.start_playlist(self.ids("pl1"))
        with self.assertRaises(PlaybackError):
            self.engine.use_camera()
        self.assertFalse(self.engine.camera_mode)
        self.assertFalse(self.obs.muted.get("Sarae Video", False))

    def test_camera_without_device_keeps_video_visible(self):
        self.obs.scene_items["Camera"] = 2
        self.engine.config = self.engine.config.model_copy(update={"camera_input": "Camera"})
        self.engine.store.save(self.engine.config)
        self.engine.start_playlist(self.ids("pl1"))
        with self.assertRaises(PlaybackError):
            self.engine.use_camera()
        self.assertFalse(self.engine.camera_mode)
        self.assertFalse(self.obs.muted["Sarae Video"])

    def test_stop_on_camera_then_restart_video(self):
        self.obs.scene_items["Camera"] = 2
        self.obs.settings["Camera"] = {"video_device_id": "device-1"}
        self.engine.config = self.engine.config.model_copy(update={"camera_input": "Camera"})
        self.engine.store.save(self.engine.config)
        self.engine.start_playlist(self.ids("pl1"))
        self.engine.use_camera()
        self.engine.stop()
        self.engine.start_playlist(self.ids("pl2"))
        self.assertTrue(self.obs.enabled[1])
        self.assertFalse(self.obs.enabled[2])
        self.assertFalse(self.engine.camera_mode)

    def test_product_play_immediate_starts_and_switches(self):
        result = self.engine.product_play(7, 11, self.ids("p1a", "p1b"), "immediate")
        self.assertEqual(result["index"], 0)
        status = self.engine.status()
        self.assertTrue(status["running"])
        self.assertEqual((status["session_mode"], status["session_id"], status["product_id"]), ("product", 7, 11))
        self.assertEqual(status["queue_ids"], self.ids("p1a", "p1b"))
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p1a"))
        self.assertEqual(self.obs.transforms[1]["boundsType"], "OBS_BOUNDS_SCALE_INNER")
        self.assertEqual(self.obs.transforms[1]["rotation"], 0)
        self.engine.product_play(7, 12, self.ids("p2a"), "immediate")
        self.assertEqual(self.engine.product_id, 12)
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p2a"))
        self.assertEqual(self.events, [], "an immediate switch is answered by the command result")

    def test_after_current_switches_at_boundary_and_emits_event(self):
        self.engine.product_play(7, 11, self.ids("p1a", "p1b"), "immediate")
        reply = self.engine.product_play(7, 12, self.ids("p2a", "p2b"), "after_current")
        self.assertEqual(reply, {"ok": True, "pending_product_id": 12})
        self.assertEqual(self.engine.status()["pending_product_id"], 12)
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p1a"))
        self.end_current()
        self.assertEqual(self.engine.product_id, 12)
        self.assertIsNone(self.engine.pending_product_id)
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p2a"))
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual((event["type"], event["session_id"], event["product_id"]), ("PRODUCT_SWITCHED", 7, 12))
        self.assertEqual(event["detail"]["trigger"], "video_end")
        self.assertEqual(event["detail"]["from_product_id"], 11)

    def test_after_current_same_product_and_cancel(self):
        self.engine.product_play(7, 11, self.ids("p1a"), "immediate")
        self.assertEqual(self.engine.product_play(7, 11, self.ids("p1a", "p1b"), "after_current")["pending_product_id"], None)
        self.engine.product_play(7, 12, self.ids("p2a"), "after_current")
        self.assertEqual(self.engine.cancel_pending(), {"ok": True, "cancelled_product_id": 12})
        self.end_current()
        # Same product continues with its refreshed lineup.
        self.assertEqual(self.engine.product_id, 11)
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p1b"))

    def test_product_queue_loops_and_skips_missing_files(self):
        self.engine.product_play(7, 11, self.ids("p1a", "p1b", "p1c"), "immediate")
        self.paths[self.files["p1b"]].unlink()
        self.end_current()
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p1c"))
        self.end_current()
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p1a"))
        self.assertTrue(self.engine.active)

    def test_next_applies_pending_switch_with_event(self):
        self.engine.product_play(7, 11, self.ids("p1a", "p1b"), "immediate")
        self.engine.product_play(7, 12, self.ids("p2a"), "after_current")
        self.engine.next()
        self.assertEqual(self.engine.product_id, 12)
        self.assertEqual(self.events[0]["detail"]["trigger"], "next")

    def test_product_play_rejects_other_session_and_empty_lineup(self):
        self.engine.product_play(7, 11, self.ids("p1a"), "immediate")
        with self.assertRaises(PlaybackError) as ctx:
            self.engine.product_play(8, 12, self.ids("p2a"), "immediate")
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(PlaybackError) as ctx:
            self.engine.product_play(7, 13, ["f" * 32], "immediate")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.engine.product_id, 11)

    def test_select_seek_pause_resume_previous_stop(self):
        self.engine.product_play(7, 11, self.ids("p1a", "p1b", "p1c"), "immediate")
        self.engine.select(2)
        self.assertEqual(self.engine.index, 2)
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("p1c"))
        with self.assertRaises(PlaybackError) as ctx:
            self.engine.select(5)
        self.assertEqual(ctx.exception.status_code, 422)
        self.engine.seek(15000)
        self.assertEqual(self.obs.cursor_calls[-1], ("Sarae Video", 15000))
        with self.assertRaises(PlaybackError) as ctx:
            self.engine.seek(61000)
        self.assertEqual(ctx.exception.status_code, 422)
        self.engine.pause()
        self.assertTrue(self.engine.status()["paused"])
        self.engine.resume()
        self.engine.previous()
        self.assertEqual(self.engine.index, 1)
        self.engine.product_play(7, 12, self.ids("p2a"), "after_current")
        self.engine.stop()
        status = self.engine.status()
        self.assertFalse(status["running"])
        self.assertIsNone(status["pending_product_id"])
        self.assertEqual(self.obs.actions[-1], ("Sarae Video", "STOP"))
        with self.assertRaises(PlaybackError) as ctx:
            self.engine.seek(0)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_playlist_mode_end_emits_playback_ended(self):
        self.engine.start_playlist(self.ids("pl1", "pl2"), loop=False)
        self.assertEqual(self.engine.status()["session_mode"], "playlist")
        with self.assertRaises(PlaybackError):
            self.engine.start_playlist(self.ids("pl1"))
        self.end_current()
        self.assertEqual(self.obs.settings["Sarae Video"]["local_file"], self.path("pl2"))
        self.end_current()
        self.assertFalse(self.engine.active)
        self.assertEqual([e["type"] for e in self.events], ["PLAYBACK_ENDED"])
        self.assertEqual(self.events[0]["detail"]["reason"], "playlist_end")

    def test_obs_errors_retry_then_end(self):
        self.engine.product_play(7, 11, self.ids("p1a"), "immediate")
        self.obs.fail = True
        for _ in range(5):
            self.engine.next_retry_at = 0
            self.engine.tick()
        self.assertFalse(self.engine.active)
        self.assertEqual(self.engine.error_count, 5)
        self.assertEqual(self.events[-1]["detail"]["reason"], "obs_error")

    def test_status_shape_matches_live_status(self):
        self.engine.product_play(7, 11, self.ids("p1a"), "immediate")
        status = self.engine.status()
        for key in ("connected", "obs_version", "media_state", "media_cursor", "media_duration", "running", "paused",
                    "loop", "session_mode", "playlist_id", "product_id", "pending_product_id", "queue_ids", "index",
                    "count", "last_status", "retry_count", "error_count", "transition_count", "recovery_count",
                    "session_seconds", "session_id", "obs"):
            self.assertIn(key, status)
        self.assertEqual(set(status["obs"]), {"host", "port", "input_name", "dual_mode", "dual_sources", "has_password", "camera_input"})
        self.obs.fail = True
        offline = self.engine.status()
        self.assertFalse(offline["connected"])
        self.assertIn("queue_ids", offline)

    def test_configure_keeps_password_and_never_returns_it(self):
        self.engine.configure(host="127.0.0.1", port=4455, password="s3cret", input_name="Sarae Video")
        status = self.engine.configure(host="127.0.0.1", port=4456, password="", input_name="Sarae Video")
        self.assertEqual(self.engine.config.password, "s3cret")
        self.assertTrue(status["obs"]["has_password"])
        self.assertNotIn("s3cret", repr(status))
        self.assertEqual(playback_engine.load_obs_config(self.engine.config_file).port, 4456)
        with self.assertRaises(PlaybackError):
            self.engine.configure(host="10.0.0.5")
        with self.assertRaises(PlaybackError):
            self.engine.configure(dual_mode=True, source_a="X", source_b="X")

    def test_videos_are_prepared_for_the_obs_canvas_and_prewarmed(self):
        with mock.patch.object(playback_engine, "obs_file", side_effect=lambda path, info, canvas=None: path) as render:
            result = self.engine.prewarm(self.ids("pl1", "pl2") + ["0" * 32])
            self.assertEqual(result, {"ready": 2, "failed": 1})
            self.assertEqual({c.args[2] for c in render.call_args_list}, {(1080, 1920)})
            self.engine.start_playlist(self.ids("pl1", "pl2"), loop=True)
            # Prewarmed copies are reused at play time.
            self.assertEqual(render.call_count, 2)

    def test_list_inputs(self):
        result = self.engine.list_inputs("127.0.0.1", 4455, "")
        self.assertEqual(result["scene"], "LIVE")
        self.assertEqual(result["inputs"][0], {"name": "Sarae Video", "kind": "ffmpeg_source", "in_scene": True})
        self.assertFalse(result["inputs"][-1]["in_scene"])


class DualModeTests(EngineTestBase):
    dual = True

    def test_rapid_switches_never_unmute_both_video_sources(self):
        self.engine.start_playlist(self.ids("pl1", "pl2"), loop=True)
        for _ in range(12):
            self.engine.next()
        a, b = "Sarae Video A", "Sarae Video B"
        assert all(states.get(a, True) or states.get(b, True) for states in self.obs.mute_history)
        self.assertEqual(self.engine.transition_count, 12)

    def test_dual_sources_stage_next_video_and_swap(self):
        self.engine.product_play(7, 11, self.ids("p1a", "p1b", "p1c"), "immediate")
        a, b = "Sarae Video A", "Sarae Video B"
        self.assertEqual(self.engine.slots, [a, b])
        self.assertTrue(self.obs.enabled[1])
        self.assertFalse(self.obs.enabled[2])
        self.assertFalse(self.obs.muted[a])
        self.assertTrue(self.obs.muted[b])
        self.assertEqual(self.obs.settings[a]["local_file"], self.path("p1a"))
        # Stand-by source preloads the following video.
        self.assertEqual(self.obs.settings[b]["local_file"], self.path("p1b"))
        self.end_current()
        self.assertEqual(self.engine.active_slot, 1)
        self.assertTrue(self.obs.enabled[2])
        self.assertFalse(self.obs.enabled[1])
        self.assertFalse(self.obs.muted[b])
        self.assertTrue(self.obs.muted[a])
        self.assertEqual(self.obs.settings[a]["local_file"], self.path("p1c"))
        self.assertEqual(self.engine.status()["obs"]["dual_sources"], [a, b])

    def test_dual_after_current_switch_restages(self):
        self.engine.product_play(7, 11, self.ids("p1a", "p1b"), "immediate")
        self.engine.product_play(7, 12, self.ids("p2a", "p2b"), "after_current")
        self.assertEqual(self.obs.settings["Sarae Video B"]["local_file"], self.path("p2a"))
        self.end_current()
        a, b = "Sarae Video A", "Sarae Video B"
        # B was already staged with the pending product before the current video ended.
        self.assertEqual(self.obs.settings[b]["local_file"], self.path("p2a"))
        self.assertEqual(self.obs.settings[a]["local_file"], self.path("p2b"))
        self.assertEqual(self.events[0]["type"], "PRODUCT_SWITCHED")

    def test_standby_waits_paused_on_first_frame_and_resumes_when_shown(self):
        self.engine.start_playlist(self.ids("pl1", "pl2"), loop=True)
        a, b = "Sarae Video A", "Sarae Video B"
        for name in (a, b):
            self.assertFalse(self.obs.settings[name]["clear_on_media_end"])
            self.assertFalse(self.obs.settings[name]["restart_on_activate"])
        self.assertEqual(self.obs.actions[-1], (b, "PAUSE"))
        self.assertEqual(self.obs.media_state[a], PLAYING)
        self.obs.actions.clear()
        self.engine.next()
        self.assertEqual(self.obs.actions[0], (b, "PLAY"))
        self.assertEqual(self.obs.media_state[b], PLAYING)
        self.assertFalse(self.obs.muted[b])

    def test_swaps_just_before_the_visible_video_ends(self):
        timers = []

        class FakeTimer:
            def __init__(self, delay, fn, args=()):
                timers.append((delay, fn, args))
                self.daemon = False

            def start(self):
                pass

        self.engine.start_playlist(self.ids("pl1", "pl2"), loop=True)
        a = "Sarae Video A"
        with mock.patch.object(playback_engine.threading, "Timer", FakeTimer):
            self.obs.cursor[a] = 30000
            self.engine.tick()
            self.assertEqual(timers, [])
            self.obs.cursor[a] = 59000
            self.engine.tick()
            self.engine.tick()
        self.assertEqual(len(timers), 1)
        delay, fn, args = timers[0]
        self.assertAlmostEqual(delay, 0.75)
        self.obs.cursor[a] = 59760
        fn(*args)
        self.assertEqual(self.engine.active_slot, 1)
        self.assertEqual(self.engine.transition_count, 1)

    def test_near_end_swap_skips_after_seek_back(self):
        self.engine.start_playlist(self.ids("pl1", "pl2"), loop=True)
        generation = self.engine.generation
        self.obs.cursor["Sarae Video A"] = 10000
        self.engine._swap_near_end(generation)
        self.assertEqual(self.engine.active_slot, 0)
        self.assertIsNone(self.engine._swap_generation)

    def test_dual_requires_both_sources(self):
        del self.obs.scene_items["Sarae Video B"]
        with self.assertRaises(PlaybackError) as ctx:
            self.engine.product_play(7, 11, self.ids("p1a"), "immediate")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertFalse(self.engine.active)


if __name__ == "__main__":
    unittest.main()

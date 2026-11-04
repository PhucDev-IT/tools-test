"""Playback policy (docs/PLAYBACK_POLICY.md): video order and automatic product rotation in PlaybackEngine."""
import random
from unittest import mock

import pytest

from sarae_agent.cloud.commands import CommandRouter, lane
from sarae_agent.errors import AgentError
from sarae_agent.playback import engine as playback_engine
from sarae_agent.playback.engine import PlaybackError
from test_playback_engine import EngineTestBase

A, B, C = 11, 12, 13


class PolicyTestBase(EngineTestBase):
    def setUp(self):
        super().setUp()
        self.engine.rng = random.Random(1234)

    def lineup(self, b=("p2a", "p2b")):
        return [{"product_id": A, "media_ids": self.ids("p1a", "p1b", "p1c")},
                {"product_id": B, "media_ids": self.ids(*b) if b else ["f" * 32]},
                {"product_id": C, "media_ids": self.ids("pl1", "pl2")}]

    def policy(self, **changes):
        self.engine.set_policy(7, {"rotate_after_videos": None, **changes}, self.lineup())

    def start(self, product=A, names=("p1a", "p1b", "p1c")):
        self.engine.product_play(7, product, self.ids(*names), "immediate")

    def current(self):
        return self.engine.ids[self.engine.index]

    def played(self, ends):
        seen = [self.current()]
        for _ in range(ends):
            self.end_current()
            seen.append(self.current())
        return seen

    def products(self, ends):
        seen = [self.engine.product_id]
        for _ in range(ends):
            self.end_current()
            seen.append(self.engine.product_id)
        return seen

    def switches(self):
        return [e for e in self.events if e["type"] == "PRODUCT_SWITCHED"]


class VideoOrderTests(PolicyTestBase):
    def test_sequential_is_unchanged(self):
        self.policy()
        self.start()
        self.assertEqual(self.played(4), self.ids("p1a", "p1b", "p1c", "p1a", "p1b"))
        self.engine.next()
        self.assertEqual(self.current(), self.files["p1c"])

    def test_random_never_repeats_and_covers_videos(self):
        self.policy(video_order="random")
        self.start()
        seen = self.played(30)
        self.assertTrue(all(a != b for a, b in zip(seen, seen[1:])))
        self.assertEqual(set(seen), set(self.ids("p1a", "p1b", "p1c")))
        for _ in range(5):
            before = self.current()
            self.engine.next()
            self.assertNotEqual(self.current(), before)

    def test_shuffle_plays_each_video_once_per_round(self):
        self.policy(video_order="shuffle")
        self.start()
        seen = self.played(11)
        everything = set(self.ids("p1a", "p1b", "p1c"))
        for round_start in range(0, 12, 3):
            self.assertEqual(set(seen[round_start:round_start + 3]), everything, seen)
        self.assertTrue(all(a != b for a, b in zip(seen, seen[1:])))

    def test_single_video_product_keeps_looping(self):
        self.engine.set_policy(7, {"video_order": "shuffle"}, [])
        self.start(names=("p1a",))
        self.assertEqual(self.played(3), self.ids("p1a", "p1a", "p1a", "p1a"))

    def test_select_and_previous_still_work(self):
        self.policy(video_order="random")
        self.start()
        self.engine.select(2)
        self.assertEqual(self.current(), self.files["p1c"])
        self.engine.previous()
        self.assertEqual(self.engine.index, 1)

    def test_skipped_video_emits_error(self):
        self.engine.start_playlist(self.ids("pl1", "pl2", "p1a"))
        self.paths[self.files["pl2"]].unlink()
        self.end_current()
        self.assertEqual(self.current(), self.files["p1a"])
        errors = [e for e in self.events if e["type"] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["detail"]["code"], "video_skipped")
        self.assertEqual(errors[0]["detail"]["media_id"], self.files["pl2"])
        self.assertTrue(errors[0]["detail"]["message"])

    def test_corrupt_video_in_random_product_is_skipped(self):
        self.policy(video_order="random")
        self.start(names=("p1a", "p1b"))
        self.engine.select(0)
        broken = self.paths[self.files["p1b"]].resolve()

        def probe(path):
            if path == broken:
                raise ValueError("corrupt")
            return mock.DEFAULT

        playback_engine.probe_media.side_effect = probe
        self.engine._prepared.clear()
        self.end_current()
        # p1b cannot play: p1a (the only other choice) starts again.
        self.assertEqual(self.current(), self.files["p1a"])
        self.assertTrue(self.engine.active)
        self.assertEqual([e["detail"]["media_id"] for e in self.events if e["type"] == "ERROR"], [self.files["p1b"]])


class RotationTests(PolicyTestBase):
    def test_rotation_after_videos_sequential(self):
        self.policy(product_rotation="sequential", rotate_after_videos=2)
        self.start()
        self.assertEqual(self.engine.metrics()["rotation"],
                         {"auto": True, "videos_in_product": 1, "product_seconds": 0, "next_product_id": B})
        self.assertEqual(self.products(7), [A, A, B, B, C, C, A, A])
        switches = self.switches()
        self.assertEqual([e["detail"]["trigger"] for e in switches], ["auto_rotation"] * 3)
        self.assertEqual([(e["detail"]["from_product_id"], e["product_id"]) for e in switches],
                         [(A, B), (B, C), (C, A)])
        self.assertEqual(switches[0]["detail"]["media_id"], self.files["p2a"])

    def test_rotation_shuffle_over_three_products(self):
        self.policy(product_rotation="shuffle", rotate_after_videos=1)
        self.start()
        seen = self.products(11)
        self.assertEqual(set(seen[0:3]), {A, B, C}, seen)
        for round_start in (3, 6, 9):
            self.assertEqual(set(seen[round_start:round_start + 3]), {A, B, C}, seen)
        self.assertTrue(all(a != b for a, b in zip(seen, seen[1:])))

    def test_rotation_random_starts_with_policy_video(self):
        self.policy(product_rotation="random", rotate_after_videos=1, video_order="shuffle")
        self.start()
        seen = self.products(10)
        self.assertTrue(all(a != b for a, b in zip(seen, seen[1:])))
        self.assertEqual(set(seen), {A, B, C})

    def test_rotation_after_minutes_after_current(self):
        self.policy(product_rotation="sequential", rotate_after_minutes=1)
        self.start()
        self.engine.tick()
        self.engine.product_started_at -= 61
        self.engine.tick()
        self.assertEqual(self.engine.product_id, A, "after_current waits for the video end")
        self.assertGreaterEqual(self.engine.metrics()["rotation"]["product_seconds"], 61)
        self.end_current()
        self.assertEqual(self.engine.product_id, B)
        self.assertEqual(self.engine.metrics()["rotation"]["product_seconds"], 0)
        self.end_current()
        self.assertEqual(self.engine.product_id, B)

    def test_rotation_after_minutes_immediate(self):
        self.policy(product_rotation="sequential", rotate_after_minutes=1, switch_mode="immediate")
        self.start()
        self.engine.tick()
        self.assertEqual(self.engine.product_id, A)
        self.engine.product_started_at -= 61
        self.engine.tick()
        self.assertEqual(self.engine.product_id, B)
        self.assertEqual(self.switches()[0]["detail"]["trigger"], "auto_rotation")

    def test_auto_false_stops_rotation(self):
        self.policy(product_rotation="sequential", rotate_after_videos=1)
        self.start()
        self.policy(product_rotation="sequential", rotate_after_videos=1, auto=False)
        rotation = self.engine.metrics()["rotation"]
        self.assertFalse(rotation["auto"])
        self.assertIsNone(rotation["next_product_id"])
        self.assertEqual(self.products(3), [A, A, A, A])
        self.policy(product_rotation="sequential", rotate_after_videos=1, auto=True)
        self.end_current()
        self.assertEqual(self.engine.product_id, B)

    def test_pending_manual_switch_wins(self):
        self.policy(product_rotation="sequential", rotate_after_videos=1)
        self.start()
        self.engine.product_play(7, C, self.ids("pl1", "pl2"), "after_current")
        self.end_current()
        self.assertEqual(self.engine.product_id, C)
        self.assertEqual([e["detail"]["trigger"] for e in self.switches()], ["video_end"])
        self.assertEqual(self.engine.metrics()["rotation"]["videos_in_product"], 1)
        self.end_current()
        self.assertEqual(self.engine.product_id, A)

    def test_manual_switch_resets_counters(self):
        self.policy(product_rotation="sequential", rotate_after_videos=2)
        self.start()
        self.end_current()
        self.assertEqual(self.engine.metrics()["rotation"]["videos_in_product"], 2)
        self.engine.product_play(7, C, self.ids("pl1", "pl2"), "immediate")
        self.assertEqual(self.engine.metrics()["rotation"]["videos_in_product"], 1)
        self.end_current()
        self.assertEqual(self.engine.product_id, C)

    def test_uncached_product_is_skipped(self):
        self.engine.set_policy(7, {"product_rotation": "sequential", "rotate_after_videos": 1},
                               self.lineup(b=None))
        self.start()
        self.assertEqual(self.engine.metrics()["rotation"]["next_product_id"], C)
        self.assertEqual(self.products(2), [A, C, A])

    def test_no_other_product_keeps_playing(self):
        self.engine.set_policy(7, {"product_rotation": "sequential", "rotate_after_videos": 1},
                               [{"product_id": A, "media_ids": self.ids("p1a", "p1b")},
                                {"product_id": B, "media_ids": ["f" * 32]}])
        self.start(names=("p1a", "p1b"))
        self.assertEqual(self.played(2), self.ids("p1a", "p1b", "p1a"))
        self.assertEqual(self.switches(), [])

    def test_policy_set_before_session_applies_when_it_starts(self):
        self.policy(product_rotation="sequential", rotate_after_videos=1)
        self.assertEqual(self.engine.metrics()["rotation"]["next_product_id"], None)
        self.start()
        self.assertEqual(self.engine.metrics()["policy"]["product_rotation"], "sequential")
        self.end_current()
        self.assertEqual(self.engine.product_id, B)
        self.assertEqual(self.switches()[0]["detail"]["trigger"], "auto_rotation")

    def test_policy_of_other_session_is_not_applied(self):
        self.engine.set_policy(8, {"product_rotation": "sequential", "rotate_after_videos": 1}, self.lineup())
        self.start()
        self.assertEqual(self.products(2), [A, A, A])

    def test_policy_applies_to_running_session(self):
        self.start()
        status = self.engine.set_policy(7, {"product_rotation": "sequential", "rotate_after_videos": 2},
                                        self.lineup())
        self.assertEqual(status["rotation"]["next_product_id"], B)
        self.assertEqual(self.engine.product_media[C], self.ids("pl1", "pl2"))
        # Counters keep running: the product's first video counts.
        self.assertEqual(self.products(2), [A, A, B])

    def test_invalid_policy_is_rejected(self):
        for bad in ({"video_order": "zigzag"}, {"product_rotation": "sequential", "rotate_after_videos": None},
                    {"rotate_after_minutes": 241}, {"unexpected": 1}):
            with self.assertRaises(PlaybackError) as ctx:
                self.engine.set_policy(7, bad, [])
            self.assertEqual(ctx.exception.status_code, 422)
        self.assertIsNone(self.engine.policy)


class DualPolicyTests(PolicyTestBase):
    dual = True

    def staged(self):
        return self.obs.settings[self.engine.slots[1 - self.engine.active_slot]]["local_file"]

    def playing(self):
        return self.obs.settings[self.engine.active_source()]["local_file"]

    def assert_preload_matches(self, ends):
        for _ in range(ends):
            staged = self.staged()
            self.end_current()
            self.assertEqual(self.playing(), staged)
            self.assertEqual(str(self.paths[self.current()].resolve()), staged)

    def test_preload_equals_next_video_shuffle(self):
        self.policy(video_order="shuffle")
        self.start()
        self.assert_preload_matches(10)

    def test_preload_equals_next_video_random(self):
        self.policy(video_order="random")
        self.start()
        self.assert_preload_matches(10)

    def test_preload_follows_rotation(self):
        self.policy(product_rotation="shuffle", rotate_after_videos=2, video_order="random")
        self.start()
        self.assert_preload_matches(12)
        self.assertGreaterEqual(len(self.switches()), 5)


# --- Cloud command ---------------------------------------------------------------------------
MID = "a" * 32


@pytest.fixture()
def router():
    engine = mock.Mock()
    return CommandRouter(playback=engine, monitor=mock.Mock(), voice=mock.Mock()), engine


def test_playback_policy_command_reaches_engine(router):
    r, engine = router
    assert lane("playback_policy") == "live"
    r.execute("playback_policy", {"session_id": 12, "policy": {"video_order": "shuffle", "product_rotation": "random",
                                                               "rotate_after_videos": 2},
                                  "lineup": [{"product_id": 5, "media_ids": [MID]}, {"product_id": 6, "media_ids": []}]})
    session_id, policy, lineup = engine.set_policy.call_args.args
    assert session_id == 12
    assert policy == {"video_order": "shuffle", "product_rotation": "random", "rotate_after_videos": 2,
                      "rotate_after_minutes": None, "switch_mode": "after_current", "auto": True}
    assert lineup == [{"product_id": 5, "media_ids": [MID]}, {"product_id": 6, "media_ids": []}]


@pytest.mark.parametrize("payload", [
    {"session_id": 0, "policy": {}, "lineup": []},
    {"session_id": 1, "policy": {"video_order": "zigzag"}, "lineup": []},
    {"session_id": 1, "policy": {"rotate_after_videos": 51}, "lineup": []},
    {"session_id": 1, "policy": {"rotate_after_minutes": 0}, "lineup": []},
    {"session_id": 1, "policy": {"switch_mode": "later"}, "lineup": []},
    {"session_id": 1, "policy": {"product_rotation": "random", "rotate_after_videos": None}, "lineup": []},
    {"session_id": 1, "policy": {"extra": 1}, "lineup": []},
    {"session_id": 1, "policy": {}, "lineup": [], "extra": 1},
    {"session_id": 1, "policy": {}, "lineup": [{"product_id": 0, "media_ids": []}]},
    {"session_id": 1, "policy": {}, "lineup": [{"product_id": 1, "media_ids": ["../x"]}]},
    {"session_id": 1, "policy": {}, "lineup": [{"product_id": 1, "media_ids": [], "x": 1}]},
    {"session_id": 1, "policy": {}, "lineup": [{"product_id": 1}, {"product_id": 1}]},
    {"session_id": 1, "policy": {}, "lineup": [{"product_id": i} for i in range(1, 302)]},
    {"session_id": 1, "policy": {}, "lineup": [{"product_id": i, "media_ids": [MID] * 1001} for i in (1, 2, 3)]},
])
def test_playback_policy_command_rejects(router, payload):
    r, engine = router
    with pytest.raises(AgentError) as exc:
        r.execute("playback_policy", payload)
    assert exc.value.code == "INVALID_COMMAND_PAYLOAD"
    engine.set_policy.assert_not_called()

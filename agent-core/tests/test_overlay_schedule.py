"""overlay/schedule.py against the golden vectors of the web implementation (web/src/overlay/overlaySchedule.ts)."""
import json
import unittest
from pathlib import Path

from sarae_agent.overlay.schedule import generator, random_plan, seed_of
from sarae_agent.overlay.service import shown_in, snapshot_problem

VECTORS = json.loads((Path(__file__).resolve().parent / "fixtures" / "overlay_schedule_vectors.json").read_text(encoding="utf-8"))


class ScheduleVectorTests(unittest.TestCase):
    def test_seed_and_generator_match_the_web(self):
        for case in VECTORS["seeds"]:
            self.assertEqual(seed_of(case["key"]), case["seed"], case["key"])
            next_ = generator(case["seed"])
            self.assertEqual([next_(), next_(), next_()], case["first"], case["key"])

    def test_plans_match_the_web(self):
        self.assertTrue(VECTORS["cases"])
        for case in VECTORS["cases"]:
            for expected in case["plans"]:
                with self.subTest(case=case["name"], iteration=expected["iteration"]):
                    self.assertEqual(random_plan(case["overlays"], case["media"], case["schedule"], case["timeline_id"],
                                                 expected["iteration"]), expected["plan"])

    def test_plans_keep_one_overlay_at_a_time_with_the_gap(self):
        for case in VECTORS["cases"]:
            for expected in case["plans"]:
                plan, gap = expected["plan"], case["schedule"]["min_gap_ms"]
                for a, b in zip(plan, plan[1:]):
                    self.assertGreaterEqual(b["start_ms"], a["end_ms"] + gap, case["name"])

    def test_shown_in_fixed_and_random(self):
        overlays = [{"id": i, "media_id": "m", "start_ms": 0, "duration_ms": 600_000, "show_ms": 5000} for i in range(6)]
        fixed = {"overlays": overlays, "timeline_id": 1}
        self.assertEqual(len(shown_in(fixed, "m", 1000)), 6, "fixed mode: the window is the appearance")
        self.assertEqual(len(shown_in({**fixed, "schedule": {"mode": "fixed", "every_ms": 60_000, "min_gap_ms": 0}}, "m", 1000)), 6)
        scene = {**fixed, "schedule": {"mode": "random", "every_ms": 60_000, "min_gap_ms": 10_000}}
        plan = random_plan(overlays, "m", scene["schedule"], 1, 2)
        first = plan[0]
        self.assertEqual(shown_in(scene, "m", first["start_ms"], 2), [overlays[first["index"]]])
        self.assertEqual(shown_in(scene, "m", first["end_ms"], 2), [])
        self.assertEqual(shown_in(scene, "other", first["start_ms"], 2), [])
        self.assertIsNotNone(snapshot_problem(fixed), "fixed: more than 4 at once is refused")
        self.assertIsNone(snapshot_problem(scene), "random: one at a time by construction")

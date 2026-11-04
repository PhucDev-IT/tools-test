import json
import random
import threading

import pytest

from sarae_agent.scheduler import Algorithm, Config, LiveScheduler, Product, SchedulerError, SwitchMode, Video
from sarae_agent.scheduler.integration_example import confirmed_manual_pin


class FakePlayback:
    def __init__(self):
        self.plays = []
        self.pauses = 0
        self.resumes = []
        self.stops = 0
        self.bad = set()

    def play(self, product, video, token):
        if video.id in self.bad:
            raise OSError("bad file")
        self.plays.append((product.id, video.id, token))

    def pause(self):
        self.pauses += 1

    def resume(self, token):
        self.resumes.append(token)

    def stop(self):
        self.stops += 1


def manifest(products=3, videos=3):
    return [Product(f"p{p}", tuple(Video(f"p{p}v{v}") for v in range(videos)))
            for p in range(products)]


def setup(products=3, videos=3, pa=Algorithm.SEQUENTIAL, va=Algorithm.SEQUENTIAL,
          mode=SwitchMode.IMMEDIATE, agent="agent"):
    adapter = FakePlayback()
    scheduler = LiveScheduler(agent, adapter, rng=random.Random(19))
    scheduler.configure(manifest(products, videos), Config(pa, va, mode))
    return scheduler, adapter


@pytest.mark.parametrize("count", [1, 2, 5])
@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_product_selection_sizes(count, algorithm):
    scheduler, adapter = setup(count, 1, pa=algorithm)
    try:
        scheduler.start()
        for _ in range(count * 3):
            scheduler.next()
        picked = [p for p, _, _ in adapter.plays]
        assert set(picked) <= {f"p{i}" for i in range(count)}
        if algorithm == Algorithm.SEQUENTIAL:
            assert picked[:count] == [f"p{i}" for i in range(count)]
            assert picked[count:2 * count] == picked[:count]
        elif algorithm == Algorithm.SHUFFLE_NO_REPEAT:
            for offset in range(0, count * 3, count):
                assert len(set(picked[offset:offset + count])) == count
    finally:
        scheduler.stop()


@pytest.mark.parametrize("count", [1, 2, 5])
@pytest.mark.parametrize("algorithm", list(Algorithm))
def test_video_selection_sizes(count, algorithm):
    scheduler, adapter = setup(1, count, va=algorithm)
    try:
        scheduler.start()
        for _ in range(count * 3):
            scheduler.next()
        picked = [v for _, v, _ in adapter.plays]
        if algorithm == Algorithm.SEQUENTIAL:
            assert picked[:count] == [f"p0v{i}" for i in range(count)]
            assert picked[count:2 * count] == picked[:count]
        elif algorithm == Algorithm.SHUFFLE_NO_REPEAT:
            for offset in range(0, count * 3, count):
                assert len(set(picked[offset:offset + count])) == count
        else:
            assert set(picked) <= {f"p0v{i}" for i in range(count)}
    finally:
        scheduler.stop()


def test_random_repeat_can_repeat_and_selectors_are_independent():
    scheduler, adapter = setup(2, 2, Algorithm.RANDOM_REPEAT, Algorithm.SHUFFLE_NO_REPEAT)
    try:
        scheduler.start()
        for _ in range(40):
            scheduler.next()
        products = [p for p, _, _ in adapter.plays]
        assert any(a == b for a, b in zip(products, products[1:]))
        for pid in ("p0", "p1"):
            vids = [v for p, v, _ in adapter.plays if p == pid]
            for offset in range(0, len(vids) - 1, 2):
                assert len(set(vids[offset:offset + 2])) == 2
    finally:
        scheduler.stop()


def test_after_current_and_duplicate_events():
    scheduler, adapter = setup(2, 1, mode=SwitchMode.AFTER_CURRENT)
    try:
        first = scheduler.start()
        scheduler.next(token=first["token"])
        scheduler.next(token=first["token"])
        assert len(adapter.plays) == 1
        assert scheduler.get_status()["pending"]
        second = scheduler.on_video_ended(first["token"])
        assert second["product_id"] == "p1"
        scheduler.on_video_ended(first["token"])
        scheduler.next(token=first["token"])
        assert len(adapter.plays) == 2
    finally:
        scheduler.stop()


def test_manual_override_cancels_pending_and_old_timer_then_explicit_resume():
    scheduler, adapter = setup(2, 2, mode=SwitchMode.AFTER_CURRENT)
    try:
        old = scheduler.start()
        scheduler.next(token=old["token"])
        manual = scheduler.manual_switch("p1", "p1v1")
        assert not manual["auto_enabled"] and not manual["pending"]
        scheduler.next(token=old["token"], mode=SwitchMode.IMMEDIATE)
        scheduler.on_video_ended(old["token"])
        scheduler.on_video_ended(manual["token"])
        assert len(adapter.plays) == 2
        assert scheduler.get_status()["video_id"] is None
        assert scheduler.resume()["auto_enabled"]
        assert len(adapter.plays) == 3
        scheduler.next(mode=SwitchMode.IMMEDIATE)
        assert len(adapter.plays) == 4
    finally:
        scheduler.stop()


def test_error_skips_video_then_exhausts_with_clear_error():
    scheduler, adapter = setup(2, 2)
    try:
        first = scheduler.start()
        second = scheduler.on_video_error(first["token"], "decode")
        assert second["product_id"] == "p0"
        assert second["video_id"] != first["video_id"]
        while scheduler.get_status()["state"] == "RUNNING":
            token = scheduler.get_status()["token"]
            try:
                scheduler.on_video_error(token)
            except SchedulerError as exc:
                assert "Không còn video khả dụng" in str(exc)
                break
        assert scheduler.get_status()["state"] == "ERROR"
        assert adapter.stops == 1
    finally:
        scheduler.stop()


def test_adapter_start_failures_are_bounded_and_empty_products_excluded():
    scheduler, adapter = setup(2, 2)
    adapter.bad = {"p0v0", "p0v1", "p1v0"}
    try:
        assert scheduler.start()["video_id"] == "p1v1"
        assert set(scheduler.get_status()["failed_video_ids"]) == adapter.bad
    finally:
        scheduler.stop()
    s2 = LiveScheduler("empty", FakePlayback())
    s2.configure([Product("empty", ()), Product("valid", (Video("one"),))], Config())
    try:
        assert s2.start()["product_id"] == "valid"
    finally:
        s2.stop()


def test_all_video_failures_can_be_retried_after_restart():
    scheduler, adapter = setup(1, 2)
    adapter.bad = {"p0v0", "p0v1"}
    with pytest.raises(SchedulerError, match="Không còn video khả dụng"):
        scheduler.start()
    assert scheduler.get_status()["state"] == "ERROR"
    adapter.bad.clear()
    try:
        assert scheduler.start()["state"] == "RUNNING"
    finally:
        scheduler.stop()


def test_confirmed_manual_pin_uses_override_and_disables_auto_pin():
    scheduler, adapter = setup(2, 1)
    disabled = []
    try:
        scheduler.start()
        state = confirmed_manual_pin(scheduler, "p1", lambda: disabled.append(True))
        assert disabled == [True]
        assert state["product_id"] == "p1" and not state["auto_enabled"]
        scheduler.resume()
        assert disabled == [True]
    finally:
        scheduler.stop()


def test_manual_pin_without_video_holds_current_and_disables_auto():
    scheduler, adapter = setup(1, 1)
    products = manifest(1, 1) + [Product("empty", ())]
    scheduler.configure(products, Config())
    try:
        first = scheduler.start()
        with pytest.raises(SchedulerError, match="no available video"):
            confirmed_manual_pin(scheduler, "empty", lambda: None)
        status = scheduler.get_status()
        assert status["video_id"] == first["video_id"]
        assert not status["auto_enabled"]
        scheduler.on_video_ended(first["token"])
        assert len(adapter.plays) == 1
    finally:
        scheduler.stop()


def test_pause_resume_stop_restart_and_stale_callbacks():
    scheduler, adapter = setup(1, 2)
    first = scheduler.start()
    paused = scheduler.pause()
    assert paused["state"] == "PAUSED"
    scheduler.on_video_ended(first["token"])
    resumed = scheduler.resume()
    assert resumed["state"] == "RUNNING" and adapter.resumes == [resumed["token"]]
    scheduler.on_video_ended(first["token"])
    assert len(adapter.plays) == 1
    scheduler.stop()
    restarted = scheduler.start()
    scheduler.on_video_ended(resumed["token"])
    assert restarted["state"] == "RUNNING" and len(adapter.plays) == 2
    scheduler.stop()


def test_snapshot_restore_json_and_explicit_resume():
    scheduler, adapter = setup(2, 2, Algorithm.SHUFFLE_NO_REPEAT, Algorithm.SHUFFLE_NO_REPEAT)
    first = scheduler.start()
    snapshot = json.loads(json.dumps(scheduler.snapshot()))
    scheduler.stop()
    other_adapter = FakePlayback()
    restored = LiveScheduler.restore(snapshot, manifest(2, 2), other_adapter)
    assert restored.get_status()["state"] == "PAUSED"
    assert not other_adapter.plays
    restored.on_video_ended(first["token"])
    try:
        resumed = restored.resume()
        assert resumed["video_id"] == first["video_id"]
        assert resumed["token"] != first["token"]
        restored.on_video_ended(first["token"])
        assert len(other_adapter.plays) == 1
    finally:
        restored.stop()


def test_restore_after_manual_video_ended_and_error_state():
    scheduler, _ = setup(1, 2, agent="restore-ended")
    first = scheduler.start()
    scheduler.manual_switch("p0")
    manual_token = scheduler.get_status()["token"]
    scheduler.on_video_ended(manual_token)
    saved = json.loads(json.dumps(scheduler.snapshot()))
    scheduler.stop()
    player = FakePlayback()
    recovered = LiveScheduler.restore(saved, manifest(1, 2), player)
    try:
        assert recovered.resume()["state"] == "RUNNING"
        assert len(player.plays) == 1
    finally:
        recovered.stop()

    broken, adapter = setup(1, 1, agent="restore-error")
    adapter.bad.add("p0v0")
    with pytest.raises(SchedulerError):
        broken.start()
    snapshot = json.loads(json.dumps(broken.snapshot()))
    restored = LiveScheduler.restore(snapshot, manifest(1, 1), FakePlayback())
    assert restored.get_status()["state"] == "ERROR"
    assert "Không còn video khả dụng" in restored.get_status()["error"]


def test_one_scheduler_per_agent_and_concurrent_duplicate_end():
    scheduler, adapter = setup(1, 2, agent="shared")
    other, _ = setup(1, 2, agent="shared")
    try:
        first = scheduler.start()
        with pytest.raises(SchedulerError, match="already has"):
            other.start()
        workers = [threading.Thread(target=scheduler.on_video_ended, args=(first["token"],)) for _ in range(20)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert len(adapter.plays) == 2
    finally:
        scheduler.stop()
        other.stop()
    other.start()
    other.stop()


def test_concurrent_start_claims_one_agent_once():
    first, _ = setup(1, 1, agent="race")
    second, _ = setup(1, 1, agent="race")
    outcomes = []

    def start(scheduler):
        try:
            scheduler.start()
            outcomes.append("started")
        except SchedulerError:
            outcomes.append("rejected")

    workers = [threading.Thread(target=start, args=(scheduler,)) for scheduler in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    try:
        assert sorted(outcomes) == ["rejected", "started"]
    finally:
        first.stop()
        second.stop()

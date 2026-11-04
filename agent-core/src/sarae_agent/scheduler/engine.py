"""Transport-free, event-driven scheduler for one Windows Agent.

No wall-clock timer is owned here. A caller may schedule ``next(token=...)``;
tokens make a cancelled timer or delayed OBS event harmless after a switch.
"""

from __future__ import annotations

import random
import logging
import threading
from typing import Any

from .types import Algorithm, Config, PlaybackAdapter, Product, Status, SwitchMode, Video


class SchedulerError(RuntimeError):
    pass


_registry_lock = threading.RLock()
_active: dict[str, LiveScheduler] = {}
log = logging.getLogger(__name__)


def _tuple_tree(value: Any) -> Any:
    return tuple(_tuple_tree(item) for item in value) if isinstance(value, list) else value


class _Selector:
    def __init__(self, algorithm: Algorithm, rng: random.Random):
        self.algorithm = algorithm
        self.rng = rng
        self.cursor = 0
        self.bag: list[str] = []

    def choose(self, ids: list[str]) -> str:
        if not ids:
            raise SchedulerError("Không còn video khả dụng để phát")
        if self.algorithm == Algorithm.SEQUENTIAL:
            choice = ids[self.cursor % len(ids)]
            self.cursor += 1
            return choice
        if self.algorithm == Algorithm.RANDOM_REPEAT:
            return self.rng.choice(ids)
        self.bag = [item for item in self.bag if item in ids]
        if not self.bag:
            self.bag = list(ids)
            self.rng.shuffle(self.bag)
        return self.bag.pop()

    def dump(self) -> dict:
        return {"cursor": self.cursor, "bag": list(self.bag)}

    def load(self, data: dict) -> None:
        self.cursor = int(data["cursor"])
        self.bag = list(data["bag"])


class LiveScheduler:
    def __init__(self, agent_id: str, adapter: PlaybackAdapter, *, rng: random.Random | None = None):
        if not agent_id:
            raise ValueError("agent_id is required")
        self.agent_id = agent_id
        self.adapter = adapter
        self._lock = threading.RLock()
        self._rng = rng or random.Random()
        self._config = Config()
        self._products: dict[str, Product] = {}
        self._product_selector = _Selector(self._config.product_algorithm, self._rng)
        self._video_selectors: dict[str, _Selector] = {}
        self._state = Status.STOPPED
        self._auto_enabled = False
        self._failed: set[str] = set()
        self._current: tuple[str, str] | None = None
        self._token = 0
        self._pending = False
        self._error: str | None = None
        self._recover_pending = False

    def configure(self, products: list[Product] | tuple[Product, ...], config: Config) -> dict:
        with self._lock:
            if self._state in (Status.RUNNING, Status.PAUSED):
                raise SchedulerError("Stop scheduler before reconfiguring")
            if not isinstance(config, Config):
                raise TypeError("config must be Config")
            if not isinstance(config.product_algorithm, Algorithm) or not isinstance(config.video_algorithm, Algorithm) \
                    or not isinstance(config.switch_mode, SwitchMode):
                raise ValueError("Scheduler config contains an unknown algorithm or switch mode")
            product_map: dict[str, Product] = {}
            seen_videos: set[str] = set()
            for product in products:
                if not product.id or product.id in product_map:
                    raise ValueError("Product IDs must be unique and nonempty")
                for video in product.videos:
                    if not video.id or video.id in seen_videos:
                        raise ValueError("Video IDs must be unique and nonempty in a manifest")
                    seen_videos.add(video.id)
                product_map[product.id] = product
            self._products = product_map
            self._config = config
            self._product_selector = _Selector(config.product_algorithm, self._rng)
            self._video_selectors = {pid: _Selector(config.video_algorithm, self._rng) for pid in product_map}
            self._failed.clear()
            self._current = None
            self._pending = False
            self._error = None
            self._recover_pending = False
            return self.get_status()

    def _claim(self) -> None:
        with _registry_lock:
            owner = _active.get(self.agent_id)
            if owner is not None and owner is not self:
                raise SchedulerError(f"Agent {self.agent_id} already has a scheduler")
            _active[self.agent_id] = self

    def _release(self) -> None:
        with _registry_lock:
            if _active.get(self.agent_id) is self:
                del _active[self.agent_id]

    def _available_products(self) -> list[str]:
        return [pid for pid, product in self._products.items()
                if any(video.available and video.id not in self._failed for video in product.videos)]

    def _available_videos(self, pid: str) -> list[str]:
        return [video.id for video in self._products[pid].videos
                if video.available and video.id not in self._failed]

    def _play(self, preferred_product: str | None = None, preferred_video: str | None = None) -> dict:
        # Each failed attempt permanently removes one video for this run, so this is bounded.
        while self._available_products():
            pid = preferred_product if preferred_product in self._available_products() else None
            pid = pid or self._product_selector.choose(self._available_products())
            vids = self._available_videos(pid)
            vid = preferred_video if preferred_video in vids and pid == preferred_product else None
            vid = vid or self._video_selectors[pid].choose(vids)
            video = next(v for v in self._products[pid].videos if v.id == vid)
            self._token += 1
            try:
                self.adapter.play(self._products[pid], video, self._token)
            except Exception as exc:
                self._failed.add(vid)
                self._error = f"Video {vid} lỗi: {exc}"
                log.warning("Scheduler skipped video %s for product %s: %s", vid, pid, exc)
                preferred_product, preferred_video = pid, None
                continue
            self._current = (pid, vid)
            self._pending = False
            self._recover_pending = False
            self._error = None
            return self.get_status()
        self._state = Status.ERROR
        self._auto_enabled = False
        self._pending = False
        self._current = None
        self._error = "Không còn video khả dụng để phát (tất cả video đã lỗi hoặc không khả dụng)"
        try:
            self.adapter.stop()
        except Exception:
            log.exception("Playback adapter failed to stop after video exhaustion")
        finally:
            self._release()
        raise SchedulerError(self._error)

    def start(self) -> dict:
        with self._lock:
            if self._state in (Status.RUNNING, Status.PAUSED):
                raise SchedulerError("Scheduler already started")
            self._failed.clear()  # a new run retries files that failed in the previous run
            if not self._available_products():
                raise SchedulerError("Không có sản phẩm nào có video khả dụng")
            self._claim()
            self._state = Status.RUNNING
            self._auto_enabled = True
            self._pending = False
            self._error = None
            try:
                return self._play()
            except Exception:
                if self._state != Status.ERROR:
                    self._state = Status.ERROR
                    self._release()
                raise

    def pause(self) -> dict:
        with self._lock:
            if self._state != Status.RUNNING:
                return self.get_status()
            self.adapter.pause()
            self._state = Status.PAUSED
            self._pending = False
            self._token += 1  # invalidate timer and media callbacks
            self._recover_pending = False
            return self.get_status()

    def resume(self) -> dict:
        with self._lock:
            if self._state != Status.PAUSED and not (self._state == Status.RUNNING and not self._auto_enabled):
                raise SchedulerError("Scheduler is not paused or manually overridden")
            self._claim()
            if self._state == Status.PAUSED:
                if self._recover_pending:
                    self._state = Status.RUNNING
                    self._auto_enabled = True
                    if self._current:
                        pid, vid = self._current
                        return self._play(pid, vid)
                    return self._play()
                self._token += 1
                self.adapter.resume(self._token)
            self._state = Status.RUNNING
            self._auto_enabled = True
            self._pending = False
            if self._current is None:
                return self._play()
            return self.get_status()

    def stop(self) -> dict:
        with self._lock:
            self._token += 1
            self._pending = False
            self._auto_enabled = False
            self._current = None
            self._failed.clear()
            self._recover_pending = False
            try:
                if self._state in (Status.RUNNING, Status.PAUSED):
                    self.adapter.stop()
            finally:
                self._state = Status.STOPPED
                self._error = None
                self._release()
            return self.get_status()

    def next(self, *, token: int | None = None, mode: SwitchMode | None = None) -> dict:
        with self._lock:
            if self._state != Status.RUNNING or not self._auto_enabled:
                return self.get_status()
            if token is not None and token != self._token:
                return self.get_status()
            chosen_mode = mode or self._config.switch_mode
            if chosen_mode == SwitchMode.AFTER_CURRENT and self._current:
                self._pending = True
                return self.get_status()
            return self._play()

    def manual_switch(self, product_id: str, video_id: str | None = None) -> dict:
        """Use for a Web user action or a confirmed manual pin observer event."""
        with self._lock:
            if self._state not in (Status.RUNNING, Status.PAUSED):
                raise SchedulerError("Scheduler is not active")
            self._auto_enabled = False
            self._pending = False
            if product_id not in self._products or not self._available_videos(product_id):
                raise SchedulerError(f"Product {product_id} has no available video")
            if video_id is not None and video_id not in self._available_videos(product_id):
                raise SchedulerError(f"Video {video_id} is unavailable for product {product_id}")
            self._state = Status.RUNNING
            return self._play(product_id, video_id)

    def on_video_ended(self, token: int) -> dict:
        with self._lock:
            if self._state != Status.RUNNING or token != self._token or not self._current:
                return self.get_status()
            self._token += 1  # consume the event before issuing another play
            if not self._auto_enabled:
                self._current = None
                return self.get_status()
            return self._play()

    def on_video_error(self, token: int, detail: str = "playback failed") -> dict:
        with self._lock:
            if self._state != Status.RUNNING or token != self._token or not self._current:
                return self.get_status()
            pid, vid = self._current
            self._token += 1
            self._failed.add(vid)
            self._error = f"Video {vid} lỗi: {detail}"
            log.warning("Scheduler skipped video %s for product %s: %s", vid, pid, detail)
            return self._play(pid)

    def get_status(self) -> dict:
        with self._lock:
            return {"agent_id": self.agent_id, "state": self._state.value,
                    "auto_enabled": self._auto_enabled, "product_id": self._current[0] if self._current else None,
                    "video_id": self._current[1] if self._current else None, "token": self._token,
                    "pending": self._pending, "failed_video_ids": sorted(self._failed), "error": self._error}

    def snapshot(self) -> dict:
        """JSON-serializable selection state; restore always requires explicit resume."""
        with self._lock:
            return {"version": 1, "agent_id": self.agent_id,
                    "config": {"product_algorithm": self._config.product_algorithm.value,
                               "video_algorithm": self._config.video_algorithm.value,
                               "switch_mode": self._config.switch_mode.value},
                    "state": self._state.value, "auto_enabled": self._auto_enabled,
                    "error": self._error,
                    "current": list(self._current) if self._current else None,
                    "failed": sorted(self._failed), "token": self._token,
                    "product_selector": self._product_selector.dump(),
                    "video_selectors": {pid: selector.dump() for pid, selector in self._video_selectors.items()},
                    "rng_state": self._rng.getstate()}

    @classmethod
    def restore(cls, snapshot: dict, products: list[Product] | tuple[Product, ...],
                adapter: PlaybackAdapter) -> LiveScheduler:
        if snapshot.get("version") != 1:
            raise ValueError("Unsupported scheduler snapshot version")
        engine = cls(snapshot["agent_id"], adapter)
        cfg = snapshot["config"]
        engine.configure(products, Config(Algorithm(cfg["product_algorithm"]),
                                          Algorithm(cfg["video_algorithm"]), SwitchMode(cfg["switch_mode"])))
        engine._rng.setstate(_tuple_tree(snapshot["rng_state"]))
        engine._product_selector.load(snapshot["product_selector"])
        for pid, data in snapshot["video_selectors"].items():
            if pid in engine._video_selectors:
                engine._video_selectors[pid].load(data)
        engine._failed = set(snapshot["failed"])
        engine._token = int(snapshot["token"]) + 1
        current = snapshot["current"]
        if current and current[0] in engine._products and current[1] in engine._available_videos(current[0]):
            engine._current = (current[0], current[1])
        if snapshot["state"] in (Status.RUNNING.value, Status.PAUSED.value):
            engine._state = Status.PAUSED
            engine._recover_pending = True
        elif snapshot["state"] == Status.ERROR.value:
            engine._state = Status.ERROR
            engine._error = snapshot.get("error")
        return engine

"""DB-free OBS playback engine of the Windows Agent.

Ported from the Cloud repository's backend/app/playback_engine.py (a port of
``app.live.PlayState``): product queue looping, pending "after_current" switch,
dual A/B Media Sources, watchdog with stall recovery and bounded retries.
Videos are addressed by Sarae Media Library ids (32-hex) and turned into files by
the ``resolve`` callable (the runtime looks them up in the cloud media cache); business state (sessions,
lineups, history) stays on the VPS, which drives this engine through Agent
commands (cloud/commands.py).

Public API (kept stable for the cloud LIVE facade; ``status()`` is the heartbeat ``playback``):
    PlaybackEngine(resolve, on_event=None, config_file=None, client_factory=None, store=None)
    start_playlist(media_ids, loop=True, start_index=0, playlist_id=None)
    product_play(session_id, product_id, media_ids, mode)  # 'immediate' | 'after_current'
    cancel_pending(), pause(), resume(), next(), previous(), select(index), seek(ms), stop()
    set_policy(session_id, policy, lineup)  # docs/PLAYBACK_POLICY.md: video order, automatic product rotation
    configure(...), list_inputs(host, port, password), set_loop(loop), status(), tick(), start_monitor()

Agent-core differences: OBS settings come from ``store`` (the runtime keeps them in
config.json, the password DPAPI-protected) and ``client_factory`` returns
obs.controller.ObsRequests over the Core's single obs-websocket connection.

Errors are raised as ``PlaybackError(status_code, detail)`` (an AgentError). Events
passed to ``on_event`` are dicts ``{type, session_id, product_id, detail}`` with type
PRODUCT_SWITCHED, PLAYBACK_ENDED or ERROR.
"""
import json
import logging
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from sarae_agent.config import paths
from sarae_agent.errors import AgentError
from sarae_agent.media.cloud_cache import MEDIA_ID
from sarae_agent.media.probe import obs_file, probe_media

log = logging.getLogger("sarae.playback")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ERROR_CODES = {409: "PLAYBACK_CONFLICT", 422: "INVALID_REQUEST", 502: "OBS_REQUEST_FAILED", 503: "OBS_UNAVAILABLE"}
# A/B: poll up to 1s (20 x 50ms) for the stand-by source's first frame before pausing it there.
STAGE_FRAME_POLLS = 20
# A/B: swap this many ms before the visible video ends, so its end never shows; the 1s watchdog tick
# schedules the swap when the remaining time falls within SWAP_HORIZON_MS.
SWAP_LEAD_MS = 250
SWAP_HORIZON_MS = 1500


class PlaybackError(AgentError):
    """Keeps the Cloud engine's (status_code, detail) shape; ``code`` is the stable IPC error code."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(ERROR_CODES.get(status_code, "PLAYBACK_FAILED"), detail)
        self.status_code = status_code
        self.detail = detail


class ObsConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=4455, ge=1, le=65535)
    password: str = ""
    input_name: str = "Sarae Video"
    dual_mode: bool = False
    # A/B Media Source names for dual mode; empty falls back to "<input_name> A" / "<input_name> B".
    source_a: str = Field(default="", max_length=200)
    source_b: str = Field(default="", max_length=200)
    camera_input: str = Field(default="", max_length=200)


class PlaybackPolicy(BaseModel):
    """docs/PLAYBACK_POLICY.md; also the payload of the ``playback_policy`` Cloud command."""
    model_config = {"extra": "forbid"}

    video_order: Literal["sequential", "random", "shuffle"] = "sequential"
    product_rotation: Literal["off", "sequential", "random", "shuffle"] = "off"
    rotate_after_videos: int | None = Field(default=3, ge=1, le=50)
    rotate_after_minutes: int | None = Field(default=None, ge=1, le=240)
    switch_mode: Literal["after_current", "immediate"] = "after_current"
    auto: bool = True

    @model_validator(mode="after")
    def _limit(self):
        if self.product_rotation != "off" and self.rotate_after_videos is None and self.rotate_after_minutes is None:
            raise ValueError("Cần rotate_after_videos hoặc rotate_after_minutes khi bật đổi sản phẩm tự động")
        return self


class LineupItem(BaseModel):
    model_config = {"extra": "forbid"}

    product_id: int = Field(ge=1)
    media_ids: list[str] = Field(default_factory=list, max_length=3000)

    @field_validator("media_ids")
    @classmethod
    def _ids(cls, value: list[str]) -> list[str]:
        if any(not (isinstance(m, str) and MEDIA_ID.fullmatch(m)) for m in value):
            raise ValueError("Danh sách media_ids không hợp lệ")
        return value


class PolicyUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    session_id: int = Field(ge=1)
    policy: PlaybackPolicy
    lineup: list[LineupItem] = Field(default_factory=list, max_length=300)

    @field_validator("lineup")
    @classmethod
    def _lineup(cls, value: list[LineupItem]) -> list[LineupItem]:
        if sum(len(item.media_ids) for item in value) > 3000:
            raise ValueError("Tối đa 3000 video trong lineup")
        if len({item.product_id for item in value}) != len(value):
            raise ValueError("Sản phẩm bị lặp trong lineup")
        return value


def load_obs_config(path: Path) -> ObsConfig:
    default = ObsConfig()
    try:
        return ObsConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception:
        # A corrupt file must not stop the Agent from starting; the operator can save the config again.
        return default


def save_obs_config(config: ObsConfig, path: Path):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class FileObsStore:
    """OBS settings in a JSON file (tests, standalone use). The runtime passes obs.controller.ObsSettingsStore."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> ObsConfig:
        return load_obs_config(self.path)

    def save(self, config: ObsConfig) -> None:
        save_obs_config(config, self.path)


def _default_client(config: ObsConfig):
    # OBS WebSocket 5.x. No output or streaming controls.
    import obsws_python as obs
    return obs.ReqClient(host=config.host, port=config.port, password=config.password, timeout=4)


def _close(client):
    try:
        close = getattr(client, "close", None)
        if close is not None:
            close()
        else:
            client.base_client.ws.close()
    except Exception:
        pass


class PlaybackEngine:
    def __init__(self, resolve: Callable[[str], Path | None], on_event: Callable[[dict], None] | None = None,
                 config_file: Path | None = None, client_factory: Callable[[ObsConfig], object] | None = None,
                 store=None, rng: random.Random | None = None,
                 on_position: Callable[[dict], None] | None = None):
        self.resolve = resolve
        # Single Video Timeline: called by the watchdog with the main video's position (sarae_agent/timeline).
        self.on_position = on_position
        self.rng = rng or random.Random()
        self.on_event = on_event
        self.config_file = Path(config_file) if config_file else paths.root / "obs_config.json"
        self.store = store or FileObsStore(self.config_file)
        self.client_factory = client_factory or _default_client
        self.lock = threading.RLock()
        self.config = self.store.load()
        self.playlist_id: int | None = None
        self.index = 0
        self.ids: list[str] = []
        self.active = False
        self.paused = False
        self.last_status = ""
        self.generation = 0
        self.dual_mode = self.config.dual_mode
        self.active_slot = 0
        self.slots: list[str] = []
        self.scene_item_ids: list[int] = []
        self.scene_name = ""
        self.camera_mode = False
        self.camera_scene_item_id: int | None = None
        self._prepared: dict[str, tuple[int, int, str]] = {}
        # OBS canvas (width, height) the prepared copies are rendered for; re-read when a session starts.
        self._canvas: tuple[int, int] | None = None
        self.loop = False
        self.retry_count = 0
        self.next_retry_at = 0.0
        self.error_count = 0
        self.transition_count = 0
        self.recovery_count = 0
        self.session_started_at = None
        self.last_progress_at = time.monotonic()
        self.last_cursor = None
        self.stall_restarts = 0
        self.session_mode = "playlist"
        # "video" sessions (Single Video Timeline): how many times a video of the session started after the first one
        # (next video or the same one again); with their order (sequential / random / shuffle).
        self.loop_iteration = 0
        self.video_order = "sequential"
        self.product_id = None
        self.pending_product_id = None
        self.session_id = None
        # Lineups pushed by the VPS: the full ordered media list per product, re-checked at each video end.
        self.product_media: dict[int, list[str]] = {}
        # Playback policy of one LIVE session (docs/PLAYBACK_POLICY.md); applies while that session is on air.
        self.policy: PlaybackPolicy | None = None
        self.policy_session_id: int | None = None
        self.product_order: list[int] = []
        self.videos_in_product = 0
        self.product_started_at: float | None = None
        self._product_starting = False
        # Choices are made once and remembered, so the A/B preload and the next play agree.
        self._planned_next: str | None = None
        self._planned_product: int | None = None
        self._planned_start: tuple[int, str] | None = None
        self._video_bag: list[str] = []
        self._product_bag: list[int] = []
        self._product_rounds = 0
        self._monitor_thread = None
        self._monitor_stop = threading.Event()
        # A/B: generation whose near-end swap timer is pending (see _schedule_swap).
        self._swap_generation: int | None = None

    # ----- helpers -------------------------------------------------------------------------------

    def _emit(self, kind: str, detail: dict | None = None, product_id=None):
        if self.on_event is None:
            return
        try:
            self.on_event({"type": kind, "session_id": self.session_id,
                           "product_id": self.product_id if product_id is None else product_id,
                           "detail": detail or {}})
        except Exception:
            pass

    def _path(self, media_id: str) -> Path | None:
        try:
            path = self.resolve(media_id)
        except Exception:
            return None
        if path is None:
            return None
        path = Path(path)
        return path if path.is_file() else None

    def _valid_ids(self, media_ids: list[str]) -> list[str]:
        return [m for m in media_ids if self._path(m) is not None]

    def product_queue(self, product_id) -> list[str]:
        ids = self._valid_ids(self.product_media.get(product_id, []))
        if not ids:
            raise PlaybackError(409, f"Sản phẩm #{product_id}: video chưa tải về cache của Agent")
        return ids

    def client(self):
        return self.client_factory(self.config)

    # ----- playback policy ------------------------------------------------------------------------

    def _policy(self) -> PlaybackPolicy | None:
        if (self.policy is not None and self.session_mode == "product" and self.session_id is not None
                and self.session_id == self.policy_session_id):
            return self.policy
        return None

    def _video_order(self) -> str:
        if self.session_mode == "video":
            return self.video_order
        policy = self._policy()
        return policy.video_order if policy else "sequential"

    def _has_cached(self, product_id) -> bool:
        return any(self._path(m) is not None for m in self.product_media.get(product_id, []))

    def _plan_next(self, current: int, exclude=frozenset()) -> int | None:
        """Index of the video after ``current`` (an index of self.ids; -1 when it left the lineup)."""
        n = len(self.ids)
        order = self._video_order()
        if order == "sequential":
            if current + 1 < n:
                return current + 1
            return 0 if self.loop and n else None
        current_id = self.ids[current] if 0 <= current < n else None
        candidates = [m for m in self.ids if m != current_id and m not in exclude]
        if not candidates:
            return None if current_id is None or current_id in exclude else current
        planned = self._planned_next
        if planned in candidates:
            return self.ids.index(planned)
        if order == "random":
            chosen = self.rng.choice(candidates)
        else:
            chosen = None
            for refilled in (False, True):
                while self._video_bag:
                    media_id = self._video_bag.pop(0)
                    if media_id in candidates:
                        chosen = media_id
                        break
                if chosen is not None or refilled:
                    break
                # New round over every video; its first is never the one that just played.
                bag = list(self.ids)
                self.rng.shuffle(bag)
                if bag[0] == current_id:
                    bag.append(bag.pop(0))
                self._video_bag = bag
            if chosen is None:
                chosen = self.rng.choice(candidates)
        self._planned_next = chosen
        return self.ids.index(chosen)

    def _plan_start(self, product_id, ids: list[str]) -> str:
        """First video of a product about to go on air, chosen by video_order."""
        planned = self._planned_start
        if planned is not None and planned[0] == product_id and planned[1] in ids:
            return planned[1]
        media_id = ids[0] if self._video_order() == "sequential" else ids[self.rng.randrange(len(ids))]
        self._planned_start = (product_id, media_id)
        return media_id

    def _start_index(self, product_id, ids: list[str]) -> int:
        media_id = self._plan_start(product_id, ids)
        self._planned_start = None
        return ids.index(media_id)

    def _rotation_policy(self) -> PlaybackPolicy | None:
        policy = self._policy()
        if policy is None or not policy.auto or policy.product_rotation == "off":
            return None
        return policy

    def _product_seconds(self) -> int:
        return int(time.monotonic() - self.product_started_at) if self.product_started_at is not None else 0

    def _plan_product(self):
        """The product automatic rotation would switch to, or None (remembered until used)."""
        policy = self._rotation_policy()
        if policy is None:
            return None
        current = self.product_id
        order = self.product_order
        planned = self._planned_product
        if planned is not None and planned != current and planned in order and self._has_cached(planned):
            return planned
        target = None
        if policy.product_rotation == "sequential":
            start = order.index(current) if current in order else -1
            for step in range(1, len(order) + 1):
                candidate = order[(start + step) % len(order)]
                if candidate != current and self._has_cached(candidate):
                    target = candidate
                    break
        elif policy.product_rotation == "random":
            candidates = [p for p in order if p != current and self._has_cached(p)]
            target = self.rng.choice(candidates) if candidates else None
        else:
            for refilled in (False, True):
                while self._product_bag:
                    candidate = self._product_bag.pop(0)
                    if candidate != current and candidate in order and self._has_cached(candidate):
                        target = candidate
                        break
                if target is not None or refilled:
                    break
                bag = [p for p in order if self._has_cached(p)]
                if self._product_rounds == 0:
                    # The product already on air counts as the first of the first round.
                    bag = [p for p in bag if p != current]
                self.rng.shuffle(bag)
                if bag and bag[0] == current:
                    bag.append(bag.pop(0))
                self._product_bag = bag
                self._product_rounds += 1
        self._planned_product = target
        return target

    def _rotation_due(self, minutes_only: bool = False) -> bool:
        policy = self._rotation_policy()
        if policy is None or self.pending_product_id is not None:
            return False
        if policy.rotate_after_minutes is not None and self._product_seconds() >= policy.rotate_after_minutes * 60:
            return True
        return (not minutes_only and policy.rotate_after_videos is not None
                and self.videos_in_product >= policy.rotate_after_videos)

    def _rotate(self) -> bool:
        target = self._plan_product()
        if target is None:
            return False
        ids = self.product_queue(target)
        start = self._start_index(target, ids)
        try:
            self.switch_product(target, trigger="auto_rotation", ids=ids, start=start)
            return True
        except PlaybackError as exc:
            if exc.status_code != 409:
                raise
            self._planned_product = None
            self._skipped(ids[start], exc)
            return False

    def _skipped(self, media_id: str, exc: PlaybackError):
        self.last_status = f"Bỏ qua video lỗi: {exc.detail}"
        self._emit("ERROR", {"code": "video_skipped", "media_id": media_id, "message": exc.detail})

    def _following_media(self, index: int, count: int) -> str | None:
        """Video to preload on the stand-by A/B source while ``index`` plays as video ``count`` of its product."""
        if (self.session_mode == "product" and self.pending_product_id is not None
                and self.pending_product_id != self.product_id):
            try:
                return self._plan_start(self.pending_product_id, self.product_queue(self.pending_product_id))
            except PlaybackError:
                return None
        policy = self._rotation_policy()
        if policy is not None and policy.rotate_after_videos is not None and count >= policy.rotate_after_videos:
            target = self._plan_product()
            if target is not None:
                try:
                    return self._plan_start(target, self.product_queue(target))
                except PlaybackError:
                    pass
        following = self._plan_next(index)
        return self.ids[following] if following is not None else None

    def _restage_following(self):
        if not (self.dual_mode and self.active and self.slots):
            return
        media_id = self._following_media(self.index, self.videos_in_product)
        if media_id is None:
            return
        client = self.client()
        try:
            self._stage_media(client, 1 - self.active_slot, media_id)
        finally:
            _close(client)

    def set_policy(self, session_id: int, policy: dict, lineup: list[dict]):
        """Store a LIVE session's playback policy and product lineup; applies now if that session is on air."""
        try:
            body = PolicyUpdate.model_validate({"session_id": session_id, "policy": policy, "lineup": lineup})
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(x) for x in first.get("loc", ())) or "policy"
            raise PlaybackError(422, f"Chính sách phát không hợp lệ: {where}: {first.get('msg')}") from exc
        with self.lock:
            previous = self._policy()
            order = [item.product_id for item in body.lineup]
            for item in body.lineup:
                self.product_media[item.product_id] = list(item.media_ids)
            if order != self.product_order or self.policy_session_id != body.session_id:
                self._product_bag = []
                self._product_rounds = 0
            self.product_order = order
            self.policy = body.policy
            self.policy_session_id = body.session_id
            self._planned_product = None
            current = self._policy()
            if previous is None or current is None or previous.video_order != current.video_order:
                self._planned_next = None
                self._planned_start = None
                self._video_bag = []
            if current is not None:
                try:
                    self._restage_following()
                except Exception as exc:
                    log.warning("Không preload lại video kế sau khi đổi chính sách: %s", exc)
        return self.status()

    def _sources(self):
        return [self.config.source_a.strip() or self.config.input_name + " A",
                self.config.source_b.strip() or self.config.input_name + " B"]

    def _locate_dual(self, client):
        """Only switch to A/B when BOTH scene items exist in current scene."""
        scene = client.get_current_program_scene().current_program_scene_name
        names = self._sources()
        try:
            ids = [client.get_scene_item_id(scene, name).scene_item_id for name in names]
        except Exception:
            return None
        return scene, names, ids

    def _media(self, index):
        return self._prepare(self.ids[index])

    def _prepare(self, media_id: str):
        path = self._path(media_id)
        if path is None:
            raise PlaybackError(409, f"Video {media_id[:12]} chưa tải về cache của Agent")
        path = path.resolve()
        stat = path.stat()
        canvas = self._canvas_size()
        key = f"{path}|{canvas}"
        cached = self._prepared.get(key)
        if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns) and Path(cached[2]).is_file():
            return cached[2], path.name
        try:
            info = probe_media(path)
            playable = obs_file(path, info, canvas)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise PlaybackError(409, f"Không kiểm tra/chuẩn hóa được {path.name}: {exc}") from exc
        self._prepared[key] = (stat.st_size, stat.st_mtime_ns, str(playable))
        return str(playable), path.name

    def _canvas_size(self) -> tuple[int, int] | None:
        """OBS canvas (base) size, read once per session; None when OBS cannot tell (OBS then letterboxes)."""
        if self._canvas is None:
            client = None
            try:
                client = self.client()
                settings = client.get_video_settings()
                width, height = settings.base_width, settings.base_height
                if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                    self._canvas = (width, height)
            except Exception as exc:
                log.warning("Không đọc được canvas OBS, video khác tỉ lệ sẽ có viền đen: %s", exc)
            finally:
                if client is not None:
                    _close(client)
        return self._canvas

    def prewarm(self, media_ids: list[str]) -> dict:
        """Render the OBS copies (rotation, blurred fill) of cached videos ahead of LIVE, outside the engine lock,
        so starting or switching a video never waits for FFmpeg."""
        ready, failed = 0, 0
        for media_id in media_ids:
            try:
                self._prepare(media_id)
                ready += 1
            except Exception as exc:  # noqa: BLE001 - playback prepares (and reports) the file again when needed
                failed += 1
                log.warning("Không chuẩn bị trước được video %s: %s", str(media_id)[:12], exc)
        return {"ready": ready, "failed": failed}

    def _stage(self, client, slot, index):
        path, _ = self._media(index)
        self._stage_path(client, slot, path)

    def _stage_media(self, client, slot, media_id):
        path, _ = self._prepare(media_id)
        self._stage_path(client, slot, path)

    def _stage_path(self, client, slot, path):
        name = self.slots[slot]
        # Keep decoder open when hidden. Mute the stand-by source to avoid audio overlap.
        # OBS defaults would blank an ended source and restart a source when it is shown: both flash black.
        client.set_input_mute(name, True)
        client.set_input_settings(name, {"local_file": path, "is_local_file": True, "looping": False,
                                         "close_when_inactive": False, "restart_on_activate": False,
                                         "clear_on_media_end": False}, overlay=True)
        client.trigger_media_input_action(name, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
        # Hold the stand-by source on its first decoded frame, so showing it later needs no decoding.
        for _ in range(STAGE_FRAME_POLLS):
            report = client.get_media_input_status(name)
            cursor = getattr(report, "media_cursor", None)
            if report.media_state == "OBS_MEDIA_STATE_PLAYING" and isinstance(cursor, (int, float)) and cursor > 0:
                break
            time.sleep(0.05)
        client.trigger_media_input_action(name, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE")

    def _verify_input(self, client, name: str, kind: str) -> tuple[str, int]:
        scene = client.get_current_program_scene().current_program_scene_name
        inputs = client.get_input_list().inputs
        if not any(i.get("inputName") == name and
                   (i.get("unversionedInputKind") or i.get("inputKind")) == kind for i in inputs):
            raise PlaybackError(422, f"OBS chưa có nguồn {kind} '{name}'")
        try:
            item_id = client.get_scene_item_id(scene, name).scene_item_id
        except Exception as exc:
            raise PlaybackError(422, f"Nguồn '{name}' chưa nằm trong scene hiện tại") from exc
        return scene, item_id

    def _fit_video(self, client, scene: str, item_id: int):
        """Use OBS fit-inside bounds, with no scene crop, rotation or mirror."""
        canvas = client.get_video_settings()
        width, height = canvas.base_width, canvas.base_height
        if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
            raise PlaybackError(503, "Không đọc được kích thước canvas OBS")
        client.set_scene_item_transform(scene, item_id, {
            "positionX": width / 2, "positionY": height / 2, "alignment": 0,
            "rotation": 0, "scaleX": 1, "scaleY": 1,
            "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0,
            "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0,
            "boundsWidth": width, "boundsHeight": height,
        })

    def use_camera(self):
        """Keep the LIVE session running while switching its visible source."""
        with self.lock:
            if not self.active:
                raise PlaybackError(409, "Chưa có phiên video để chuyển sang camera")
            if self.camera_mode:
                return {"ok": True, "source_mode": "camera"}
            name = self.config.camera_input.strip()
            if not name or name in ([self.config.input_name] + self._sources()):
                raise PlaybackError(422, "Cần cấu hình Video Capture Device riêng trong OBS")
            client = self.client()
            scene = None
            camera_id = video_id = None
            try:
                scene, camera_id = self._verify_input(client, name, "dshow_input")
                video_scene, video_id = self._verify_input(client, self.active_source(), "ffmpeg_source")
                if scene != video_scene:
                    raise PlaybackError(422, "Camera và video phải cùng scene")
                camera_settings = client.get_input_settings(name).input_settings
                if not camera_settings.get("video_device_id"):
                    raise PlaybackError(422, "Camera OBS chưa chọn thiết bị hình ảnh")
                # Keep the current video visible until OBS accepts the camera item.
                client.set_input_mute(name, True)
                client.set_scene_item_enabled(scene, camera_id, True)
                client.set_input_mute(self.active_source(), True)
                client.set_scene_item_enabled(scene, video_id, False)
                client.set_input_mute(name, False)
                self.camera_scene_item_id = camera_id
                self.camera_mode = True
                self.paused = False
                self.last_status = "Đang dùng camera"
                return {"ok": True, "source_mode": "camera"}
            except Exception:
                try:
                    if scene is not None and video_id is not None:
                        client.set_scene_item_enabled(scene, video_id, True)
                    client.set_input_mute(self.active_source(), False)
                    client.set_input_mute(name, True)
                    if scene is not None and camera_id is not None:
                        client.set_scene_item_enabled(scene, camera_id, False)
                except Exception:
                    pass
                raise
            finally:
                _close(client)

    def use_video(self):
        with self.lock:
            if not self.camera_mode:
                return {"ok": True, "source_mode": "video"}
            client = self.client()
            try:
                scene, video_id = self._verify_input(client, self.active_source(), "ffmpeg_source")
                camera_id = self.camera_scene_item_id
                if camera_id is None:
                    raise PlaybackError(409, "Không rõ camera đang dùng")
                client.set_input_mute(self.active_source(), True)
                client.trigger_media_input_action(self.active_source(), "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                client.set_scene_item_enabled(scene, video_id, True)
                client.set_input_mute(self.config.camera_input, True)
                client.set_input_mute(self.active_source(), False)
                client.set_scene_item_enabled(scene, camera_id, False)
                self.camera_mode = False
                self.paused = False
                self.last_progress_at = time.monotonic()
                self.last_cursor = None
                self.last_status = "Đã trở lại video"
                return {"ok": True, "source_mode": "video"}
            except Exception:
                try:
                    client.set_input_mute(self.active_source(), True)
                    if 'scene' in locals() and 'video_id' in locals():
                        client.set_scene_item_enabled(scene, video_id, False)
                    client.set_input_mute(self.config.camera_input, False)
                except Exception:
                    pass
                raise
            finally:
                _close(client)

    def _play_dual(self, client, index, initial=False, count=1):
        if initial:
            located = self._locate_dual(client)
            if not located:
                raise PlaybackError(422, "Cần có đủ hai Media Source " + " và ".join(self._sources()) + " trong scene OBS hiện tại")
            self.scene_name, self.slots, self.scene_item_ids = located
            self.active_slot = 0
            for name in self.slots:
                self._verify_input(client, name, "ffmpeg_source")
            for item_id in self.scene_item_ids:
                self._fit_video(client, self.scene_name, item_id)
            if self.config.camera_input:
                try:
                    camera_scene, camera_id = self._verify_input(client, self.config.camera_input, "dshow_input")
                    if camera_scene == self.scene_name:
                        client.set_input_mute(self.config.camera_input, True)
                        client.set_scene_item_enabled(camera_scene, camera_id, False)
                except PlaybackError:
                    pass
            for slot, name in enumerate(self.slots):
                client.set_input_mute(name, True)
                client.set_scene_item_enabled(self.scene_name, self.scene_item_ids[slot], False)
            self._stage(client, 0, index)
            client.set_scene_item_enabled(self.scene_name, self.scene_item_ids[0], True)
            client.trigger_media_input_action(self.slots[0], "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY")
            client.set_input_mute(self.slots[0], False)
        else:
            incoming = 1 - self.active_slot
            # The next input has been staged during the previous video.
            expected_path, _ = self._media(index)
            actual_settings = client.get_input_settings(self.slots[incoming]).input_settings
            if actual_settings.get("local_file") != expected_path:
                self._stage(client, incoming, index)
            # Show the next video first: no empty frame between sources. It waits paused on its first frame.
            client.set_scene_item_enabled(self.scene_name, self.scene_item_ids[incoming], True)
            client.trigger_media_input_action(self.slots[incoming], "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY")
            client.set_input_mute(self.slots[self.active_slot], True)
            client.set_input_mute(self.slots[incoming], False)
            client.set_scene_item_enabled(self.scene_name, self.scene_item_ids[self.active_slot], False)
            self.active_slot = incoming
        following = self._following_media(index, count)
        if following is not None:
            try:
                self._stage_media(client, 1 - self.active_slot, following)
            except PlaybackError as exc:
                self.last_status = f"Không preload được video kế: {exc.detail}"

    def active_source(self):
        return self.slots[self.active_slot] if self.dual_mode and self.slots else self.config.input_name

    def _media_action(self, action: str, label: str):
        c = None
        try:
            c = self.client()
            c.trigger_media_input_action(self.active_source(), action)
        except Exception as exc:
            raise PlaybackError(503, f"OBS {label} thất bại: {exc}") from exc
        finally:
            if c is not None:
                _close(c)

    def _deactivate(self, reason: str, message: str):
        """Engine-initiated end of playback (not an explicit stop command)."""
        self.active = False
        self.paused = False
        self.last_status = message
        self._emit("PLAYBACK_ENDED", {"reason": reason, "message": message})

    # ----- status ---------------------------------------------------------------------------------

    def metrics(self):
        return {"session_mode": self.session_mode, "queue_ids": list(self.ids), "product_id": self.product_id,
                "loop_iteration": self.loop_iteration,
                "pending_product_id": self.pending_product_id, "session_id": self.session_id,
                "paused": self.paused, "loop": self.loop, "retry_count": self.retry_count,
                "error_count": self.error_count, "transition_count": self.transition_count,
                "recovery_count": self.recovery_count, "source_mode": "camera" if self.camera_mode else "video",
                "session_seconds": int(time.time() - self.session_started_at) if self.session_started_at else 0,
                "policy": self.policy.model_dump() if self.policy is not None else None,
                "rotation": {"auto": bool(self.policy is not None and self.policy.auto),
                             "videos_in_product": self.videos_in_product if self.session_mode == "product" else 0,
                             "product_seconds": self._product_seconds() if self.session_mode == "product" else 0,
                             "next_product_id": self._plan_product() if self.active else None},
                # Current connection settings (never the password) so the UI can show and prefill them.
                "obs": {"host": self.config.host, "port": self.config.port, "input_name": self.config.input_name,
                        "dual_mode": self.dual_mode, "dual_sources": self._sources(),
                        "has_password": bool(self.config.password), "camera_input": self.config.camera_input}}

    def status(self):
        """Same shape as GET /api/live/status."""
        with self.lock:
            try:
                c = self.client()
                try:
                    version = c.get_version().obs_version
                    if self.dual_mode:
                        located = self._locate_dual(c)
                        if located is None:
                            return {"connected": False, "error": "Cần hai Media Sources: " + " và ".join(self._sources()) + " trong scene đang chạy", "dual_mode": True, "running": self.active, "playlist_id": self.playlist_id, "index": self.index, "count": len(self.ids), "last_status": self.last_status, "loop": self.loop, "retry_count": self.retry_count, **self.metrics()}
                        source_name = self.slots[self.active_slot] if self.active and self.slots else located[1][0]
                    else:
                        source_name = self.config.input_name
                    media = c.get_media_input_status(source_name)
                    return {"connected": True, "obs_version": version, "media_state": media.media_state,
                            "media_cursor": getattr(media, "media_cursor", None),
                            "media_duration": getattr(media, "media_duration", None),
                            "running": self.active, "playlist_id": self.playlist_id,
                            "index": self.index, "count": len(self.ids),
                            "input_name": self.config.input_name, "dual_mode": self.dual_mode, "last_status": self.last_status, **self.metrics()}
                finally:
                    _close(c)
            except Exception as exc:
                return {"connected": False, "error": str(exc), "running": self.active,
                        "playlist_id": self.playlist_id, "index": self.index,
                        "count": len(self.ids), "input_name": self.config.input_name,
                        "last_status": self.last_status, **self.metrics()}

    # ----- core playback --------------------------------------------------------------------------

    def play(self, index: int):
        with self.lock:
            if self.camera_mode:
                raise PlaybackError(409, "Đang dùng camera; hãy chuyển về video trước")
            if not self.ids:
                raise PlaybackError(409, "Playlist rỗng")
            if not 0 <= index < len(self.ids):
                raise PlaybackError(422, "Vị trí video không hợp lệ")
            if not self.active:
                self._canvas = None  # the OBS canvas may have changed between sessions
            path, name = self._media(index)
            fresh = self._product_starting
            count = 1 if fresh else self.videos_in_product + 1
            if self._planned_next == self.ids[index]:
                self._planned_next = None
            if fresh:
                # A product goes on air: its shuffle round starts with this video.
                self._planned_next = None
                self._video_bag = []
                if self._video_order() == "shuffle":
                    self._video_bag = [m for m in self.ids if m != self.ids[index]]
                    self.rng.shuffle(self._video_bag)
            try:
                client = self.client()
                try:
                    if self.dual_mode:
                        self._play_dual(client, index, initial=not self.active, count=count)
                    else:
                        video_scene, video_id = self._verify_input(client, self.config.input_name, "ffmpeg_source")
                        self._fit_video(client, video_scene, video_id)
                        client.set_scene_item_enabled(video_scene, video_id, True)
                        if self.config.camera_input:
                            try:
                                camera_scene, camera_id = self._verify_input(client, self.config.camera_input, "dshow_input")
                                if camera_scene == video_scene:
                                    client.set_input_mute(self.config.camera_input, True)
                                    client.set_scene_item_enabled(camera_scene, camera_id, False)
                            except PlaybackError:
                                pass
                        client.set_input_settings(self.config.input_name,
                                                  {"local_file": path, "is_local_file": True, "looping": False,
                                                   "close_when_inactive": False, "restart_on_activate": False},
                                                  overlay=True)
                        applied = client.get_input_settings(self.config.input_name).input_settings
                        if applied.get("local_file") != path:
                            raise PlaybackError(503, "OBS không nhận file mới")
                        client.trigger_media_input_action(self.config.input_name,
                                                          "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                        client.set_input_mute(self.config.input_name, False)
                    muted = client.get_input_mute(self.active_source()).input_muted
                    log.info("OBS playback source=%s file=%s mute=%s", self.active_source(), name, muted)
                    if muted:
                        raise PlaybackError(503, "OBS vẫn mute nguồn video đang phát")
                finally:
                    _close(client)
            except PlaybackError:
                raise
            except Exception as exc:
                self.last_status = str(exc)
                raise PlaybackError(503, f"Không điều khiển được OBS: {exc}") from exc
            if self.active:
                self.transition_count += 1
                if self.session_mode == "video":
                    self.loop_iteration += 1
            self.index = index
            self.last_progress_at = time.monotonic()
            self.last_cursor = None
            self.stall_restarts = 0
            self.retry_count = 0
            self.next_retry_at = 0.0
            self.active = True
            self.paused = False
            self.generation += 1
            if self.session_mode == "product":
                self.videos_in_product = count
                if fresh or self.product_started_at is None:
                    self.product_started_at = time.monotonic()
            self._product_starting = False
            self.last_status = f"Đang phát {name}"
            # The new video (and loop iteration) is known now: no wait for the next watchdog tick.
            self._report_position("OBS_MEDIA_STATE_PLAYING", 0)
            return {"ok": True, "index": index, "filename": name, "media_id": self.ids[index]}

    def _begin_session(self, ids, *, mode, loop, playlist_id=None, session_id=None, product_id=None):
        self.session_mode = mode
        self.session_id = session_id
        self.product_id = product_id
        self.pending_product_id = None
        self.ids = ids
        self.playlist_id = playlist_id
        self.loop = loop
        self.paused = False
        self.session_started_at = time.time()
        self.transition_count = self.recovery_count = self.error_count = 0
        self.loop_iteration = 0
        self.video_order = "sequential"
        self.videos_in_product = 0
        self.product_started_at = None
        self._product_starting = mode == "product"
        self._planned_next = self._planned_product = self._planned_start = None
        self._video_bag, self._product_bag, self._product_rounds = [], [], 0

    def start_playlist(self, media_ids: list[str], loop: bool = True, start_index: int = 0, playlist_id=None):
        with self.lock:
            if self.active:
                raise PlaybackError(409, "Hãy dừng playlist hiện tại trước")
            self._refresh_config_when_idle()
            if not media_ids:
                raise PlaybackError(422, "Playlist cần ít nhất 1 video")
            missing = [m for m in media_ids if self._path(m) is None]
            if missing:
                raise PlaybackError(409, f"{len(missing)} video chưa tải về cache của Agent")
            self._begin_session(list(media_ids), mode="playlist", loop=bool(loop), playlist_id=playlist_id)
            return self.play(start_index)

    def video_play(self, session_id: int, media_id: str | None = None, loop: bool = True, start_ms: int = 0,
                   media_ids: list[str] | None = None, video_order: str = "sequential"):
        """Start a Single Video Timeline session: its videos (``media_ids``, or the one ``media_id``) play one after
        another in ``video_order``, the whole list again at its end when ``loop``. ``media_id`` is the video to start
        with (default: the first by the order) and ``start_ms`` the position in it. Without ``loop``, random and
        shuffle play each video once in a random order. The same command again for the running session only updates
        ``loop`` (a retried start never restarts playback)."""
        ids = list(media_ids or [media_id])
        with self.lock:
            if self.active:
                if self.session_mode == "video" and self.session_id == session_id and sorted(self.ids) == sorted(ids):
                    self.loop = bool(loop)
                    return {"ok": True, "unchanged": True, "media_id": self.ids[self.index]}
                raise PlaybackError(409, "Agent đang phát nội dung khác; hãy dừng trước")
            self._refresh_config_when_idle()
            missing = [m for m in ids if self._path(m) is None]
            if missing:
                raise PlaybackError(409, f"{len(missing)}/{len(ids)} video của phiên chưa tải về cache của Agent")
            if video_order != "sequential" and not loop:
                self.rng.shuffle(ids)  # one round in a random order
                video_order = "sequential"
            self._begin_session(ids, mode="video", loop=bool(loop), session_id=session_id)
            self.video_order = video_order
            if media_id is not None:
                start = ids.index(media_id)
            else:
                start = 0 if video_order == "sequential" else self.rng.randrange(len(ids))
            if video_order == "shuffle":
                # The first round goes through every other video before one repeats.
                self._video_bag = [m for m in ids if m != ids[start]]
                self.rng.shuffle(self._video_bag)
            result = self.play(start)
        if start_ms > 0:
            try:
                self.seek(start_ms)
            except PlaybackError as exc:
                log.warning("Không tua được video chính tới %sms: %s", start_ms, exc.detail)
        return result

    def _report_now(self, media_state: str) -> None:
        """Report the cursor right after a pause / resume (the watchdog does not report while paused)."""
        if self.on_position is None or self.session_mode != "video":
            return
        try:
            client = self.client()
            try:
                cursor = getattr(client.get_media_input_status(self.active_source()), "media_cursor", None)
            finally:
                _close(client)
        except Exception as exc:  # noqa: BLE001 - the next tick reports anyway
            log.debug("Không đọc được vị trí phát: %s", exc)
            return
        self._report_position(media_state, cursor)

    def _report_position(self, media_state: str, cursor) -> None:
        if self.on_position is None or self.session_mode != "video" or not isinstance(cursor, (int, float)):
            return
        try:
            self.on_position({"session_id": self.session_id, "media_id": self.ids[self.index],
                              "position_ms": int(cursor), "media_state": media_state, "iteration": self.loop_iteration})
        except Exception:  # noqa: BLE001 - pin scheduling never breaks the watchdog
            log.exception("Timeline không nhận được vị trí phát")

    def switch_product(self, product_id, trigger: str | None = None, ids: list[str] | None = None,
                       start: int | None = None):
        """Play product_id from the video chosen by the policy (the first one by default).

        ``trigger`` set means the engine switched by itself (pending switch applied or auto_rotation).
        """
        ids = self.product_queue(product_id) if ids is None else ids
        start = self._start_index(product_id, ids) if start is None else start
        old_ids, old_index, old_product = self.ids, self.index, self.product_id
        self.ids = ids
        # Set before play() so the A/B preload plans from the new product.
        self.product_id = product_id
        self._product_starting = True
        try:
            result = self.play(start)
        except Exception:
            self.ids, self.index, self.product_id = old_ids, old_index, old_product
            self._product_starting = False
            raise
        previous = old_product
        if self._planned_product == product_id:
            self._planned_product = None
        self.pending_product_id = None
        self.session_mode = "product"
        if trigger is not None:
            self._emit("PRODUCT_SWITCHED", {"trigger": trigger, "from_product_id": previous,
                                            "media_id": result["media_id"]})
        return result

    def product_play(self, session_id: int, product_id: int, media_ids: list[str], mode: str = "immediate"):
        """Start a product LIVE session, or switch product inside the running one.

        mode 'immediate' plays the product now; 'after_current' queues it until the current
        video ends (then PRODUCT_SWITCHED is emitted). When nothing runs, it always starts now.
        """
        if mode not in ("immediate", "after_current"):
            raise PlaybackError(422, "mode phải là immediate hoặc after_current")
        with self.lock:
            if not self.active:
                self._refresh_config_when_idle()
            if self.active and self.session_mode != "product":
                raise PlaybackError(409, "Đang phát playlist; hãy dừng trước khi phát sản phẩm")
            if self.active and self.session_id != session_id:
                raise PlaybackError(409, f"Phiên LIVE #{self.session_id} đang phát; hãy dừng trước")
            previous_lineup = self.product_media.get(product_id)
            self.product_media[product_id] = list(media_ids)
            try:
                self.product_queue(product_id)
            except PlaybackError:
                if previous_lineup is None:
                    self.product_media.pop(product_id, None)
                else:
                    self.product_media[product_id] = previous_lineup
                raise
            if not self.active:
                ids = self.product_queue(product_id)
                self._begin_session(ids, mode="product", loop=True, session_id=session_id, product_id=product_id)
                try:
                    return self.play(self._start_index(product_id, ids))
                except Exception:
                    self.product_id = None
                    self._product_starting = False
                    raise
            if mode == "immediate":
                return self.switch_product(product_id)
            if product_id == self.product_id:
                # Same product: the new lineup applies at the next video boundary.
                self.pending_product_id = None
                return {"ok": True, "pending_product_id": None}
            self.pending_product_id = product_id
            if self.dual_mode and self.slots:
                # The pending product will be needed at the next boundary. Replace the
                # ordinary next-video preload while its scene item is still hidden.
                pending_first = self._plan_start(product_id, self.product_queue(product_id))
                try:
                    client = self.client()
                    try:
                        self._stage_media(client, 1 - self.active_slot, pending_first)
                    finally:
                        _close(client)
                except Exception as exc:
                    log.warning("Không preload được sản phẩm #%s: %s", product_id, exc)
            self.last_status = f"Đã xếp sản phẩm #{product_id} sau video hiện tại"
            return {"ok": True, "pending_product_id": product_id}

    def set_loop(self, loop: bool):
        """Local operator toggle (desktop IPC); product sessions always loop their lineup."""
        with self.lock:
            if self.session_mode != "product":
                self.loop = bool(loop)
            return {"ok": True, "loop": self.loop}

    def cancel_pending(self):
        with self.lock:
            pending = self.pending_product_id
            self.pending_product_id = None
            if pending is not None:
                self._restage_following()
            return {"ok": True, "cancelled_product_id": pending}

    def pause(self):
        with self.lock:
            if not self.active or self.paused:
                raise PlaybackError(409, "Không có video đang phát để Pause")
            self._media_action("OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE", "Pause")
            self.paused = True
            self.last_status = "Đã tạm dừng"
            self._report_now("OBS_MEDIA_STATE_PAUSED")
            return {"ok": True}

    def resume(self):
        with self.lock:
            if not self.active or not self.paused:
                raise PlaybackError(409, "Không có video tạm dừng để Resume")
            self._media_action("OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY", "Resume")
            self.paused = False
            self.last_cursor = None
            self.last_progress_at = time.monotonic()
            self.last_status = "Đã tiếp tục phát"
            self._report_now("OBS_MEDIA_STATE_PLAYING")
            return {"ok": True}

    def next(self):
        with self.lock:
            if not self.active:
                raise PlaybackError(409, "Chưa phát playlist")
            if self.pending_product_id is not None:
                return self.switch_product(self.pending_product_id, trigger="next")
            target = self._plan_next(self.index)
            if target is None:
                return self.stop()
            return self.play(target)

    def previous(self):
        with self.lock:
            if not self.active:
                raise PlaybackError(409, "Chưa phát playlist")
            target = self.index - 1
            if target < 0:
                target = len(self.ids) - 1 if self.loop else 0
            return self.play(target)

    def select(self, index: int):
        with self.lock:
            if not self.active:
                raise PlaybackError(409, "Hãy bắt đầu playlist trước")
            return self.play(index)

    def seek(self, milliseconds: int):
        with self.lock:
            if not self.active:
                raise PlaybackError(409, "Chưa phát playlist")
            if milliseconds < 0:
                raise PlaybackError(422, "Thời điểm tua không hợp lệ")
            c = None
            try:
                c = self.client()
                # OBS knows the duration of the loaded file; the Agent has no media DB.
                try:
                    duration = getattr(c.get_media_input_status(self.active_source()), "media_duration", None)
                except Exception:
                    duration = None
                if isinstance(duration, (int, float)) and duration > 0 and milliseconds > duration:
                    raise PlaybackError(422, "Thời điểm tua vượt quá thời lượng video")
                c.set_media_input_cursor(self.active_source(), milliseconds)
            except PlaybackError:
                raise
            except Exception as exc:
                raise PlaybackError(503, f"OBS Seek thất bại: {exc}") from exc
            finally:
                if c is not None:
                    _close(c)
            self.last_progress_at = time.monotonic()
            self.last_cursor = milliseconds
            self.last_status = f"Đã tua đến {milliseconds / 1000:.1f}s"
            # Pins and overlays recompute at the new position at once (skipped markers are never replayed).
            self._report_position("OBS_MEDIA_STATE_PAUSED" if self.paused else "OBS_MEDIA_STATE_PLAYING", milliseconds)
            return {"ok": True}

    def stop(self):
        with self.lock:
            if self.camera_mode:
                client = self.client()
                try:
                    client.set_input_mute(self.config.camera_input, True)
                    if self.camera_scene_item_id is not None:
                        scene = client.get_current_program_scene().current_program_scene_name
                        client.set_scene_item_enabled(scene, self.camera_scene_item_id, False)
                finally:
                    _close(client)
                self.camera_mode = False
            self.active = False
            self.paused = False
            self.generation += 1
            try:
                client = self.client()
                try:
                    if self.dual_mode and self.slots:
                        for slot, name in enumerate(self.slots):
                            client.set_input_mute(name, True)
                            client.set_scene_item_enabled(self.scene_name, self.scene_item_ids[slot], False)
                            client.trigger_media_input_action(name, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP")
                    else:
                        client.trigger_media_input_action(self.config.input_name,
                                                          "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP")
                finally:
                    _close(client)
            except Exception as exc:
                raise PlaybackError(503, f"Không dừng được OBS: {exc}") from exc
            self.retry_count = 0
            self.pending_product_id = None
            self.last_status = "Đã dừng"
            return {"ok": True}

    # ----- OBS configuration ----------------------------------------------------------------------

    def configure(self, host="127.0.0.1", port=4455, password="", input_name="Sarae Video",
                  dual_mode=False, source_a="", source_b="", camera_input=""):
        """Save the OBS connection locally. An empty password keeps the saved one."""
        try:
            body = ObsConfig(host=host, port=port, password=password or "", input_name=input_name,
                             dual_mode=dual_mode, source_a=source_a or "", source_b=source_b or "",
                             camera_input=camera_input or "")
        except Exception as exc:
            raise PlaybackError(422, f"Cấu hình OBS không hợp lệ: {exc}") from exc
        if body.host not in LOCAL_HOSTS:
            raise PlaybackError(422, "Chỉ hỗ trợ OBS trên cùng máy Agent")
        if not body.input_name.strip():
            raise PlaybackError(422, "Thiếu tên Media Source")
        if body.dual_mode:
            a, b = body.source_a.strip() or body.input_name + " A", body.source_b.strip() or body.input_name + " B"
            if a == b:
                raise PlaybackError(422, "Hai Media Source A và B phải khác tên nhau")
        with self.lock:
            if self.active:
                raise PlaybackError(409, "Dừng playback trước khi đổi cấu hình OBS")
            if not body.password:
                body = body.model_copy(update={"password": self.config.password})
            self.active = False
            self.config = body
            self.dual_mode = body.dual_mode
            self.slots = []
            self.camera_mode = False
            self.last_status = "Đã cập nhật cấu hình OBS"
            try:
                self.store.save(body)
            except (OSError, AgentError) as exc:
                self.last_status = f"Đã cập nhật cấu hình OBS nhưng không lưu được xuống đĩa: {exc}"
        return self.status()

    def _refresh_config_when_idle(self):
        self.config = self.store.load()
        self.dual_mode = self.config.dual_mode
        self.slots = []

    def list_inputs(self, host="127.0.0.1", port=4455, password=""):
        """List OBS inputs, marking those placed in the current program scene. Saved config is untouched."""
        if host not in LOCAL_HOSTS:
            raise PlaybackError(422, "Chỉ hỗ trợ OBS trên cùng máy Agent")
        config = ObsConfig(host=host, port=port, password=password or self.config.password)
        try:
            client = self.client_factory(config)
        except Exception as exc:
            raise PlaybackError(503, f"Không kết nối được OBS: {exc}") from exc
        try:
            scene = client.get_current_program_scene().current_program_scene_name
            in_scene = {item.get("sourceName") for item in client.get_scene_item_list(scene).scene_items}
            inputs = [{"name": item.get("inputName"), "kind": item.get("unversionedInputKind") or item.get("inputKind"),
                       "in_scene": item.get("inputName") in in_scene}
                      for item in client.get_input_list().inputs]
            # Media Sources first, then those in the live scene, then by name.
            inputs.sort(key=lambda i: (i["kind"] != "ffmpeg_source", not i["in_scene"], i["name"] or ""))
            return {"scene": scene, "inputs": inputs}
        except Exception as exc:
            raise PlaybackError(502, f"OBS từ chối yêu cầu: {exc}") from exc
        finally:
            _close(client)

    # ----- watchdog -------------------------------------------------------------------------------

    def tick(self):
        """One watchdog iteration: advance ended videos, recover stalls, retry OBS with backoff."""
        with self.lock:
            if not self.active or self.paused or self.camera_mode or not self.ids or time.monotonic() < self.next_retry_at:
                return
            try:
                client = self.client()
                try:
                    source = self.active_source()
                    report = client.get_media_input_status(source)
                    media_status = report.media_state
                finally:
                    _close(client)
                cursor = getattr(report, "media_cursor", None)
                now = time.monotonic()
                if media_status in ("OBS_MEDIA_STATE_PLAYING", "OBS_MEDIA_STATE_ENDED"):
                    self._report_position(media_status, cursor)
                if media_status == "OBS_MEDIA_STATE_PLAYING" and isinstance(cursor, (int, float)):
                    if self.last_cursor is None or cursor > self.last_cursor + 100 or cursor < self.last_cursor - 500:
                        self.last_cursor = cursor
                        self.last_progress_at = now
                    elif now - self.last_progress_at >= 20:
                        if self.stall_restarts >= 2:
                            self._deactivate("stalled", "Watchdog: video đứng sau 2 lần thử khôi phục")
                            return
                        restore = self.client()
                        try:
                            restore.trigger_media_input_action(source, "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                        finally:
                            _close(restore)
                        self.stall_restarts += 1
                        self.recovery_count += 1
                        self.last_cursor = None
                        self.last_progress_at = now
                        self.last_status = "Watchdog: đã restart video bị đứng"
                elif media_status in ("OBS_MEDIA_STATE_PAUSED", "OBS_MEDIA_STATE_STOPPED"):
                    self.last_cursor = None
                    self.last_progress_at = now
                if self.retry_count:
                    self.recovery_count += 1
                    self.last_cursor = None
                    self.last_progress_at = now
                    self.last_status = "OBS đã kết nối lại; tiếp tục theo dõi"
                self.retry_count = 0
                self.next_retry_at = 0
                if media_status == "OBS_MEDIA_STATE_ENDED":
                    self._advance_after_end()
                elif (self.session_mode == "product" and self._rotation_policy() is not None
                      and self._rotation_policy().switch_mode == "immediate" and self._rotation_due(minutes_only=True)):
                    if not self._rotate():
                        # Nothing to switch to: stay on this product for another period instead of retrying each tick.
                        self.product_started_at = time.monotonic()
                elif self.dual_mode and media_status == "OBS_MEDIA_STATE_PLAYING":
                    self._schedule_swap(cursor, getattr(report, "media_duration", None))
            except Exception as exc:
                self.retry_count += 1
                self.error_count += 1
                self.last_status = f"OBS lỗi (thử lại {self.retry_count}/5): {exc}"
                if self.retry_count >= 5:
                    self._deactivate("obs_error", f"Đã tạm dừng do OBS lỗi nhiều lần: {exc}")
                else:
                    self.next_retry_at = time.monotonic() + min(2 ** self.retry_count, 20)

    def _schedule_swap(self, cursor, duration):
        """A/B: swap to the stand-by source just before the visible video ends; the watchdog polls only each second."""
        if not (isinstance(cursor, (int, float)) and isinstance(duration, (int, float)) and duration > 0):
            return
        remaining = duration - cursor
        if remaining > SWAP_HORIZON_MS or self._swap_generation == self.generation:
            return
        self._swap_generation = self.generation
        timer = threading.Timer(max(0, remaining - SWAP_LEAD_MS) / 1000, self._swap_near_end, args=(self.generation,))
        timer.daemon = True
        timer.start()

    def _swap_near_end(self, generation: int):
        with self.lock:
            self._swap_generation = None
            if not self.active or self.paused or self.camera_mode or generation != self.generation:
                return
            try:
                client = self.client()
                try:
                    report = client.get_media_input_status(self.active_source())
                finally:
                    _close(client)
                cursor = getattr(report, "media_cursor", None)
                duration = getattr(report, "media_duration", None)
                # Paused, already ended (the watchdog advances) or sought back: leave it to the next tick.
                if (report.media_state != "OBS_MEDIA_STATE_PLAYING" or not isinstance(cursor, (int, float))
                        or not isinstance(duration, (int, float)) or duration - cursor > SWAP_LEAD_MS + 500):
                    return
                self._advance_after_end()
            except Exception as exc:
                log.warning("A/B: không chuyển sớm được, chờ video hết: %s", exc)

    def _advance_after_end(self):
        if (self.session_mode == "product" and self.pending_product_id is not None
                and self.pending_product_id != self.product_id):
            pending = self.pending_product_id
            try:
                self.switch_product(pending, trigger="video_end")
                return
            except PlaybackError as exc:
                self.pending_product_id = None
                self.last_status = f"Chuyển sản phẩm lỗi: {exc.detail}; tiếp tục sản phẩm cũ"
                self._emit("ERROR", {"code": "switch_failed", "pending_product_id": pending,
                                     "message": exc.detail})
        elif self.session_mode == "product" and self._rotation_due() and self._rotate():
            return
        if self.session_mode == "product":
            try:
                refreshed = self.product_queue(self.product_id)
                previous_id = self.ids[self.index]
                self.index = refreshed.index(previous_id) if previous_id in refreshed else -1
                self.ids = refreshed
            except PlaybackError as exc:
                self._deactivate("no_media", f"Dừng phiên: {exc.detail}")
                return
        if self._video_order() != "sequential":
            failed: set[str] = set()
            for _ in range(len(self.ids)):
                candidate = self._plan_next(self.index, exclude=failed)
                if candidate is None:
                    break
                try:
                    self.play(candidate)
                    return
                except PlaybackError as exc:
                    if exc.status_code != 409:
                        raise
                    failed.add(self.ids[candidate])
                    self._planned_next = None
                    self._skipped(self.ids[candidate], exc)
            self._deactivate("no_media", "Không còn video hợp lệ để phát")
            return
        target = self.index + 1
        if target >= len(self.ids):
            if not self.loop:
                self._deactivate("playlist_end", "Đã hết playlist")
                return
            target = 0
        # Skips missing media files, up to one complete playlist traversal.
        for shift in range(len(self.ids)):
            candidate = (target + shift) % len(self.ids)
            if not self.loop and candidate < target:
                break
            try:
                self.play(candidate)
                return
            except PlaybackError as exc:
                if exc.status_code == 409:
                    self._skipped(self.ids[candidate], exc)
                    continue
                raise
        self._deactivate("no_media", "Không còn video hợp lệ để phát")

    def start_monitor(self, interval: float = 1.0):
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()

        def run():
            while not self._monitor_stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    pass

        self._monitor_thread = threading.Thread(target=run, daemon=True, name="sarae-playback-monitor")
        self._monitor_thread.start()

    def stop_monitor(self):
        self._monitor_stop.set()

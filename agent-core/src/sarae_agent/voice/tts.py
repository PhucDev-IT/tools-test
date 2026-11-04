"""LIVE voice queue and OBS playback; provider synthesis lives in providers.py.

Sentences come from the Cloud (`tts_speak`, AI replies the Cloud already checked) or the local GUI (`voice.speak`).
They wait in a short queue and a single worker speaks them in order:
  * a sentence older than its `ttl_seconds` is dropped, not spoken late (a viewer's question goes stale fast);
  * the MP3 is cached by provider, model, voice, settings, rate and text;
  * it plays on the OBS input named in config.obs.voice_input (a Media Source with audio in the mixer), and the next
    sentence starts only when OBS reports the previous one ended, so voices never overlap;
  * an `intro` (viewer names, e.g. "Lan với Hoa ơi,") is spoken just before the sentence as its own file, so the
    sentence body - the same answer for many viewers - stays one cached file;
  * while the voice speaks, the video inputs are lowered by DUCK_DB and restored when the queue is empty. The levels
    to restore are kept on disk too, so a crash mid-sentence is repaired the next time the voice starts.
`prepare()` (Cloud `tts_prepare`) fills the cache ahead of the LIVE on a second thread and never plays anything.
`clear()` (Cloud `tts_clear`) empties the queue and stops the sentence playing. The video media input is never
played or replaced here, only its volume changed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Callable

from sarae_agent.config import ConfigStore, paths
from sarae_agent.errors import AgentError
from sarae_agent.obs.controller import ENDED_STATES, ObsController
from sarae_agent.voice.providers import ProviderRegistry, default_registry

log = logging.getLogger("sarae.voice")

VOICES = ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural")
MAX_CHARS = 450
MAX_INTRO_CHARS = 120
MAX_PREPARE_TEXTS = 40


from sarae_agent.voice.jobs import JobVoiceMixin


class VoiceEngine(JobVoiceMixin):
    MAX_QUEUE = 4
    DEFAULT_TTL = 120.0
    DUCK_DB = 12.0
    POLL_SECONDS = 0.15
    MAX_SPEECH_SECONDS = 45.0
    CACHE_KEEP = 300
    SYLLABLES_PER_SECOND = 4.2
    MAX_PREPARE_PENDING = 200

    def __init__(self, config: ConfigStore, obs: ObsController,
                 synthesize: Callable[[str, str, str, str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 registry: ProviderRegistry | None = None):
        self._config = config
        self._obs = obs
        self._legacy_synthesize = synthesize
        self._registry = registry or default_registry()
        self._clock = clock
        self._cond = threading.Condition()
        self._queue: deque[dict] = deque()
        self._worker: threading.Thread | None = None
        self._prepare: deque[dict] = deque()  # texts to generate into the cache, never played
        self._prepare_keys: set[str] = set()
        self._preparer: threading.Thread | None = None
        self._prune_lock = threading.Lock()
        self._closed = False
        self._interrupt = threading.Event()
        self._ducked: dict[str, float] = {}
        self.current: dict | None = None
        self.last: dict | None = None
        self.stats = {"queued": 0, "spoken": 0, "expired": 0, "failed": 0, "cache_hits": 0, "cleared": 0,
                      "prepared": 0, "prepare_failed": 0}

    # --- synthesis -----------------------------------------------------------------------
    def _spec(self, provider: str, model: str | None, voice: str, rate: int, settings: dict | None) -> dict:
        if not isinstance(voice, str) or not 1 <= len(voice) <= 100:
            raise AgentError("INVALID_PARAMS", "Voice ID không hợp lệ")
        normalized_model, normalized_settings = self._registry.get(provider).normalize(model, voice, int(rate), settings)
        return {"provider": provider, "model": normalized_model, "voice": voice,
                "settings": normalized_settings}

    @property
    def _cache_dir(self):
        path = paths.voice / "cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _key(text: str, voice: str, rate: int, provider: str = "edge",
             model: str | None = None, settings: dict | None = None) -> str:
        return hashlib.sha256(json.dumps([provider, model, voice, rate, settings or {}, text],
                                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]

    def _cached(self, key: str):
        target = self._cache_dir / f"{key}.mp3"
        try:
            return target if target.is_file() and target.stat().st_size > 0 else None
        except OSError:  # pruned meanwhile
            return None

    def _audio(self, text: str, voice: str, rate: int, provider: str = "edge",
               model: str | None = None, settings: dict | None = None):
        """MP3 for one text: from the cache, else generated once and kept. Called by the speak worker and the
        prepare thread at the same time: each writes its own temp file, then os.replace (atomic)."""
        spec = self._spec(provider, model, voice, rate, settings)
        key = self._key(text, voice, rate, provider, spec["model"], spec["settings"])
        target = self._cached(key)
        if target is not None:
            try:
                os.utime(target)
            except OSError:
                pass
            with self._cond:
                self.stats["cache_hits"] += 1
            log.info("TTS audio ready provider=%s model=%s voice=%s chars=%s cache=hit",
                     provider, spec["model"], voice, len(text))
            return target
        target = self._cache_dir / f"{key}.mp3"
        temp = self._cache_dir / f"tmp_{uuid.uuid4().hex}.mp3"
        try:
            started = self._clock()
            log.info("TTS request started provider=%s model=%s voice=%s chars=%s cache=miss",
                     provider, spec["model"], voice, len(text))
            if self._legacy_synthesize and provider == "edge":
                self._legacy_synthesize(text, voice, str(temp), f"{rate:+d}%")
            else:
                provider_impl = self._registry.get(provider)
                provider_impl.synthesize(text, voice, spec["model"], spec["settings"], temp,
                    None, lambda: log.info("TTS first audio received provider=%s latency_ms=%s",
                                                provider, int((self._clock() - started) * 1000)))
            if not temp.is_file() or temp.stat().st_size == 0:
                raise AgentError("TTS_FAILED", "Dịch vụ TTS không trả về âm thanh", retryable=True)
            os.replace(temp, target)
            log.info("TTS audio ready provider=%s model=%s voice=%s chars=%s latency_ms=%s cache=miss",
                     provider, spec["model"], voice, len(text), int((self._clock() - started) * 1000))
        finally:
            temp.unlink(missing_ok=True)
        self._prune_cache()
        return target

    def _prune_cache(self) -> None:
        with self._prune_lock:
            files = []
            for path in list(self._cache_dir.glob("*.mp3")) + list(self._cache_dir.glob("*.wav")):
                if path.name.startswith("tmp_"):
                    continue  # being written by the other thread
                try:
                    files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            files.sort()
            for _, old in files[:-self.CACHE_KEEP]:
                try:
                    old.unlink()
                except OSError:
                    pass  # still opened by OBS
        for legacy in paths.voice.glob("voice_*.mp3"):  # one file per sentence, before the cache
            try:
                legacy.unlink()
            except OSError:
                pass

    # --- public API ----------------------------------------------------------------------
    def speak(self, text: str, voice: str = VOICES[0], obs_source: str | None = None, *, rate: int = 0,
              ttl_seconds: float | None = None, duck: bool = True, intro: str | None = None,
              provider: str = "edge", model: str | None = None, settings: dict | None = None,
              tts: dict | None = None, command_id=None, session_id=None, fallback=None) -> dict:
        """Queue one sentence; returns at once with its place in the queue.

        ``intro`` (viewer names) is spoken right before ``text``; the two are cached apart so the body is reused."""
        if provider == "omnivoice":
            return self._job_speak(text, voice, rate, ttl_seconds, duck, intro, tts, command_id, session_id, fallback)
        spec = self._spec(provider, model, voice, rate, settings)
        text = self._check(text)
        if intro is not None:
            intro = " ".join(intro.split())
            if not 1 <= len(intro) <= MAX_INTRO_CHARS:
                raise AgentError("INVALID_PARAMS", f"Lời gọi tên TTS từ 1 đến {MAX_INTRO_CHARS} ký tự")
        source = obs_source or self._config.get().obs.voice_input
        now = self._clock()
        item = {"id": uuid.uuid4().hex[:12], "text": text, "intro": intro, "voice": voice, "rate": int(rate), "source": source,
                "duck": bool(duck), "queued_at": now, "deadline": now + (ttl_seconds or self.DEFAULT_TTL),
                "provider": provider, "model": spec["model"], "settings": spec["settings"]}
        with self._cond:
            if self._closed:
                raise AgentError("TTS_UNAVAILABLE", "Dịch vụ giọng nói đã dừng")
            if len(self._queue) >= self.MAX_QUEUE:
                raise AgentError("TTS_QUEUE_FULL", f"Đang có {len(self._queue)} câu chờ đọc", retryable=True)
            self._queue.append(item)
            self.stats["queued"] += 1
            position = len(self._queue) + (1 if self.current else 0)
            self._ensure_worker()
            self._cond.notify_all()
        return {"status": "queued", "id": item["id"], "position": position, "source": source, "voice": voice,
                "provider": provider, "model": spec["model"], "chars": _chars(item)}

    def prepare(self, texts: list[str], voice: str = VOICES[0], rate: int = 0, *,
                provider: str = "edge", model: str | None = None, settings: dict | None = None, tts_jobs=None, fallback=None) -> dict:
        """Generate texts into the cache ahead of time (returns at once; a second thread does the work)."""
        if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_PREPARE_TEXTS:
            raise AgentError("INVALID_PARAMS", f"Danh sách chuẩn bị TTS từ 1 đến {MAX_PREPARE_TEXTS} câu")
        if provider == "omnivoice":
            if not tts_jobs or len(tts_jobs) != len(texts):
                raise AgentError("INVALID_PARAMS", "tts_jobs không khớp texts")
            with self._cond:
                if self._closed:
                    raise AgentError("TTS_UNAVAILABLE", "Voice engine đã dừng")
                if len(self._prepare) + len(texts) > self.MAX_PREPARE_PENDING:
                    raise AgentError("TTS_QUEUE_FULL", "Hàng đợi chuẩn bị đầy")
                for text, ref in zip(texts, tts_jobs):
                    self._prepare.append({"key": ref["utterance_id"], "omni": self._job_item(ref, text=text, voice=voice, fallback=fallback)})
                self._ensure_preparer()
                self._cond.notify_all()
            return {"accepted": len(tts_jobs), "already_cached": 0}
        spec = self._spec(provider, model, voice, rate, settings)
        cleaned = [self._check(t) for t in texts]
        accepted = cached = 0
        with self._cond:
            if self._closed:
                raise AgentError("TTS_UNAVAILABLE", "Dịch vụ giọng nói đã dừng")
            for text in dict.fromkeys(cleaned):
                key = self._key(text, voice, int(rate), provider, spec["model"], spec["settings"])
                if self._cached(key) is not None:
                    cached += 1
                    continue
                accepted += 1
                if key in self._prepare_keys:
                    continue  # already waiting
                self._prepare.append({"key": key, "text": text, "voice": voice, "rate": int(rate),
                                      "provider": provider, "model": spec["model"], "settings": spec["settings"]})
                self._prepare_keys.add(key)
                while len(self._prepare) > self.MAX_PREPARE_PENDING:  # oldest dropped: generated when spoken
                    self._prepare_keys.discard(self._prepare.popleft()["key"])
            if self._prepare:
                self._ensure_preparer()
                self._cond.notify_all()
        return {"accepted": accepted, "already_cached": cached}

    @staticmethod
    def _check(text: str) -> str:
        text = " ".join((text or "").split())
        if not 1 <= len(text) <= MAX_CHARS:
            raise AgentError("INVALID_PARAMS", f"Nội dung TTS từ 1 đến {MAX_CHARS} ký tự")
        return text

    def clear(self) -> dict:
        """Empty the queue and stop the sentence being spoken (emergency stop)."""
        with self._cond:
            self._job_generation = getattr(self, "_job_generation", 0) + 1
            cleared = len(self._queue)
            for item in self._queue:
                if item.get("tts"):
                    self._job_event(item, "cancelled", error_code="TTS_CANCELLED")
            self._queue.clear()
            self._prepare.clear()
            self._prepare_keys.clear()
            self.stats["cleared"] += cleared
            playing = self.current
            self._interrupt.set()
            self._cond.notify_all()
        stopped = False
        if playing is not None:
            try:
                self._obs.media_action(playing["source"], "stop")
                stopped = True
            except AgentError as exc:
                log.warning("Không dừng được nguồn giọng nói: %s", exc.message)
        return {"cleared": cleared, "stopped": stopped}

    def status(self) -> dict:
        with self._cond:
            speaking = " ".join(filter(None, (self.current["intro"], self.current["text"])))[:80] \
                if self.current else None
            from sarae_agent import __version__
            return {"version": __version__, "capabilities": {"providers": ["edge"] + (["omnivoice"] if self.job_provider else []), "tts_job": 1 if self.job_provider else 0, "audio_path": ["direct", "relay"]}, "queue": len(self._queue), "speaking": speaking, "ducked": bool(self._ducked),
                    "prepare_pending": len(self._prepare), "stats": dict(self.stats), "last": self.last}

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._prepare.clear()
            self._prepare_keys.clear()
            self._interrupt.set()
            self._cond.notify_all()
            threads = (self._worker, self._preparer)
        for thread in threads:
            if thread is not None:
                thread.join(timeout=3)

    # --- worker --------------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="voice-worker", daemon=True)
            self._worker.start()

    def _ensure_preparer(self) -> None:
        if self._preparer is None or not self._preparer.is_alive():
            self._preparer = threading.Thread(target=self._run_prepare, name="voice-prepare", daemon=True)
            self._preparer.start()

    def _run_prepare(self) -> None:
        while True:
            with self._cond:
                while not self._prepare and not self._closed:
                    self._cond.wait(timeout=5.0)
                if self._closed:
                    break
                job = self._prepare.popleft()
            try:
                if "omni" in job:
                    self._job_audio(job["omni"], job["omni"]["tts"], interrupt=False)
                    continue
                if self._cached(job["key"]) is None:  # spoken (and cached) by the worker meanwhile: nothing to do
                    self._audio(job["text"], job["voice"], job["rate"], job["provider"],
                                job["model"], job["settings"])
                    with self._cond:
                        self.stats["prepared"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad text must not stop the others
                with self._cond:
                    self.stats["prepare_failed"] += 1
                log.warning("Chuẩn bị TTS lỗi: %s", _reason(exc))
            finally:
                with self._cond:
                    self._prepare_keys.discard(job["key"])

    def _run(self) -> None:
        self._restore_saved()
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait(timeout=5.0)
                if self._closed:
                    break
                item = self._queue.popleft()
                self.current = item
                self._interrupt.clear()
            try:
                self._play(item)
            finally:
                with self._cond:
                    idle = not self._queue
                if idle:  # the video comes back before the voice reports itself idle
                    self._restore()
                with self._cond:
                    self.current = None
        self._restore()

    def _play(self, item: dict) -> None:
        if item["provider"] == "omnivoice":
            self._job_play(item)
            return
        base = {"id": item["id"], "chars": _chars(item), "voice": item["voice"], "source": item["source"],
                "provider": item["provider"], "model": item["model"]}
        if self._clock() > item["deadline"]:
            self.stats["expired"] += 1
            self.last = {**base, "status": "expired"}
            log.info("Bỏ câu TTS đã quá hạn (%s ký tự)", len(item["text"]))
            return
        try:
            intro_path = None
            if item.get("intro"):
                try:
                    intro_path = self._audio(item["intro"], item["voice"], item["rate"], item["provider"],
                                             item["model"], item["settings"])
                except Exception as exc:  # noqa: BLE001 - without the names the answer is still worth saying
                    base["intro_failed"] = True
                    log.warning("Không tạo được lời gọi tên TTS, chỉ đọc phần trả lời: %s", _reason(exc))
            path = self._audio(item["text"], item["voice"], item["rate"], item["provider"],
                               item["model"], item["settings"])
            if self._interrupt.is_set():
                return
            if self._clock() > item["deadline"]:
                self.stats["expired"] += 1
                self.last = {**base, "status": "expired"}
                return
            if item["duck"]:
                self._duck()
            if intro_path is not None:  # names first, then the answer, on the same OBS input
                started = self._clock()
                self._obs.play_file(item["source"], str(intro_path.resolve()), unmute=True)
                log.info("TTS OBS playback started provider=%s model=%s voice=%s intro=true",
                         item["provider"], item["model"], item["voice"])
                self._wait_end(item["source"], item["intro"], item["rate"], started)
                if self._interrupt.is_set():
                    self.last = {**base, "status": "stopped"}
                    return
            started = self._clock()
            media = self._obs.play_file(item["source"], str(path.resolve()), unmute=True)
            log.info("TTS OBS playback started provider=%s model=%s voice=%s queue_latency_ms=%s",
                     item["provider"], item["model"], item["voice"], int((started - item["queued_at"]) * 1000))
            self._wait_end(item["source"], item["text"], item["rate"], started)
            log.info("TTS OBS playback finished provider=%s model=%s voice=%s playback_ms=%s",
                     item["provider"], item["model"], item["voice"], int((self._clock() - started) * 1000))
            if self._interrupt.is_set():
                self.last = {**base, "status": "stopped"}
                return
            self.stats["spoken"] += 1
            self.last = {**base, "status": "spoken", "media_state": media.get("state"),
                         "duration_ms": int((self._clock() - item["queued_at"]) * 1000)}
            log.info("Đã đọc TTS (%s ký tự) qua nguồn OBS '%s'", len(item["text"]), item["source"])
        except AgentError as exc:
            self.stats["failed"] += 1
            self.last = {**base, "status": "failed", "code": exc.code, "message": exc.message}
            log.warning("TTS lỗi: %s", exc.message, extra={"code": exc.code})
        except Exception as exc:  # noqa: BLE001 - the worker must survive any single sentence
            self.stats["failed"] += 1
            self.last = {**base, "status": "failed", "code": "TTS_FAILED", "message": f"{type(exc).__name__}: {exc}"[:200]}
            log.exception("TTS lỗi không mong đợi")

    def _wait_end(self, source: str, text: str, rate: int, started: float) -> None:
        """Until OBS reports the file ended (or a time limit from its length), so the next one never overlaps."""
        words = len(text.split())
        estimate = words / (self.SYLLABLES_PER_SECOND * (1 + rate / 100)) + 1.5
        limit = started + min(self.MAX_SPEECH_SECONDS, max(3.0, estimate * 2))
        seen_playing = False
        while self._clock() < limit:
            if self._interrupt.wait(self.POLL_SECONDS):
                return
            try:
                status = self._obs.media_status(source)
            except AgentError:
                return
            state = status.get("state")
            if state == "OBS_MEDIA_STATE_PLAYING":
                seen_playing = True
                duration = status.get("duration_ms")
                if isinstance(duration, (int, float)) and duration > 0:
                    limit = min(limit, started + duration / 1000 + 1.5)
            elif state in ENDED_STATES and (seen_playing or self._clock() - started > 1.0):
                return

    # --- ducking -------------------------------------------------------------------------
    def _video_inputs(self) -> list[str]:
        cfg = self._config.get().obs
        names = [cfg.media_input]
        if cfg.dual_mode:
            names += [cfg.source_a.strip() or cfg.media_input + " A", cfg.source_b.strip() or cfg.media_input + " B"]
        return [n for n in dict.fromkeys(names) if n and n != cfg.voice_input]

    @property
    def _duck_file(self):
        return paths.voice / "ducked.json"

    def _duck(self) -> None:
        if self._ducked:
            return
        for name in self._video_inputs():
            try:
                level = self._obs.input_volume_db(name)
                self._obs.set_input_volume_db(name, level - self.DUCK_DB)
                self._ducked[name] = level
            except AgentError:
                continue  # input missing in this scene collection: nothing to lower
        if self._ducked:
            try:
                self._duck_file.write_text(json.dumps(self._ducked), encoding="utf-8")
            except OSError:
                pass

    def _restore(self) -> None:
        failed = {}
        for name, level in list(self._ducked.items()):
            try:
                self._obs.set_input_volume_db(name, level)
            except AgentError as exc:
                failed[name] = level
                log.warning("Không trả lại âm lượng video '%s': %s", name, exc.message)
        self._ducked.clear()
        try:
            if failed:  # OBS unreachable now: tried again the next time the voice starts
                self._duck_file.write_text(json.dumps(failed), encoding="utf-8")
            else:
                self._duck_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _restore_saved(self) -> None:
        """Levels saved by a voice that stopped mid-sentence (crash, restart): put them back first."""
        try:
            saved = json.loads(self._duck_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            self._ducked = {str(k): float(v) for k, v in saved.items() if isinstance(v, (int, float))}
            self._restore()


def _chars(item: dict) -> int:
    return len(item["text"]) + len(item.get("intro") or "")


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"[:200]


# Existing desktop and Cloud callers keep their public import during migration.
VoiceService = VoiceEngine

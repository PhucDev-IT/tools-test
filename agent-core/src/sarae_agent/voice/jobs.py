"""Persistent at-most-once acceptance and WAV playback on the existing voice worker."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
import wave
from contextlib import contextmanager
from datetime import datetime

from sarae_agent.config import paths
from sarae_agent.errors import AgentError


class JobVoiceMixin:
    server_offset = 0.0
    event_sink = None
    job_provider = None

    @contextmanager
    def _job_db(self):
        db = sqlite3.connect(paths.voice / "utterances.sqlite", timeout=15)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS utterances (id TEXT PRIMARY KEY, state TEXT, at REAL)")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _job_event(self, item, state, **detail):
        ref = item["tts"]
        with self._job_db() as db:
            db.execute("UPDATE utterances SET state=?, at=? WHERE id=?", (state, time.time(), ref["utterance_id"]))
        if self.event_sink:
            self.event_sink({"type": "tts_playback", "session_id": item.get("session_id"), "detail": {
                "utterance_id": ref["utterance_id"], "job_id": ref["job_id"],
                "command_id": item.get("command_id"), "provider": "omnivoice", "status": state,
                "state": state, **detail}})

    def _job_check(self, item, interrupt=True):
        if self._closed or item.get("generation", 0) != getattr(self, "_job_generation", 0) or (interrupt and self._interrupt.is_set()):
            raise AgentError("TTS_CANCELLED", "Đã dừng giọng nói")
        if self._clock() > item["deadline"]:
            raise AgentError("TTS_EXPIRED", "Câu đã hết hạn")

    def _job_item(self, ref, **fields):
        deadline = ref.get("deadline_at")
        remaining = 900.0
        if deadline:
            remaining = datetime.fromisoformat(deadline).timestamp() - (time.time() + self.server_offset)
        now = self._clock()
        return {"id": uuid.uuid4().hex[:12], "tts": ref, "queued_at": now,
                "deadline": now + min(remaining, fields.pop("ttl_seconds", None) or 900),
                "generation": getattr(self, "_job_generation", 0), **fields}

    def _job_speak(self, text, voice, rate, ttl_seconds, duck, intro, tts, command_id, session_id, fallback):
        if not tts:
            raise AgentError("INVALID_PARAMS", "Thiếu job reference")
        text = self._check(text)
        item = self._job_item(tts, text=text, voice=voice, rate=rate, ttl_seconds=ttl_seconds or self.DEFAULT_TTL,
                              duck=duck, intro=intro, source=self._config.get().obs.voice_input,
                              provider="omnivoice", model="demo_default", command_id=command_id,
                              session_id=session_id, fallback=fallback)
        with self._cond:
            if self._closed:
                raise AgentError("TTS_UNAVAILABLE", "Voice engine đã dừng")
            if len(self._queue) >= self.MAX_QUEUE:
                raise AgentError("TTS_QUEUE_FULL", "Hàng đợi giọng nói đầy")
            with self._job_db() as db:
                # Keep all acceptance tombstones: restart must never replay an uncertain utterance.
                inserted = db.execute("INSERT OR IGNORE INTO utterances VALUES (?, 'accepted', ?)",
                                      (tts["utterance_id"], time.time())).rowcount
                if inserted and tts.get("intro"):
                    fresh_intro = db.execute("INSERT OR IGNORE INTO utterances VALUES (?, 'accepted', ?)",
                        (tts["intro"]["utterance_id"], time.time())).rowcount
                    if not fresh_intro:
                        item["intro"] = None
            if not inserted:
                if self.event_sink:
                    self.event_sink({"type": "tts_playback", "session_id": session_id, "detail": {"utterance_id": tts["utterance_id"], "job_id": tts["job_id"], "status": "duplicate", "command_id": command_id}})
                return {"status": "duplicate", "utterance_id": tts["utterance_id"]}
            self._job_event(item, "accepted")
            if item["deadline"] <= self._clock():
                self._job_event(item, "expired", error_code="TTS_EXPIRED")
                return {"status": "expired"}
            self._queue.append(item)
            self._ensure_worker()
            self._cond.notify_all()
        return {"status": "queued", "utterance_id": tts["utterance_id"]}

    def _job_audio(self, item, ref, interrupt=True, text=None):
        self._job_check(item, interrupt)
        # Job identity prevents cross-owner/revision reuse; voice-server owns content result caching.
        key = hashlib.sha256(json.dumps(["omnivoice", item["voice"], ref["utterance_id"], ref["job_id"], "demo_default"], sort_keys=True).encode()).hexdigest()
        target = self._cache_dir / (key + ".wav")
        if target.is_file():
            try:
                with wave.open(str(target)) as cached:
                    frames = cached.getnframes()
                    if (cached.getnchannels(), cached.getsampwidth(), cached.getframerate(), cached.getcomptype()) != (1, 2, 24000, "NONE") or not frames or len(cached.readframes(frames)) != frames * 2:
                        raise ValueError("invalid cached WAV")
                return target, {"cache_hit": True, "audio_path": "cache", "wait_audio_ms": 0,
                                "download_ms": 0, "duration_ms": frames / 24}
            except (wave.Error, EOFError, ValueError):
                target.unlink(missing_ok=True)
        if self.job_provider is None:
            raise AgentError("TTS_NOT_READY", "Chưa kết nối provider OmniVoice")
        temp = target.with_name("tmp_" + uuid.uuid4().hex + ".wav")
        try:
            try:
                info = self.job_provider.download(ref, temp, lambda: self._job_check(item, interrupt),
                                                   item["tts"].get("audio_path", "relay"))
            except AgentError as exc:
                policy = item.get("fallback") or {}
                if exc.code not in ("TTS_BACKEND_UNREACHABLE", "TTS_NOT_READY", "TTS_BUSY", "TTS_INFERENCE_FAILED") or policy.get("mode") != "provider":
                    raise
                self._job_check(item, interrupt)
                spec = policy["provider"]
                clip = self._audio(text or item["text"], spec["voice"], spec.get("rate", 0), spec["provider"], spec.get("model"), spec.get("settings"))
                self._job_check(item, interrupt)
                return clip, {"audio_path": "cache", "cache_hit": False, "fallback_used": spec["provider"]}
            self._job_check(item, interrupt)
            with self._cond:
                self._job_check(item, interrupt)
                os.replace(temp, target)
            self._prune_cache()
            return target, info
        finally:
            temp.unlink(missing_ok=True)

    def _job_play(self, item):
        try:
            self._job_check(item)
            self._job_event(item, "waiting_audio")
            intro = None
            intro_info = {}
            intro_failed = None
            if item.get("intro") and item["tts"].get("intro"):
                try:
                    intro, intro_info = self._job_audio(item, item["tts"]["intro"], text=item["intro"])
                except AgentError as exc:
                    if exc.code in ("TTS_EXPIRED", "TTS_CANCELLED"):
                        raise
                    intro_failed = exc.code
            audio, info = self._job_audio(item, item["tts"])
            self._job_event(item, "ready", **info, intro_failed=intro_failed)
            self._job_check(item)
            if item["duck"]:
                self._duck()
            for path, clip_ref, clip_info in ([(intro, item["tts"]["intro"], intro_info)] if intro else []) + [(audio, item["tts"], info)]:
                self._job_check(item)
                clip_item = {**item, "tts": clip_ref}
                started = self._clock()
                with self._cond:
                    # Serialize the final check/start with clear's generation change.
                    self._job_check(item)
                    initial = self._obs.play_file(item["source"], str(path.resolve()), unmute=True)
                seen = initial.get("state") == "OBS_MEDIA_STATE_PLAYING"
                if seen:
                    self._job_event(clip_item, "playing", **clip_info,
                        playback_start_ms=int((self._clock()-item["queued_at"])*1000),
                        obs_startup_ms=int((self._clock()-started)*1000),
                        observation="OBS_MEDIA_STATE_PLAYING")
                while self._clock() - started < self.MAX_SPEECH_SECONDS:
                    if self._interrupt.wait(self.POLL_SECONDS):
                        raise AgentError("TTS_CANCELLED", "Đã dừng giọng nói")
                    state = self._obs.media_status(item["source"])
                    if state.get("state") == "OBS_MEDIA_STATE_PLAYING" and not seen:
                        seen = True
                        self._job_event(clip_item, "playing", **clip_info,
                            playback_start_ms=int((self._clock()-item["queued_at"])*1000),
                            obs_startup_ms=int((self._clock()-started)*1000),
                            observation="OBS_MEDIA_STATE_PLAYING")
                    elif state.get("state") == "OBS_MEDIA_STATE_STOPPED" and seen:
                        raise AgentError("TTS_CANCELLED", "OBS đã dừng phát")
                    elif state.get("state") == "OBS_MEDIA_STATE_ENDED" and seen:
                        break
                else:
                    self._obs.media_action(item["source"], "stop")
                    raise AgentError("OBS_PLAYBACK_TIMEOUT", "OBS không xác nhận kết thúc phát")
                if clip_ref is not item["tts"]:
                    self._job_event(clip_item, "played", **clip_info, observation="OBS media state")
            self._job_event(item, "played", **info, observation="OBS media state")
            self.stats["spoken"] += 1
            self.last = {"status": "spoken", "provider": "omnivoice"}
        except AgentError as exc:
            state = "expired" if exc.code == "TTS_EXPIRED" else "cancelled" if exc.code == "TTS_CANCELLED" else "failed"
            self._job_event(item, state, error_code=exc.code)
            self.last = {"status": state, "code": exc.code, "provider": "omnivoice"}
        except Exception:
            self._job_event(item, "failed", error_code="TTS_FAILED")

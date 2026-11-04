"""Voice queue: one sentence at a time, stale ones dropped, audio cached, video lowered while speaking and restored."""
import json
import threading
import time

import pytest

from sarae_agent.config import ConfigStore, paths
from sarae_agent.errors import AgentError
from sarae_agent.obs.controller import ObsController
from sarae_agent.voice.tts import VoiceService

from fake_obs import FakeWs

VIDEO, VOICE = "Sarae Video", "Sarae Voice"


class EndingWs(FakeWs):
    """A media input plays for two status polls after a restart, then reports ENDED."""

    def __init__(self):
        super().__init__(inputs=(VIDEO, VOICE))
        self.polls_left: dict = {}
        self.volume_log: list = []
        self.played: list = []

    def send(self, request, data=None, raw=False):
        data = data or {}
        name = data.get("inputName")
        if request == "TriggerMediaInputAction" and data["mediaAction"].endswith("RESTART"):
            self.polls_left[name] = 2
            self.played.append(self.settings.get(name, {}).get("local_file"))
        if request == "GetMediaInputStatus" and name == VOICE:
            self.calls.append(request)
            if self.polls_left.get(name, 0) > 0:
                self.polls_left[name] -= 1
                return {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaDuration": 400, "mediaCursor": 0}
            return {"mediaState": "OBS_MEDIA_STATE_ENDED", "mediaDuration": 400, "mediaCursor": 400}
        if request == "SetInputVolume":
            self.volume_log.append((name, data["inputVolumeDb"]))
        return super().send(request, data, raw)


class Synth:
    def __init__(self):
        self.calls: list = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail: set = set()

    def __call__(self, text, voice, output, rate):
        self.calls.append((text, voice, rate))
        assert self.gate.wait(5)
        if text in self.fail:
            raise AgentError("TTS_FAILED", "fake failure", retryable=True)
        with open(output, "wb") as f:
            f.write(b"ID3fake-mp3")


@pytest.fixture()
def setup(home, events):
    ws = EndingWs()
    obs = ObsController(ConfigStore(), events, client_factory=lambda cfg, pw: ws)
    obs.RECONNECT_MIN_SECONDS = 0
    obs.connect()
    synth = Synth()
    clock = {"now": 1000.0}
    voice = VoiceService(ConfigStore(), obs, synthesize=synth, clock=lambda: clock["now"])
    voice.POLL_SECONDS = 0.01
    yield voice, ws, synth, clock
    voice.close()


def wait_idle(voice, done, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = voice.status()
        if status["queue"] == 0 and status["speaking"] is None and done(status):
            return status
        time.sleep(0.01)
    raise AssertionError(f"voice not idle: {voice.status()}")


def test_sentences_play_in_order_with_cache_and_ducking(setup):
    voice, ws, synth, _ = setup
    ws.volume[VIDEO] = -3.0
    first = voice.speak("Lan ơi, mẫu này hai trăm nghìn nha.", rate=5)
    assert first["status"] == "queued" and first["source"] == VOICE
    voice.speak("Năm mươi lăm ký mặc em là vừa.", rate=5)
    voice.speak("Lan ơi, mẫu này hai trăm nghìn nha.", rate=5)
    status = wait_idle(voice, lambda s: s["stats"]["spoken"] == 3)
    assert [c[0] for c in synth.calls] == ["Lan ơi, mẫu này hai trăm nghìn nha.", "Năm mươi lăm ký mặc em là vừa."]
    assert synth.calls[0][2] == "+5%"
    assert status["stats"]["cache_hits"] == 1, "the repeated sentence is not generated again"
    assert ws.played[0] == ws.played[2] and ws.played[0] != ws.played[1]
    # Lowered once for the whole run of sentences, restored to the exact level after.
    assert ws.volume_log == [(VIDEO, -15.0), (VIDEO, -3.0)]
    assert ws.volume[VIDEO] == -3.0
    assert not (paths.voice / "ducked.json").exists()


def test_stale_sentence_is_dropped_not_spoken_late(setup):
    voice, ws, synth, clock = setup
    synth.gate.clear()
    voice.speak("Câu đầu tiên đang tạo âm thanh.")
    voice.speak("Câu hỏi này sẽ quá hạn.", ttl_seconds=5)
    clock["now"] += 10
    synth.gate.set()
    status = wait_idle(voice, lambda s: s["stats"]["spoken"] + s["stats"]["expired"] == 2)
    assert status["stats"] == {**status["stats"], "spoken": 1, "expired": 1}
    assert [c[0] for c in synth.calls] == ["Câu đầu tiên đang tạo âm thanh."]


def test_queue_is_bounded(setup):
    voice, _, synth, _ = setup
    synth.gate.clear()
    voice.speak("Một.")
    deadline = time.monotonic() + 2
    while voice.status()["speaking"] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    for i in range(voice.MAX_QUEUE):
        voice.speak(f"Câu số {i}.")
    with pytest.raises(AgentError) as exc:
        voice.speak("Câu thừa.")
    assert exc.value.code == "TTS_QUEUE_FULL"
    synth.gate.set()


def test_clear_empties_the_queue_and_restores_volume(setup):
    voice, ws, synth, _ = setup
    synth.gate.clear()
    voice.speak("Một.")
    voice.speak("Hai.")
    voice.speak("Ba.")
    deadline = time.monotonic() + 2
    while voice.status()["speaking"] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    result = voice.clear()
    assert result["cleared"] == 2
    synth.gate.set()
    status = wait_idle(voice, lambda s: True)
    assert status["stats"]["spoken"] == 0 and status["stats"]["cleared"] == 2
    assert ws.volume.get(VIDEO, 0.0) == 0.0 and not status["ducked"]


def test_volume_left_lowered_by_a_crash_is_restored_first(setup):
    voice, ws, _, _ = setup
    ws.volume[VIDEO] = -12.0
    paths.voice.mkdir(parents=True, exist_ok=True)
    (paths.voice / "ducked.json").write_text(json.dumps({VIDEO: 0.0}), encoding="utf-8")
    voice.speak("Xin chào.", duck=False)
    wait_idle(voice, lambda s: s["stats"]["spoken"] == 1)
    assert ws.volume[VIDEO] == 0.0
    assert not (paths.voice / "ducked.json").exists()


def test_invalid_input_is_rejected(setup):
    voice, *_ = setup
    for kwargs in ({"text": ""}, {"text": "x" * 451}, {"text": "hi", "voice": "en-US"}, {"text": "hi", "rate": 80}):
        with pytest.raises(AgentError):
            voice.speak(**kwargs)
    with pytest.raises(AgentError):
        voice.speak("hi", intro="x" * 121)


ANSWER = "Mẫu này hai trăm nghìn nha."


def test_queued_edge_voice_keeps_its_snapshot(setup):
    voice, ws, synth, _ = setup
    synth.gate.clear()
    voice.speak('Câu đầu đang chờ.', voice='vi-VN-HoaiMyNeural', rate=0)
    voice.speak('Câu tiếp theo.', voice='vi-VN-NamMinhNeural', rate=10)
    synth.gate.set()
    status = wait_idle(voice, lambda s: s['stats']['spoken'] == 2)
    assert synth.calls == [('Câu đầu đang chờ.', 'vi-VN-HoaiMyNeural', '+0%'),
                           ('Câu tiếp theo.', 'vi-VN-NamMinhNeural', '+10%')]
    assert status['last']['provider'] == 'edge'
    assert 'elevenlabs' not in status['capabilities']['providers']
    assert len(ws.played) == 2


def test_intro_then_body_and_body_cached_across_viewers(setup):
    voice, ws, synth, _ = setup
    first = voice.speak(ANSWER, intro="Lan với Hoa ơi,")
    assert first["chars"] == len(ANSWER) + len("Lan với Hoa ơi,")
    voice.speak(ANSWER, intro="Mai ơi,")
    status = wait_idle(voice, lambda s: s["stats"]["spoken"] == 2)
    assert [c[0] for c in synth.calls] == ["Lan với Hoa ơi,", ANSWER, "Mai ơi,"], "the body is generated once"
    assert len(ws.played) == 4
    intro1, body1, intro2, body2 = ws.played
    assert body1 == body2 and len({intro1, intro2, body1}) == 3
    assert status["stats"]["cache_hits"] == 1
    assert status["last"]["chars"] == len(ANSWER) + len("Mai ơi,")


def test_intro_failure_still_speaks_the_body(setup):
    voice, ws, synth, _ = setup
    synth.fail.add("Lan ơi,")
    voice.speak(ANSWER, intro="Lan ơi,")
    status = wait_idle(voice, lambda s: s["stats"]["spoken"] == 1)
    assert len(ws.played) == 1 and status["stats"]["failed"] == 0
    assert status["last"]["status"] == "spoken" and status["last"]["intro_failed"] is True


def wait_prepared(voice, count, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = voice.status()
        if status["prepare_pending"] == 0 and status["stats"]["prepared"] + status["stats"]["prepare_failed"] >= count:
            return status
        time.sleep(0.01)
    raise AssertionError(f"prepare not done: {voice.status()}")


def test_prepare_returns_at_once_and_fills_the_cache(setup):
    voice, ws, synth, _ = setup
    synth.gate.clear()
    started = time.monotonic()
    result = voice.prepare([ANSWER, "  Năm mươi   lăm ký.  ", "Câu lỗi."], rate=5)
    assert time.monotonic() - started < 0.5 and result == {"accepted": 3, "already_cached": 0}
    synth.fail.add("Câu lỗi.")
    synth.gate.set()
    status = wait_prepared(voice, 3)
    assert status["stats"]["prepared"] == 2 and status["stats"]["prepare_failed"] == 1
    assert ws.played == [], "prepare never plays"
    voice.speak("Năm mươi lăm ký.", rate=5)
    status = wait_idle(voice, lambda s: s["stats"]["spoken"] == 1)
    assert status["stats"]["cache_hits"] == 1 and len(synth.calls) == 3, "spoken from the prepared cache"


def test_prepare_skips_cached_texts(setup):
    voice, _, synth, _ = setup
    voice.prepare([ANSWER])
    wait_prepared(voice, 1)
    assert voice.prepare([ANSWER, "Câu mới."]) == {"accepted": 1, "already_cached": 1}
    wait_prepared(voice, 2)
    assert [c[0] for c in synth.calls] == [ANSWER, "Câu mới."]


def test_prepare_pending_is_bounded(setup):
    voice, _, synth, _ = setup
    voice.MAX_PREPARE_PENDING = 5
    synth.gate.clear()
    voice.prepare(["Câu đầu."])
    deadline = time.monotonic() + 2
    while not synth.calls and time.monotonic() < deadline:  # the thread is busy with the first text
        time.sleep(0.01)
    result = voice.prepare([f"Câu số {i}." for i in range(8)])
    assert result["accepted"] == 8 and voice.status()["prepare_pending"] == 5
    synth.gate.set()
    wait_prepared(voice, 6)
    assert [c[0] for c in synth.calls] == ["Câu đầu."] + [f"Câu số {i}." for i in range(3, 8)], "oldest dropped"
    with pytest.raises(AgentError):
        voice.prepare([])

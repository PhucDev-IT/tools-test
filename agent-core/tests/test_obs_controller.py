import pytest

from sarae_agent.config import ConfigStore
from sarae_agent.errors import AgentError
from sarae_agent.obs.controller import ObsController

from fake_obs import FakeWs


def controller(home, events, factory):
    obs = ObsController(ConfigStore(), events, client_factory=factory)
    obs.RECONNECT_MIN_SECONDS = 0
    return obs


def test_obs_connect_play_and_verify(home, events):
    ws = FakeWs()
    obs = controller(home, events, lambda cfg, pw: ws)
    assert obs.connect()["obs_version"] == "31.0.0"
    status = obs.play_file("Sarae Video", "C:\\v\\a.mp4")
    assert status["state"] == "OBS_MEDIA_STATE_PLAYING"
    assert ws.settings["Sarae Video"]["local_file"] == "C:\\v\\a.mp4"
    assert "TriggerMediaInputAction" in ws.calls
    assert any(name == "obs.status_changed" and d["connected"] for name, d in events.captured)


def test_obs_errors_are_typed(home, events):
    obs = controller(home, events, lambda cfg, pw: FakeWs(apply=False))
    obs.connect()
    with pytest.raises(AgentError) as exc:
        obs.play_file("Missing Input", "C:\\a.mp4")
    assert exc.value.code == "OBS_INPUT_MISSING"
    with pytest.raises(AgentError) as exc:
        obs.play_file("Sarae Video", "C:\\a.mp4")
    assert exc.value.code == "OBS_NOT_APPLIED"  # OBS answered but did not take the file


@pytest.mark.parametrize("error,code", [
    (ConnectionRefusedError("[WinError 10061] refused"), "OBS_NOT_RUNNING"),
    (Exception("authentication enabled but no password provided"), "OBS_AUTH_FAILED"),
    (TimeoutError("timed out"), "OBS_TIMEOUT"),
])
def test_obs_connect_failures(home, events, error, code):
    def factory(cfg, pw):
        raise error

    obs = controller(home, events, factory)
    with pytest.raises(AgentError) as exc:
        obs.connect()
    assert exc.value.code == code
    assert obs.status()["connected"] is False


def test_obs_drop_and_reconnect(home, events):
    sockets = [FakeWs(), FakeWs()]
    obs = controller(home, events, lambda cfg, pw: sockets.pop(0))
    obs.connect()
    obs._client.broken = True
    with pytest.raises(AgentError) as exc:
        obs.media_status("Sarae Video")
    assert exc.value.code == "OBS_DISCONNECTED" and not obs.status()["connected"]
    assert obs.media_status("Sarae Video")["state"] == "OBS_MEDIA_STATE_PLAYING"  # reconnected
    assert obs.status()["connected"]

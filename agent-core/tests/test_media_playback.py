"""Playback of Media Library videos (32-hex ids) from the Agent cloud media cache."""
import json

import pytest

from conftest import windows_only
from fake_obs import FakeWs
from sarae_agent.config import ConfigStore, paths
from sarae_agent.errors import AgentError
from sarae_agent.media.cloud_cache import cache_dir, cached_path
from sarae_agent.obs.controller import ObsController, ObsSettingsStore
from sarae_agent.playback.engine import PlaybackEngine
from sarae_agent.playback.service import PlaybackService

ENDED = "OBS_MEDIA_STATE_ENDED"
A, B, C = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture()
def cache(home, tmp_path):
    """A published cloud media cache: <id>.mp4 / <id>.mov, as CloudMediaCache.prepare leaves it."""
    root = tmp_path / "cloud-media-cache" / "agt_1"
    root.mkdir(parents=True)
    for name in (f"{A}.mp4", f"{B}.mp4", f"{C}.mov"):
        (root / name).write_bytes(b"\x00" * 16)
    return root


def test_cached_path_finds_published_files_only(cache):
    assert cached_path(cache, A) == cache / f"{A}.mp4"
    assert cached_path(cache, C) == cache / f"{C}.mov"
    partial = "d" * 32
    (cache / f"{partial}.mp4.part").write_bytes(b"\x00")
    assert cached_path(cache, partial) is None  # an unfinished download never plays
    assert cached_path(cache, "e" * 32) is None  # not downloaded
    for bad in ("a" * 64, "A" * 32, "../" + "a" * 29, "", None):
        assert cached_path(cache, bad) is None  # 64-hex legacy local ids and junk are rejected


def test_cache_dir_is_per_agent_and_rejects_paths(home):
    assert cache_dir("agt_1") == paths.root / "cloud-media-cache" / "agt_1"
    for bad in ("", "..", "a/b", "a\\b"):
        with pytest.raises(AgentError):
            cache_dir(bad)


# --- playback: ported PlaybackEngine on the Core's OBS connection ------------------------------
@pytest.fixture()
def player(cache, events, monkeypatch):
    from sarae_agent.playback import engine as playback_engine
    monkeypatch.setattr(playback_engine, "probe_media", lambda path: type("Info", (), {"rotation": 0})())
    monkeypatch.setattr(playback_engine, "obs_file", lambda path, info, canvas=None: path)
    store = ConfigStore()
    resolve = lambda media_id: cached_path(cache, media_id)  # noqa: E731
    ws = FakeWs()
    connections = []

    def factory(cfg, password):
        connections.append((cfg.host, cfg.port, password))
        return ws

    obs = ObsController(store, events, client_factory=factory)
    obs.RECONNECT_MIN_SECONDS = 0
    engine = PlaybackEngine(resolve=resolve, client_factory=obs.requests_for, store=ObsSettingsStore(store, obs))
    service = PlaybackService(engine, events)
    return service, engine, ws, resolve, cache, connections


def test_playlist_plays_and_auto_advances_over_one_connection(player):
    service, engine, ws, _, cache, connections = player
    status = service.play_playlist([A, B], loop=False)
    assert status["active"] and status["current_media_id"] == A and status["count"] == 2
    assert status["queue_ids"] == [A, B]
    assert ws.settings["Sarae Video"]["local_file"] == str((cache / f"{A}.mp4").resolve())
    ws.media_state["Sarae Video"] = ENDED
    engine.tick()
    assert service.status()["current_media_id"] == B
    ws.media_state["Sarae Video"] = ENDED
    engine.tick()
    assert service.status()["active"] is False  # last video ended without loop
    assert len(connections) == 1  # engine requests reuse the Core's single obs-websocket


def test_operator_controls_and_loop(player):
    service, engine, ws, _, _, _ = player
    service.play_playlist([A, B], loop=True)
    service.next()
    service.next()
    assert service.status()["index"] == 0  # wrapped
    service.previous()
    assert service.status()["index"] == 1
    assert service.pause()["paused"] and ws.media_state["Sarae Video"] == "OBS_MEDIA_STATE_PAUSED"
    assert not service.resume()["paused"]
    assert service.set_loop(False)["loop"] is False
    with pytest.raises(AgentError) as exc:
        service.play(C)  # another playlist while one runs: stop first
    assert exc.value.code == "PLAYBACK_CONFLICT"
    assert service.stop()["active"] is False


def test_no_fake_success(player):
    service, engine, ws, _, cache, _ = player
    with pytest.raises(AgentError) as exc:
        service.play("f" * 32)  # not downloaded to the cache
    assert exc.value.code == "PLAYBACK_CONFLICT" and not service.status()["active"]
    assert "cache" in exc.value.message
    (cache / f"{A}.mp4").unlink()
    with pytest.raises(AgentError):
        service.play_playlist([A, B])
    ws.broken = True
    with pytest.raises(AgentError) as exc:
        service.play(B)
    assert exc.value.code == "OBS_UNAVAILABLE"
    status = service.status()
    assert status["active"] is False and status["connected"] is False and status["last_error"]


def test_product_switch_event_reaches_callback(player):
    service, engine, ws, _, _, _ = player
    seen = []
    engine.on_event = seen.append
    engine.product_play(7, 1, [A], "immediate")
    assert engine.product_play(7, 2, [C], "after_current") == {"ok": True, "pending_product_id": 2}
    ws.media_state["Sarae Video"] = ENDED
    engine.tick()
    assert seen[-1]["type"] == "PRODUCT_SWITCHED" and seen[-1]["product_id"] == 2 and seen[-1]["session_id"] == 7
    assert service.status()["current_media_id"] == C  # .mov from the cache


def test_product_without_cached_video_says_cache(player):
    _, engine, _, _, _, _ = player
    with pytest.raises(AgentError) as exc:
        engine.product_play(7, 3, ["e" * 32], "immediate")
    assert "chưa tải về cache" in exc.value.message


def test_restart_requires_manual_resume(player, events):
    service, engine, ws, resolve, _, _ = player
    service.play(B)
    again = PlaybackService(engine.__class__(resolve=resolve, client_factory=engine.client_factory,
                                             store=engine.store), events)
    assert again.status()["needs_manual_resume"] is True and again.status()["active"] is False


@windows_only
def test_obs_configure_keeps_password_in_dpapi_store(player):
    service, engine, ws, _, _, connections = player
    status = engine.configure(host="127.0.0.1", port=4456, password="s3cret", input_name="Sarae Video",
                              dual_mode=False)
    assert "s3cret" not in json.dumps(status) and status["obs"]["has_password"] is True
    saved = json.loads(paths.config_file.read_text(encoding="utf-8"))["obs"]
    assert saved["port"] == 4456 and "s3cret" not in json.dumps(saved)
    engine.status()
    assert connections[-1] == ("127.0.0.1", 4456, "s3cret")  # reconnected with the new settings
    engine.configure(host="127.0.0.1", port=4456, password="", input_name="Sarae Video")  # empty keeps it
    assert engine.store.load().password == "s3cret"
    with pytest.raises(AgentError) as exc:
        engine.configure(host="10.0.0.5", port=4455)
    assert exc.value.code == "INVALID_REQUEST"  # OBS must run on the Agent PC
    inputs = engine.list_inputs("127.0.0.1", 4457, "other")  # unsaved values: temporary connection
    assert inputs["inputs"][0]["name"] == "Sarae Video" and connections[-1] == ("127.0.0.1", 4457, "other")
    assert engine.store.load().port == 4456

import json

import pytest

from sarae_agent.config import ConfigError, ConfigStore, normalize_cloud_url, paths, read_json, write_json
from sarae_agent.ipc import protocol


def test_encode_is_single_ascii_line():
    frame = protocol.encode(protocol.event("log.entry", {"message": "Đã kết nối\nOBS"}))
    assert frame.endswith(b"\n") and frame.count(b"\n") == 1
    frame.decode("ascii")
    assert json.loads(frame)["data"]["message"] == "Đã kết nối\nOBS"


@pytest.mark.parametrize("line,code", [
    (b"not json", "INVALID_JSON"),
    (b"[1,2]", "INVALID_FRAME"),
    (b'{"v":2,"request_id":"a","method":"system.status"}', "UNSUPPORTED_VERSION"),
    (b'{"method":"system.status"}', "INVALID_REQUEST_ID"),
    (b'{"request_id":"a","method":"rm -rf"}', "METHOD_NOT_FOUND"),
    (b'{"request_id":"a","method":"system.status","params":[1]}', "INVALID_PARAMS"),
    (b'{"type":"event","request_id":"a","method":"system.status"}', "INVALID_FRAME"),
])
def test_parse_request_rejects(line, code):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(line)
    assert exc.value.code == code


def test_parse_request_ok_with_bom():
    request = protocol.parse_request('\ufeff{"v":1,"request_id":"r-1","method":"playback.play","params":{"media_id":"x"}}'.encode())
    assert (request.request_id, request.method, request.params) == ("r-1", "playback.play", {"media_id": "x"})


def test_oversized_frame_rejected():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(b"{" + b" " * (protocol.MAX_FRAME_BYTES + 1) + b"}")
    assert exc.value.code == "FRAME_TOO_LARGE"


@pytest.mark.parametrize("raw,expected", [
    ("https://sarae.vn", "https://sarae.vn/api"),
    ("https://sarae.vn/api/", "https://sarae.vn/api"),
    ("https://sarae.vn:8443/saas/api", "https://sarae.vn:8443/saas/api"),
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000/api"),
    ("http://localhost:8000/api", "http://localhost:8000/api"),
    ("", ""),
])
def test_normalize_cloud_url(raw, expected):
    assert normalize_cloud_url(raw) == expected


@pytest.mark.parametrize("raw", ["http://sarae.vn/api", "ftp://x/api", "https://u:p@sarae.vn/api",
                                 "https://sarae.vn/other", "https://sarae.vn/api?x=1"])
def test_normalize_cloud_url_rejects(raw):
    with pytest.raises(ConfigError):
        normalize_cloud_url(raw)


def test_config_defaults_persist_and_validate(home):
    store = ConfigStore()
    assert store.get().monitor.interval_seconds == 240
    assert paths.config_file.is_file()
    assert str(paths.root).startswith(str(home))
    updated = store.update({"obs": {"port": 4460}, "cloud": {"api_url": "https://cloud.example/api/"}})
    assert updated.obs.port == 4460 and updated.cloud.api_url == "https://cloud.example/api"
    with pytest.raises(ConfigError):
        store.update({"obs": {"port": 70000}})
    with pytest.raises(ConfigError):
        store.update({"unknown": {}})
    assert ConfigStore().get().obs.port == 4460  # reloaded from disk, invalid write not persisted


def test_monitor_interval_migrates_and_rejects_out_of_range(home):
    write_json(paths.config_file, {"version": 2, "monitor": {"interval_seconds": 2.0,
                                                        "telegram_chat_id": "-100777"}})
    store = ConfigStore()
    assert store.get().monitor.interval_seconds == 240
    assert "telegram_chat_id" not in store.get().monitor.model_dump()
    assert read_json(paths.config_file)["monitor"]["interval_seconds"] == 240
    with pytest.raises(ConfigError):
        store.update({"monitor": {"interval_seconds": 179}})
    with pytest.raises(ConfigError):
        store.update({"monitor": {"interval_seconds": 241}})


def test_config_migrates_legacy_agent_files(home):
    legacy = paths.legacy_data
    legacy.mkdir(parents=True)
    write_json(legacy / "agent_gui_settings.json", {"cloud_url": "https://old.example/api"})
    write_json(legacy / "agent_media_folders.json", ["D:\\Videos"])  # retired: ignored
    config = ConfigStore().get()
    assert config.version == 2
    assert config.cloud.api_url == "https://old.example/api"
    assert "media" not in config.model_dump()


def test_config_with_retired_media_section_still_loads(home):
    write_json(paths.config_file, {"version": 2, "obs": {"port": 4461},
                                   "media": {"folders": ["D:\\Videos"], "rescan_minutes": 10}})
    config = ConfigStore().get()
    assert config.obs.port == 4461
    assert "media" not in config.model_dump()
    assert not paths.config_file.with_suffix(".invalid.json").exists()


def test_corrupt_config_is_backed_up(home):
    paths.config_file.write_text("{broken", encoding="utf-8")
    config = ConfigStore().get()
    assert config.version == 2
    write_json(paths.config_file, {"version": 2, "obs": {"port": "not-a-port"}})
    ConfigStore()
    assert paths.config_file.with_suffix(".invalid.json").exists()

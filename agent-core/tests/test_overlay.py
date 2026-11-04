"""LIVE overlays on OBS (Phase 3): the Browser Source this Agent owns, the local renderer server, the image cache and
the READY handshake. OBS is a stateful fake speaking the obs-websocket requests the overlay uses."""
import hashlib
import http.client
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from sarae_agent.errors import AgentError
from sarae_agent.overlay.cache import OverlayCache
from sarae_agent.overlay.obs_source import ObsOverlaySource, ours_by_url
from sarae_agent.overlay.schedule import random_plan
from sarae_agent.overlay.server import OverlayServer
from sarae_agent.overlay.service import OverlayService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
VIDEO = "Sarae Video"
WRITES = {"SetInputSettings", "CreateInput", "CreateSceneItem", "SetSceneItemTransform", "SetSceneItemIndex",
          "SetSceneItemEnabled", "RemoveInput", "RemoveSceneItem", "SetVideoSettings", "SetProfileParameter",
          "SetCurrentSceneCollection", "SetCurrentProgramScene"}


class FakeObs:
    """Scenes, inputs and scene items of one OBS, with transforms read back the way OBS reports them."""

    def __init__(self, width=1080, height=1920):
        self.video = {"baseWidth": width, "baseHeight": height, "outputWidth": width, "outputHeight": height, "fpsNumerator": 30}
        self.collections = {"Main": self._collection()}
        self.current = "Main"
        self.requests: list[tuple[str, dict]] = []
        self.connected = True
        self.streaming = False
        self.next_item = 100

    def _collection(self):
        video = str(uuid.uuid4())
        return {"inputs": {video: {"name": VIDEO, "kind": "ffmpeg_source", "settings": {}}},
                "scenes": {"LIVE": [self._item(1, VIDEO, video, 0)], "Other": []}}

    def _item(self, item_id, name, source_uuid, index, enabled=True):
        return {"sceneItemId": item_id, "sourceName": name, "sourceUuid": source_uuid, "sceneItemIndex": index,
                "sceneItemEnabled": enabled, "transform": {"positionX": 0.0, "positionY": 0.0, "rotation": 0.0, "scaleX": 1.0,
                                                           "scaleY": 1.0, "cropLeft": 0, "cropRight": 0, "cropTop": 0,
                                                           "cropBottom": 0, "boundsType": "OBS_BOUNDS_NONE", "alignment": 5}}

    @property
    def col(self):
        return self.collections[self.current]

    def item(self, scene, item_id):
        for i in self.col["scenes"][scene]:
            if i["sceneItemId"] == item_id:
                return i
        raise AgentError("OBS_REQUEST_FAILED", "missing item", {"obs_code": 600})

    def add_input(self, scene, name, kind, settings):
        source = str(uuid.uuid4())
        self.col["inputs"][source] = {"name": name, "kind": kind, "settings": dict(settings)}
        self.next_item += 1
        items = self.col["scenes"][scene]
        items.append(self._item(self.next_item, name, source, len(items)))
        return source, self.next_item

    def __call__(self, request, data=None):
        if not self.connected:
            raise AgentError("OBS_DISCONNECTED", "Mất kết nối OBS")
        data = data or {}
        self.requests.append((request, data))
        col = self.col
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": self.current, "sceneCollections": list(self.collections)}
        if request == "GetVideoSettings":
            return dict(self.video)
        if request == "GetInputList":
            return {"inputs": [{"inputName": v["name"], "inputUuid": k, "inputKind": v["kind"]} for k, v in col["inputs"].items()
                               if "inputKind" not in data or v["kind"] == data["inputKind"]]}
        if request == "GetInputSettings":
            return {"inputSettings": dict(col["inputs"][data["inputUuid"]]["settings"])}
        if request == "SetInputSettings":
            col["inputs"][data["inputUuid"]]["settings"].update(data["inputSettings"])
            return {}
        if request == "GetSceneList":
            return {"scenes": [{"sceneName": name} for name in col["scenes"]]}
        if request == "GetSceneItemList":
            return {"sceneItems": [{k: v for k, v in i.items() if k != "transform"} for i in col["scenes"][data["sceneName"]]]}
        if request == "CreateInput":
            if any(v["name"] == data["inputName"] for v in col["inputs"].values()):
                raise AgentError("OBS_REQUEST_FAILED", "name exists", {"obs_code": 601})
            source, item_id = self.add_input(data["sceneName"], data["inputName"], data["inputKind"], data["inputSettings"])
            self.item(data["sceneName"], item_id)["sceneItemEnabled"] = data.get("sceneItemEnabled", True)
            return {"inputUuid": source, "sceneItemId": item_id}
        if request == "CreateSceneItem":
            self.next_item += 1
            items = col["scenes"][data["sceneName"]]
            name = col["inputs"][data["sourceUuid"]]["name"]
            items.append(self._item(self.next_item, name, data["sourceUuid"], len(items), data.get("sceneItemEnabled", True)))
            return {"sceneItemId": self.next_item}
        if request == "GetSceneItemTransform":
            item = self.item(data["sceneName"], data["sceneItemId"])
            t = item["transform"]
            source = col["inputs"][item["sourceUuid"]]["settings"]
            width, height = source.get("width", 1080), source.get("height", 1920)
            return {"sceneItemTransform": {**t, "sourceWidth": width, "sourceHeight": height,
                                           "width": width * t["scaleX"] - t["cropLeft"] - t["cropRight"],
                                           "height": height * t["scaleY"] - t["cropTop"] - t["cropBottom"]}}
        if request == "SetSceneItemTransform":
            self.item(data["sceneName"], data["sceneItemId"])["transform"].update(data["sceneItemTransform"])
            return {}
        if request == "SetSceneItemIndex":
            items = col["scenes"][data["sceneName"]]
            item = self.item(data["sceneName"], data["sceneItemId"])
            items.remove(item)
            items.insert(data["sceneItemIndex"], item)
            for index, i in enumerate(items):
                i["sceneItemIndex"] = index
            return {}
        if request == "GetSceneItemIndex":
            return {"sceneItemIndex": self.item(data["sceneName"], data["sceneItemId"])["sceneItemIndex"]}
        if request == "SetSceneItemEnabled":
            self.item(data["sceneName"], data["sceneItemId"])["sceneItemEnabled"] = data["sceneItemEnabled"]
            return {}
        if request == "GetSceneItemEnabled":
            return {"sceneItemEnabled": self.item(data["sceneName"], data["sceneItemId"])["sceneItemEnabled"]}
        if request == "GetStreamStatus":
            return {"outputActive": self.streaming}
        if request == "PressInputPropertiesButton":
            return {}
        raise AssertionError(f"unexpected request {request}")

    # helpers
    def ours(self, scene="LIVE"):
        return [i for i in self.col["scenes"][scene] if self.col["inputs"][i["sourceUuid"]]["kind"] == "browser_source"
                and ours_by_url(self.col["inputs"][i["sourceUuid"]]["settings"].get("url", ""))]

    def writes(self):
        return [(r, d) for r, d in self.requests if r in WRITES]


class FakeConfig:
    def __init__(self, **overlay):
        self.overlay = SimpleNamespace(enabled=True, port=0, source_name="Sarae Overlay", input_uuids={}, token="tok", **overlay)

    def get(self):
        return self

    def update(self, patch):
        for key, value in patch.get("overlay", {}).items():
            setattr(self.overlay, key, value)


URL = "http://127.0.0.1:17891/overlay/?t=tok&sarae_overlay=1"
MEDIA_A, MEDIA_B = "a" * 32, "b" * 32


def overlay(id, media_id="m" * 32, start_ms=0, duration_ms=3_600_000, z=0, enabled=True):
    return {"id": id, "media_id": media_id, "start_ms": start_ms, "duration_ms": duration_ms, "enabled": enabled,
            "layout": {"x": 0, "y": 0, "width": 0.5, "height": 0.1, "z_index": z, "opacity": 1},
            "animation_in": {"name": "fade", "duration_ms": 300}, "animation_out": {"name": "fade", "duration_ms": 300},
            "elements": [{"kind": "text", "text": f"#{id}"}]}


def source_for(obs, config=None):
    config = config or FakeConfig()
    return ObsOverlaySource(obs, lambda: config.overlay.source_name, lambda: dict(config.overlay.input_uuids),
                            lambda c, u: config.update({"overlay": {"input_uuids": {**config.overlay.input_uuids, c: u}}})), config


class ObsSourceTests(unittest.TestCase):
    def test_creates_one_hidden_locked_source_above_the_video(self):
        obs = FakeObs(720, 1280)
        source, config = source_for(obs)
        result = source.ensure(URL, [VIDEO])
        ours = obs.ours()
        self.assertEqual(len(ours), 1)
        item = ours[0]
        self.assertEqual((item["sceneItemEnabled"], item["sceneItemIndex"]), (False, 1), "hidden, above the video")
        t = obs(**{"request": "GetSceneItemTransform", "data": {"sceneName": "LIVE", "sceneItemId": item["sceneItemId"]}})["sceneItemTransform"]
        self.assertEqual((t["positionX"], t["positionY"], t["rotation"], t["cropLeft"], t["boundsType"], t["alignment"]),
                         (0.0, 0.0, 0.0, 0, "OBS_BOUNDS_NONE", 5))
        self.assertEqual((round(t["width"]), round(t["height"])), (720, 1280), "the 1080×1920 page fills the 720×1280 canvas")
        self.assertEqual(result["scenes"], {"LIVE": item["sceneItemId"]}, "tracked by scene item id, only scenes with the video")
        settings = obs.col["inputs"][item["sourceUuid"]]["settings"]
        self.assertEqual((settings["width"], settings["height"], settings["shutdown"]), (1080, 1920, False))
        self.assertIn("rgba(0, 0, 0, 0)", settings["css"])
        self.assertEqual(config.overlay.input_uuids, {"Main": item["sourceUuid"]})

    def test_never_touches_video_settings_or_other_sources(self):
        obs = FakeObs()
        source, _ = source_for(obs)
        source.ensure(URL, [VIDEO])
        source.set_visible(True)
        source.ensure(URL, [VIDEO])
        ours = obs.ours()[0]
        for request, data in obs.writes():
            self.assertNotIn(request, {"SetVideoSettings", "SetProfileParameter", "RemoveInput", "RemoveSceneItem",
                                       "SetCurrentSceneCollection", "SetCurrentProgramScene"})
            target = data.get("inputUuid") or data.get("sourceUuid")
            if target:
                self.assertEqual(target, ours["sourceUuid"])
            if "sceneItemId" in data:
                self.assertEqual(data["sceneItemId"], ours["sceneItemId"])

    def test_a_source_of_the_same_name_made_by_the_operator_is_left_alone(self):
        obs = FakeObs()
        obs.add_input("LIVE", "Sarae Overlay", "browser_source", {"url": "https://example.com/my-own-page"})
        source, _ = source_for(obs)
        with self.assertRaises(AgentError) as caught:
            source.ensure(URL, [VIDEO])
        self.assertEqual(caught.exception.code, "OVERLAY_NAME_TAKEN")
        self.assertEqual(obs.writes(), [])
        obs2 = FakeObs()
        obs2.add_input("LIVE", "Sarae Overlay", "image_source", {})
        with self.assertRaises(AgentError) as caught:
            source_for(obs2)[0].ensure(URL, [VIDEO])
        self.assertEqual((caught.exception.code, obs2.writes()), ("OVERLAY_NAME_TAKEN", []))

    def test_a_moved_or_cropped_source_is_locked_again(self):
        obs = FakeObs()
        source, _ = source_for(obs)
        source.ensure(URL, [VIDEO])
        item = obs.ours()[0]
        item["transform"].update(positionX=140.0, scaleX=0.5, cropTop=30, rotation=12.0)
        result = source.ensure(URL, [VIDEO])
        self.assertIn("transform:LIVE", result["changes"])
        self.assertEqual((item["transform"]["positionX"], item["transform"]["scaleX"], item["transform"]["cropTop"],
                          item["transform"]["rotation"]), (0.0, 1.0, 0, 0.0))
        self.assertEqual(source.ensure(URL, [VIDEO])["changes"], [], "a locked source is left as it is")

    def test_a_source_moved_below_the_video_goes_back_on_top(self):
        obs = FakeObs()
        source, _ = source_for(obs)
        source.ensure(URL, [VIDEO])
        item = obs.ours()[0]
        obs("SetSceneItemIndex", {"sceneName": "LIVE", "sceneItemId": item["sceneItemId"], "sceneItemIndex": 0})
        source.ensure(URL, [VIDEO])
        self.assertGreater(item["sceneItemIndex"], next(i for i in obs.col["scenes"]["LIVE"] if i["sourceName"] == VIDEO)["sceneItemIndex"])

    def test_a_deleted_source_is_created_again(self):
        obs = FakeObs()
        source, config = source_for(obs)
        source.ensure(URL, [VIDEO])
        gone = obs.ours()[0]
        obs.col["scenes"]["LIVE"].remove(gone)
        del obs.col["inputs"][gone["sourceUuid"]]
        result = source.ensure(URL, [VIDEO])
        self.assertIn("created", result["changes"])
        self.assertEqual(len(obs.ours()), 1)
        self.assertNotEqual(config.overlay.input_uuids["Main"], gone["sourceUuid"])

    def test_a_scene_item_removed_from_one_scene_is_added_back_with_a_new_id(self):
        obs = FakeObs()
        obs.col["scenes"]["Other"].append(obs._item(9, VIDEO, next(iter(obs.col["inputs"])), 0))
        source, _ = source_for(obs)
        first = source.ensure(URL, [VIDEO])["scenes"]
        self.assertEqual(set(first), {"LIVE", "Other"})
        obs.col["scenes"]["Other"] = [i for i in obs.col["scenes"]["Other"] if i["sceneItemId"] != first["Other"]]
        again = source.ensure(URL, [VIDEO])
        self.assertIn("added:Other", again["changes"])
        self.assertNotEqual(again["scenes"]["Other"], first["Other"])
        self.assertEqual(again["scenes"]["LIVE"], first["LIVE"])

    def test_scene_collection_change_uses_the_source_of_that_collection(self):
        obs = FakeObs()
        source, config = source_for(obs)
        source.ensure(URL, [VIDEO])
        main_uuid = config.overlay.input_uuids["Main"]
        obs.collections["Rehearsal"] = obs._collection()
        obs.current = "Rehearsal"
        result = source.ensure(URL, [VIDEO])
        self.assertIn("collection", result["changes"])
        self.assertIn("created", result["changes"])
        self.assertEqual(set(config.overlay.input_uuids), {"Main", "Rehearsal"})
        obs.current = "Main"
        source.ensure(URL, [VIDEO])
        self.assertEqual(source.input_uuid, main_uuid, "back to the first collection: its own source again")

    def test_a_canvas_that_is_not_9_16_keeps_the_overlay_out(self):
        obs = FakeObs(1920, 1080)
        source, _ = source_for(obs)
        with self.assertRaises(AgentError) as caught:
            source.ensure(URL, [VIDEO])
        self.assertEqual(caught.exception.code, "OVERLAY_CANVAS_NOT_9_16")
        self.assertEqual(obs.writes(), [])

    def test_no_scene_with_the_video(self):
        obs = FakeObs()
        source, _ = source_for(obs)
        with self.assertRaises(AgentError) as caught:
            source.ensure(URL, ["Another Video"])
        self.assertEqual(caught.exception.code, "OVERLAY_NO_VIDEO_SCENE")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cache = OverlayCache(self.root)
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        return {"/api/live/overlay-assets/a1/file": PNG, "https://cdn.example/p.jpg": b"\xff\xd8\xff" + b"\x00" * 20,
                "https://cdn.example/evil.html": b"<html>"}.get(url.split("?")[0], b"")

    def snapshot(self, sha=None, product_url="https://cdn.example/p.jpg"):
        return {"revision": "r1", "assets": {"a1": {"url": "/api/live/overlay-assets/a1/file?exp=1&sig=x",
                                                    "sha256": sha or hashlib.sha256(PNG).hexdigest()}},
                "products": {"7": {"name": "Áo", "image_url": product_url}}, "overlays": []}

    def test_images_point_at_checked_local_copies(self):
        scene, missing = self.cache.localize(self.snapshot(), self.fetch, "tok")
        self.assertEqual(missing, [])
        name = scene["assets"]["a1"]["url"].split("/")[-1].split("?")[0]
        self.assertTrue(scene["assets"]["a1"]["url"].startswith("/api/overlay-files/") and scene["assets"]["a1"]["url"].endswith("?t=tok"))
        self.assertEqual(self.cache.path(name).read_bytes(), PNG)
        self.assertTrue(scene["products"]["7"]["image_url"].startswith("/api/overlay-files/"))
        self.cache.localize({**self.snapshot(), "assets": {"a1": {"url": "/api/live/overlay-assets/a1/file?exp=2&sig=y",
                                                                  "sha256": hashlib.sha256(PNG).hexdigest()}}}, self.fetch, "tok")
        self.assertEqual(len(self.fetched), 2, "a new signature of the same image is not downloaded again")

    def test_wrong_content_is_reported_missing(self):
        scene, missing = self.cache.localize(self.snapshot(sha="0" * 64, product_url="https://cdn.example/evil.html"), self.fetch, "tok")
        self.assertEqual({m.get("asset_id") or m.get("product_id") for m in missing}, {"a1", "7"})
        self.assertEqual((scene["assets"]["a1"]["url"], scene["products"]["7"]["image_url"]), ("", ""))
        _, missing = self.cache.localize(self.snapshot(product_url="http://plain.example/x.png"), self.fetch, "tok")
        self.assertEqual([m.get("product_id") for m in missing], ["7"], "only signed /api/ or https URLs")

    def test_old_unused_images_are_cleaned_up_and_the_scene_in_use_is_kept(self):
        import os
        import time
        scene, _ = self.cache.localize(self.snapshot(), self.fetch, "tok")
        used = {u.split("/")[-1].split("?")[0] for u in (scene["assets"]["a1"]["url"], scene["products"]["7"]["image_url"])}
        stale = self.root / ("c" * 64 + ".png")
        stale.write_bytes(PNG)
        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))
        for name in used:
            os.utime(self.root / name, (old, old))
        removed = self.cache.prune(used)
        self.assertEqual(removed, [stale.name])
        self.assertTrue(all((self.root / n).is_file() for n in used), "images of the scene in use are never removed")

    def test_served_names_cannot_escape_the_cache(self):
        for name in ("../config.json", "index.json", "x.png", "a" * 64 + ".exe"):
            self.assertIsNone(self.cache.path(name))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.obs = FakeObs()
        self.config = FakeConfig()
        self.snapshots = {}
        self.playback = {}
        self.service = OverlayService(self.config, self.obs, lambda: self.obs.connected, lambda: [VIDEO],
                                      self.fetch_snapshot, lambda url: PNG, OverlayCache(Path(tempfile.mkdtemp())),
                                      clock=lambda: self.now, server=SimpleNamespace(port=17891, start=lambda p: 17891, stop=lambda: None),
                                      playback_state=lambda: self.playback)

    def fetch_snapshot(self, session_id, timeline_id=None):
        if timeline_id is not None:
            return self.snapshots.get((session_id, timeline_id))
        found = self.snapshots.get(session_id)
        if isinstance(found, Exception):
            raise found
        return found

    def snap(self, revision="r1", version=1, overlays=1):
        return {"revision": revision, "version": version, "timeline_id": version, "session_id": 5,
                "overlays": [overlay(i) for i in range(overlays)],
                "assets": {"a1": {"url": "/api/live/overlay-assets/a1/file?exp=1", "sha256": hashlib.sha256(PNG).hexdigest()}},
                "products": {}}

    def step(self, seconds=2.0, position=1000, state="OBS_MEDIA_STATE_PLAYING", renderer=True):
        self.now += seconds
        self.service.observe(5, "m" * 32, position, state, 0)
        if renderer:
            self.service.renderer_state()
        self.service.tick()

    def item(self):
        return self.obs.ours()[0]

    def test_shown_only_after_the_renderer_is_ready(self):
        self.snapshots[5] = self.snap()
        self.step()
        state = self.service.renderer_state()
        self.assertEqual((state["revision"], state["show"]), ("r1", False))
        self.assertIn("/api/overlay-files/", self.service.scene_for("r1")["assets"]["a1"]["url"])
        self.assertFalse(self.item()["sceneItemEnabled"])
        self.assertEqual(self.service.renderer_ready({"revision": "r1", "ok": True}), {"accepted": True})
        self.step()
        self.assertTrue(self.item()["sceneItemEnabled"])
        self.assertEqual(self.service.status()["state"], "showing")
        clock = self.service.renderer_state()["clock"]
        self.assertEqual((clock["media_id"], clock["position_ms"], clock["playing"]), ("m" * 32, 1000, True))

    def test_a_new_version_replaces_the_old_only_when_ready_else_rolls_back(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        self.step()
        self.snapshots[5] = self.snap("r2", 2)
        self.now += 20
        self.step()
        self.assertEqual(self.service.renderer_state()["revision"], "r2")
        self.assertTrue(self.item()["sceneItemEnabled"], "the old version stays on screen while the new one loads")
        self.assertEqual(self.service.renderer_ready({"revision": "r2", "ok": False, "missing": ["x.png"]}), {"accepted": False})
        self.assertEqual((self.service.renderer_state()["revision"], self.service.status()["version"]), ("r1", 1), "rollback")
        self.assertEqual(self.service.status()["error"]["code"], "RENDERER_NOT_READY")
        self.step()
        self.assertTrue(self.item()["sceneItemEnabled"])

    def test_no_ready_in_time_rolls_back(self):
        self.snapshots[5] = self.snap()
        self.step()
        for _ in range(11):
            self.step()
        self.assertIsNone(self.service.pending)
        self.assertEqual(self.service.status()["error"]["code"], "RENDERER_TIMEOUT")
        self.assertFalse(self.item()["sceneItemEnabled"])

    def test_a_lost_renderer_hides_the_overlay_and_reloads_the_page(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        self.step()
        self.assertTrue(self.item()["sceneItemEnabled"])
        for _ in range(4):
            self.step(renderer=False)
        self.assertFalse(self.item()["sceneItemEnabled"])
        self.assertIn("PressInputPropertiesButton", [r for r, _ in self.obs.requests])
        self.service.renderer_ready({"revision": "r1", "ok": True})  # the reloaded page reports again
        self.step()
        self.assertTrue(self.item()["sceneItemEnabled"])

    def test_stopped_playback_or_a_stale_clock_hides_the_overlay(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        self.step()
        self.service.clear("playback_ended")
        self.now += 2
        self.service.renderer_state()
        self.service.tick()
        self.assertFalse(self.item()["sceneItemEnabled"])

    def test_paused_video_keeps_the_overlay_with_a_still_clock(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        self.step(position=4000, state="OBS_MEDIA_STATE_PAUSED")
        self.assertTrue(self.item()["sceneItemEnabled"])
        self.assertFalse(self.service.renderer_state()["clock"]["playing"])

    def test_obs_disconnect_and_reconnect(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        self.step()
        self.obs.connected = False
        self.step()
        self.assertEqual(self.service.status()["state"], "hidden")
        self.obs.connected = True
        self.step()
        self.assertTrue(self.item()["sceneItemEnabled"], "visibility is applied again after a reconnect")

    def test_a_missing_overlay_image_keeps_the_previous_overlay(self):
        self.service._fetch_image = lambda url: b"not an image"
        self.snapshots[5] = self.snap()
        self.step()
        self.assertIsNone(self.service.pending)
        self.assertEqual(self.service.status()["error"]["code"], "ASSET_MISSING")

    def test_cloud_unreachable_keeps_what_is_loaded(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        self.snapshots[5] = ConnectionError("offline")
        self.now += 20
        self.step()
        self.assertTrue(self.item()["sceneItemEnabled"])
        self.assertEqual(self.service.status()["revision"], "r1")

    def test_no_active_version_hides(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        self.step()
        self.snapshots[5] = None
        self.now += 20
        self.step()
        self.assertFalse(self.item()["sceneItemEnabled"])

    def test_a_page_that_outlived_an_agent_restart_and_draws_the_revision_counts_as_ready(self):
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_state({"revision": "r1", "seq": 40, "build": ""})  # never loads it again: its ACK is READY
        self.assertEqual(self.service.status()["ready_revision"], "r1")
        self.step()
        self.assertTrue(self.item()["sceneItemEnabled"])

    def test_a_page_of_an_older_renderer_build_is_reloaded(self):
        self.service.server.build = "overlay-new.js"
        self.snapshots[5] = self.snap()
        self.step()
        self.service.renderer_state({"revision": "", "seq": 0, "build": "overlay-old.js"})
        self.now += 31
        self.service.observe(5, "m" * 32, 1000, "OBS_MEDIA_STATE_PLAYING", 0)
        self.service.renderer_state({"revision": "", "seq": 0, "build": "overlay-old.js"})
        self.service.tick()
        self.assertIn("PressInputPropertiesButton", [r for r, _ in self.obs.requests])
        refreshes = [r for r, _ in self.obs.requests].count("PressInputPropertiesButton")
        self.service.renderer_state({"revision": "", "seq": 0, "build": "overlay-new.js"})
        self.now += 40
        self.step()
        self.assertEqual([r for r, _ in self.obs.requests].count("PressInputPropertiesButton"), refreshes, "an up-to-date page is left alone")

    def test_pre_live_check_prepares_everything_and_never_shows(self):
        self.snapshots[5] = self.snap()
        ready = lambda seconds: self.service.renderer_ready({"revision": "r1", "ok": True})  # the page answers while we wait
        self.service.server.build = "overlay-x.js"
        report = self.service.check(5, sleep=ready)
        codes = {c["code"]: c["ok"] for c in report["checks"]}
        self.assertTrue(report["ok"], report)
        for code in ("RENDERER_BUILT", "ACTIVE_VERSION", "ASSETS_CACHED", "OBS_CONNECTED", "OBS_CANVAS", "OBS_SOURCE",
                     "OBS_TRANSFORM", "RENDERER_READY"):
            self.assertTrue(codes.get(code), code)
        self.assertFalse(self.item()["sceneItemEnabled"], "a check never shows the overlay")
        self.assertEqual(self.service.status()["revision"], "r1", "prepared: shows at once when the session plays")

    def test_pre_live_check_reports_what_is_wrong(self):
        self.service.server.build = "overlay-x.js"
        self.snapshots[5] = None
        self.obs.video["baseWidth"] = 1920
        report = self.service.check(5, sleep=lambda s: None)
        codes = {c["code"]: (c["ok"], c["level"]) for c in report["checks"]}
        self.assertFalse(report["ok"])
        self.assertEqual(codes["ACTIVE_VERSION"], (False, "warning"))
        self.assertEqual(codes["OVERLAY_CANVAS_NOT_9_16"], (False, "error"))
        self.obs.connected = False
        self.assertEqual({c["code"]: c["ok"] for c in self.service.check(5, sleep=lambda s: None)["checks"]}["OBS_CONNECTED"], False)

    def test_each_agent_run_has_its_own_clock_epoch(self):
        self.service.observe(5, "m" * 32, 1000, "OBS_MEDIA_STATE_PLAYING", 0)
        first = self.service.renderer_state()["clock"]
        restarted = OverlayService(self.config, self.obs, lambda: True, lambda: [VIDEO], self.fetch_snapshot, lambda url: PNG,
                                   OverlayCache(Path(tempfile.mkdtemp())), clock=lambda: self.now,
                                   server=SimpleNamespace(port=17891, start=lambda p: 17891, stop=lambda: None))
        restarted.observe(5, "m" * 32, 1000, "OBS_MEDIA_STATE_PLAYING", 0)
        again = restarted.renderer_state()["clock"]
        self.assertEqual((first["seq"], again["seq"]), (1, 1))
        self.assertNotEqual(first["epoch"], again["epoch"], "a page that outlived the restart must not drop seq 1 again")

    def preview_ready(self, **kwargs):
        self.snapshots[(5, 11)] = self.snap("draft-r", 1)
        result = self.service.preview_on_obs(5, 11, "m" * 32, kwargs.pop("position", 1000), **kwargs)
        self.service.renderer_state()
        self.service.renderer_ready({"revision": "draft-r", "ok": True})
        self.service.tick()
        return result

    def test_a_draft_is_previewed_on_obs_without_live(self):
        self.assertTrue(self.preview_ready()["preview"])
        self.assertTrue(self.item()["sceneItemEnabled"], "shown with no playback at all")
        clock = self.service.renderer_state()["clock"]
        self.assertEqual((clock["media_id"], clock["position_ms"], clock["playing"]), ("m" * 32, 1000, False))
        self.now += 30
        self.assertEqual(self.service.renderer_state()["clock"]["position_ms"], 1000, "paused in the editor: it holds")
        self.service.preview_on_obs(5, 11, "m" * 32, 5000, playing=True)
        self.now += 3
        self.assertEqual(self.service.renderer_state()["clock"]["position_ms"], 8000, "playing in the editor: it runs on")
        self.assertEqual(self.service.status()["preview"]["timeline_id"], 11)

    def test_preview_is_refused_on_air_or_during_live(self):
        self.snapshots[(5, 11)] = self.snap("draft-r", 1)
        self.obs.streaming = True
        with self.assertRaises(AgentError) as caught:
            self.service.preview_on_obs(5, 11, "m" * 32, 0)
        self.assertEqual(caught.exception.code, "PREVIEW_ON_AIR")
        self.obs.streaming, self.playback = False, {"active": True, "session_id": 4}
        with self.assertRaises(AgentError) as caught:
            self.service.preview_on_obs(5, 11, "m" * 32, 0)
        self.assertEqual(caught.exception.code, "PREVIEW_BUSY")
        self.assertIsNone(self.service.preview)

    def test_preview_ends_by_itself_by_stop_or_when_live_starts(self):
        self.preview_ready(seconds=60)
        self.now += 61
        self.service.tick()
        self.assertIsNone(self.service.preview)
        self.assertFalse(self.item()["sceneItemEnabled"])
        self.preview_ready()
        self.assertTrue(self.item()["sceneItemEnabled"])
        self.service.preview_on_obs(5, stop=True)
        self.service.tick()
        self.assertFalse(self.item()["sceneItemEnabled"])
        self.preview_ready()
        self.service.observe(5, "m" * 32, 0, "OBS_MEDIA_STATE_PLAYING", 0)
        self.assertIsNone(self.service.preview, "a LIVE playback takes over")

    def test_preview_reports_the_editors_loop_iteration(self):
        self.preview_ready(iteration=3)
        self.assertEqual(self.service.renderer_state()["clock"]["iteration"], 3)
        seq = self.service.renderer_state()["clock"]["seq"]
        self.service.preview_on_obs(5, 11, "m" * 32, 1000, iteration=4)
        clock = self.service.renderer_state()["clock"]
        self.assertEqual((clock["iteration"], clock["seq"]), (4, seq + 1), "a new loop at the same position is a new clock")

    def test_active_overlays_follow_the_random_plan_of_the_loop(self):
        media = "m" * 32
        snap = self.snap(overlays=3)
        snap["schedule"] = {"mode": "random", "every_ms": 60_000, "min_gap_ms": 10_000}
        for o in snap["overlays"]:
            o["show_ms"] = 5000
        self.snapshots[5] = snap
        self.step()
        self.service.renderer_ready({"revision": "r1", "ok": True})
        for iteration in (0, 1):
            plan = random_plan(snap["overlays"], media, snap["schedule"], snap["timeline_id"], iteration)
            self.assertTrue(plan)
            for a in plan[:4]:
                self.service.observe(5, media, a["start_ms"], "OBS_MEDIA_STATE_PLAYING", iteration)
                self.assertEqual(self.service.status()["active_overlays"], [snap["overlays"][a["index"]]["id"]])
                self.service.observe(5, media, a["end_ms"], "OBS_MEDIA_STATE_PLAYING", iteration)
                self.assertEqual(self.service.status()["active_overlays"], [], "end is exclusive; min gap after it")
        self.assertNotEqual(random_plan(snap["overlays"], media, snap["schedule"], 1, 0),
                            random_plan(snap["overlays"], media, snap["schedule"], 1, 1), "each loop draws its own plan")

    def test_the_active_version_never_replaces_the_previewed_draft(self):
        self.snapshots[5] = self.snap("active-r", 2)
        self.preview_ready()
        self.now += 60
        self.service.tick()
        self.assertEqual(self.service.renderer_state()["revision"], "draft-r")

    def test_an_overlay_failure_never_raises_into_the_loop(self):
        self.snapshots[5] = self.snap()
        self.obs.video["baseWidth"] = 1920  # canvas not 9:16
        self.step()
        self.assertEqual(self.service.status()["error"]["code"], "OVERLAY_CANVAS_NOT_9_16")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.renderer = Path(tempfile.mkdtemp())
        (self.renderer / "assets").mkdir()
        (self.renderer / "overlay.html").write_text("<html>renderer</html>", encoding="utf-8")
        (self.renderer / "assets" / "app.js").write_text("1", encoding="utf-8")
        self.ready, self.acks = [], []
        handlers = SimpleNamespace(token="tok", renderer_state=lambda ack=None: self.acks.append(ack) or {"revision": "r1", "show": True, "clock": None},
                                   scene_for=lambda r: {"overlays": []} if r == "r1" else None,
                                   renderer_ready=lambda body: self.ready.append(body) or {"accepted": True},
                                   file_path=lambda name: (self.renderer / "overlay.html") if name == "a" * 64 + ".png" else None)
        self.server = OverlayServer(handlers, self.renderer)
        self.port = self.server.start(0)

    def tearDown(self):
        self.server.stop()

    def get(self, path, host=None, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers={"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data, response

    def test_routes_and_token(self):
        self.assertEqual(self.get("/overlay/?t=tok")[0], 200)
        self.assertEqual(self.get("/overlay/assets/app.js")[0], 200)
        self.assertEqual(self.get("/overlay/state")[0], 403)
        self.assertEqual(self.get("/overlay/state?t=bad")[0], 403)
        status, body, response = self.get("/overlay/state?t=tok")
        self.assertEqual((status, json.loads(body)["revision"], response.getheader("Cache-Control")), (200, "r1", "no-store"))
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(self.get("/overlay/scene?t=tok&revision=r1")[0], 200)
        self.assertEqual(self.get("/overlay/scene?t=tok&revision=r9")[0], 404)
        self.assertEqual(self.get("/overlay/ready?t=tok", method="POST", body=json.dumps({"revision": "r1", "ok": True}))[0], 200)
        self.assertEqual(self.ready, [{"revision": "r1", "ok": True}])
        self.assertEqual(self.get(f"/api/overlay-files/{'a' * 64}.png?t=tok")[0], 200)
        self.assertEqual(self.get("/api/overlay-files/..%2F..%2Fconfig.json?t=tok")[0], 404)
        self.assertEqual(self.get("/overlay/assets/..%2Foverlay.html")[0], 404)

    def test_other_hosts_are_refused(self):
        self.assertEqual(self.get("/overlay/state?t=tok", host="evil.example")[0], 403)
        self.assertEqual(self.get("/overlay/?t=tok", host=f"attacker.test:{self.port}")[0], 403)


if __name__ == "__main__":
    unittest.main()

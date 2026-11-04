"""The "Sarae Overlay" Browser Source in OBS, managed through obs-websocket.

What this module will and will not do:

* It changes only the Browser Source this Agent created, recognised by its input UUID (remembered per scene
  collection) or, for one created before the UUID was saved, by our URL marker. A source of the same name made by the
  operator is never edited, moved or deleted: the overlay then reports OVERLAY_NAME_TAKEN.
* It never deletes a source, and never changes OBS video settings (FPS, base or output resolution); it only reads them.
* In every scene that shows the video (a scene item of the video Media Source), it keeps one scene item of the overlay,
  tracked by scene item ID per scene (not by name), above the video, with a locked transform: position (0,0),
  rotation 0, no crop, no bounds, top-left alignment, scale = OBS canvas / 1080×1920. Each change is read back.
* Visibility is the scene item's "enabled" flag; it is turned on only when the service says the renderer is READY.
"""
from __future__ import annotations

import logging
from typing import Callable

from sarae_agent.errors import AgentError

log = logging.getLogger("sarae.overlay")

CANVAS_WIDTH, CANVAS_HEIGHT = 1080, 1920
BROWSER_KIND = "browser_source"
URL_MARKER = "sarae_overlay=1"
ALIGN_TOP_LEFT = 5
TRANSPARENT_CSS = "body { background-color: rgba(0, 0, 0, 0); margin: 0; overflow: hidden; }"


def ours_by_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith("http://127.0.0.1:") and "/overlay/" in url and URL_MARKER in url


class OverlaySourceError(AgentError):
    pass


class ObsOverlaySource:
    def __init__(self, call: Callable[[str, dict | None], dict], source_name: Callable[[], str],
                 remembered: Callable[[], dict], remember: Callable[[str, str], None]):
        self._call = call
        self._source_name = source_name
        self._remembered = remembered
        self._remember = remember
        self.input_uuid: str | None = None
        self.collection: str | None = None
        # scene name -> scene item id of our overlay in that scene
        self.items: dict[str, int] = {}
        self.canvas: tuple[int, int] | None = None

    # ----- reading -----
    def current_collection(self) -> str:
        return str(self._call("GetSceneCollectionList", None).get("currentSceneCollectionName") or "")

    def _canvas(self) -> tuple[int, int]:
        video = self._call("GetVideoSettings", None)
        width, height = int(video.get("baseWidth") or 0), int(video.get("baseHeight") or 0)
        if width <= 0 or height <= 0:
            raise OverlaySourceError("OVERLAY_CANVAS_UNKNOWN", "Không đọc được kích thước canvas OBS")
        if abs(width / height - CANVAS_WIDTH / CANVAS_HEIGHT) > 0.01:
            raise OverlaySourceError("OVERLAY_CANVAS_NOT_9_16",
                                     f"Canvas OBS {width}×{height} không phải 9:16; overlay được giữ ẩn (Sarae không đổi cài đặt OBS)",
                                     {"width": width, "height": height})
        return width, height

    def _find_input(self) -> str | None:
        """Our Browser Source in the current collection (its UUID), None when there is none yet."""
        inputs = self._call("GetInputList", {"inputKind": BROWSER_KIND}).get("inputs") or []
        remembered = self._remembered().get(self.collection or "")
        by_uuid = {i.get("inputUuid"): i for i in inputs}
        if remembered and remembered in by_uuid:
            return remembered
        name = self._source_name()
        named = next((i for i in inputs if i.get("inputName") == name), None)
        if named is None:
            if any(i.get("inputName") == name for i in self._call("GetInputList", None).get("inputs") or []):
                raise OverlaySourceError("OVERLAY_NAME_TAKEN", f"OBS đã có nguồn '{name}' không phải Browser Source của Sarae; "
                                         "Sarae không sửa nguồn của bạn. Đổi tên nguồn đó hoặc đổi tên overlay trong cấu hình")
            return None
        settings = self._call("GetInputSettings", {"inputUuid": named["inputUuid"]}).get("inputSettings") or {}
        if not ours_by_url(settings.get("url", "")):
            raise OverlaySourceError("OVERLAY_NAME_TAKEN", f"Browser Source '{name}' do bạn tạo (không phải của Sarae); "
                                     "Sarae không sửa nó. Đổi tên nguồn đó hoặc đổi tên overlay trong cấu hình")
        self._remember(self.collection or "", named["inputUuid"])
        return named["inputUuid"]

    def _video_scenes(self, video_sources: list[str]) -> dict[str, list[dict]]:
        scenes = self._call("GetSceneList", None).get("scenes") or []
        found = {}
        for scene in scenes:
            name = scene.get("sceneName")
            items = self._call("GetSceneItemList", {"sceneName": name}).get("sceneItems") or []
            if any(i.get("sourceName") in video_sources for i in items):
                found[name] = items
        return found

    # ----- the source -----
    def settings(self, url: str) -> dict:
        return {"url": url, "width": CANVAS_WIDTH, "height": CANVAS_HEIGHT, "css": TRANSPARENT_CSS,
                "shutdown": False, "restart_when_active": False, "reroute_audio": False, "fps_custom": False,
                "is_local_file": False}

    def _ensure_settings(self, url: str) -> None:
        current = self._call("GetInputSettings", {"inputUuid": self.input_uuid}).get("inputSettings") or {}
        wanted = self.settings(url)
        if any(current.get(k) != v for k, v in wanted.items() if k in ("url", "width", "height", "css", "shutdown")):
            log.info("Cập nhật cài đặt Browser Source overlay")
            self._call("SetInputSettings", {"inputUuid": self.input_uuid, "inputSettings": wanted, "overlay": True})

    def transform(self) -> dict:
        width, height = self.canvas
        return {"positionX": 0.0, "positionY": 0.0, "rotation": 0.0, "scaleX": width / CANVAS_WIDTH,
                "scaleY": height / CANVAS_HEIGHT, "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0,
                "boundsType": "OBS_BOUNDS_NONE", "alignment": ALIGN_TOP_LEFT}

    def _transform_ok(self, actual: dict) -> bool:
        width, height = self.canvas
        for key, value in self.transform().items():
            if key == "boundsType" or key == "alignment":
                if actual.get(key) != value:
                    return False
            elif abs(float(actual.get(key) or 0) - value) > 1e-3:
                return False
        # The source's drawn size must be the canvas (a renderer of another size would be scaled wrong).
        return abs(float(actual.get("width") or 0) - width) <= 0.5 and abs(float(actual.get("height") or 0) - height) <= 0.5

    def _lock_transform(self, scene: str, item_id: int) -> bool:
        """Re-lock a moved/scaled/cropped item; True when it had drifted."""
        actual = self._call("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id}).get("sceneItemTransform") or {}
        if self._transform_ok(actual):
            return False
        self._call("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id, "sceneItemTransform": self.transform()})
        actual = self._call("GetSceneItemTransform", {"sceneName": scene, "sceneItemId": item_id}).get("sceneItemTransform") or {}
        if not self._transform_ok(actual):
            # width/height of a fresh Browser Source are 0 until its first frame: accept the scale part then.
            if float(actual.get("width") or 0) == 0 and all(abs(float(actual.get(k) or 0) - v) <= 1e-3 for k, v in self.transform().items()
                                                            if k not in ("boundsType", "alignment")):
                return True
            raise OverlaySourceError("OVERLAY_TRANSFORM_REJECTED", f"OBS không nhận vị trí/tỉ lệ overlay trong scene '{scene}'",
                                     {"scene": scene, "transform": {k: actual.get(k) for k in ("positionX", "positionY", "scaleX",
                                                                                                 "scaleY", "width", "height")}})
        return True

    def _above_video(self, scene: str, item_id: int, items: list[dict], video_sources: list[str]) -> None:
        index = {i.get("sceneItemId"): i.get("sceneItemIndex", 0) for i in items}
        video_top = max(index[i["sceneItemId"]] for i in items if i.get("sourceName") in video_sources)
        if index.get(item_id, -1) > video_top:
            return
        self._call("SetSceneItemIndex", {"sceneName": scene, "sceneItemId": item_id, "sceneItemIndex": len(items) - 1})
        after = self._call("GetSceneItemList", {"sceneName": scene}).get("sceneItems") or []
        index = {i.get("sceneItemId"): i.get("sceneItemIndex", 0) for i in after}
        videos = [index[i["sceneItemId"]] for i in after if i.get("sourceName") in video_sources]
        if index.get(item_id) is None or (videos and index[item_id] <= max(videos)):
            raise OverlaySourceError("OVERLAY_ORDER_REJECTED", f"Không đưa được overlay lên trên video trong scene '{scene}'")

    def ensure(self, url: str, video_sources: list[str]) -> dict:
        """Create or check our source and its scene items; returns what was changed. Raises OverlaySourceError."""
        changes: list[str] = []
        collection = self.current_collection()
        if collection != self.collection:
            if self.collection is not None:
                log.info("OBS đổi Scene Collection: %s → %s", self.collection, collection)
                changes.append("collection")
            self.collection, self.input_uuid, self.items = collection, None, {}
        self.canvas = self._canvas()
        scenes = self._video_scenes(video_sources)
        if not scenes:
            raise OverlaySourceError("OVERLAY_NO_VIDEO_SCENE", "Không scene nào chứa nguồn video của Sarae; chưa thêm được overlay",
                                     {"video_sources": video_sources})
        uuid = self._find_input()
        if uuid is None:
            first = next(iter(scenes))
            created = self._call("CreateInput", {"sceneName": first, "inputName": self._source_name(), "inputKind": BROWSER_KIND,
                                                 "inputSettings": self.settings(url), "sceneItemEnabled": False})
            uuid = created.get("inputUuid")
            if not uuid:
                raise OverlaySourceError("OVERLAY_CREATE_FAILED", "OBS không trả về Browser Source vừa tạo")
            self._remember(collection, uuid)
            log.info("Đã tạo Browser Source '%s' trong scene '%s'", self._source_name(), first)
            changes.append("created")
            scenes[first] = self._call("GetSceneItemList", {"sceneName": first}).get("sceneItems") or []
        if uuid != self.input_uuid:
            self.input_uuid, self.items = uuid, {}
        self._ensure_settings(url)
        items: dict[str, int] = {}
        for scene, scene_items in scenes.items():
            ours = [i for i in scene_items if i.get("sourceUuid") == uuid]
            if not ours:
                created = self._call("CreateSceneItem", {"sceneName": scene, "sourceUuid": uuid, "sceneItemEnabled": False})
                scene_items = self._call("GetSceneItemList", {"sceneName": scene}).get("sceneItems") or []
                ours = [i for i in scene_items if i.get("sceneItemId") == created.get("sceneItemId")]
                log.info("Đã thêm overlay vào scene '%s'", scene)
                changes.append(f"added:{scene}")
            item_id = ours[0]["sceneItemId"]
            for extra in ours[1:]:  # a copy of our own source pasted twice: kept but hidden
                self._call("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": extra["sceneItemId"], "sceneItemEnabled": False})
            if self.items.get(scene) not in (None, item_id):
                changes.append(f"item_changed:{scene}")
            if self._lock_transform(scene, item_id):
                changes.append(f"transform:{scene}")
            self._above_video(scene, item_id, scene_items, video_sources)
            items[scene] = item_id
        self.items = items
        if changes:
            log.info("Overlay OBS: %s", ", ".join(changes))
        return {"changes": changes, "scenes": dict(items), "canvas": list(self.canvas), "collection": collection}

    def set_visible(self, visible: bool) -> None:
        for scene, item_id in self.items.items():
            self._call("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": visible})
            applied = self._call("GetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id}).get("sceneItemEnabled")
            if applied is not visible:
                raise OverlaySourceError("OVERLAY_VISIBILITY_REJECTED", f"OBS không {'hiện' if visible else 'ẩn'} được overlay trong '{scene}'")

    def hide_everywhere(self) -> None:
        """Best effort: hide every scene item of our source that is known (failure is logged by the caller)."""
        self.set_visible(False)

    def refresh(self) -> None:
        """Reload the renderer page (the video and OBS keep running)."""
        if self.input_uuid:
            self._call("PressInputPropertiesButton", {"inputUuid": self.input_uuid, "propertyName": "refreshnocache"})

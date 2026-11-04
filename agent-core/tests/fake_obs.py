"""Raw obs-websocket emulator: what obsws_python.ReqClient.send(request, data, raw=True) would answer."""


class RequestError(Exception):
    """Like OBSSDKRequestError: OBS answered with a failure status."""

    def __init__(self, code):
        super().__init__(f"code {code}")
        self.code = code


class FakeWs:
    def __init__(self, inputs=("Sarae Video",), apply=True, scene="LIVE"):
        self.inputs = set(inputs)
        self.scene = scene
        self.scene_items = {name: i + 1 for i, name in enumerate(inputs)}
        self.settings: dict = {}
        self.media_state: dict = {}
        self.cursor: dict = {}
        self.muted: dict = {}
        self.enabled: dict = {}
        self.volume: dict = {}
        self.apply = apply
        self.broken = False
        self.calls: list[str] = []

    def send(self, request, data=None, raw=False):
        self.calls.append(request)
        if self.broken:
            raise ConnectionResetError("socket closed")
        data = data or {}
        name = data.get("inputName") or (data.get("sourceName") if request == "GetSceneItemId" else None)
        if name is not None and name not in self.inputs:
            raise RequestError(600)
        if request == "GetVersion":
            return {"obsVersion": "31.0.0", "obsWebSocketVersion": "5.5.0"}
        if request == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.scene}
        if request == "GetSceneItemId":
            return {"sceneItemId": self.scene_items[name]}
        if request == "GetSceneItemList":
            return {"sceneItems": [{"sourceName": n} for n in self.scene_items]}
        if request == "GetVideoSettings":
            return {"baseWidth": 1080, "baseHeight": 1920}
        if request == "SetSceneItemTransform":
            return {}
        if request == "GetInputList":
            return {"inputs": [{"inputName": n, "inputKind": "ffmpeg_source"} for n in sorted(self.inputs)]}
        if request == "SetInputSettings" and self.apply:
            self.settings.setdefault(name, {}).update(data["inputSettings"])
        if request == "GetInputSettings":
            return {"inputSettings": dict(self.settings.get(name, {}))}
        if request == "TriggerMediaInputAction":
            action = data["mediaAction"].rsplit("_", 1)[-1]
            self.media_state[name] = {"RESTART": "OBS_MEDIA_STATE_PLAYING", "PLAY": "OBS_MEDIA_STATE_PLAYING",
                                      "PAUSE": "OBS_MEDIA_STATE_PAUSED",
                                      "STOP": "OBS_MEDIA_STATE_STOPPED"}.get(action, self.media_state.get(name))
            if action == "RESTART":
                self.cursor[name] = 0
        if request == "GetMediaInputStatus":
            return {"mediaState": self.media_state.get(name, "OBS_MEDIA_STATE_PLAYING"), "mediaDuration": 60000,
                    "mediaCursor": self.cursor.get(name, 10)}
        if request == "SetInputMute":
            self.muted[name] = data["inputMuted"]
        if request == "GetInputMute":
            return {"inputMuted": self.muted.get(name, False)}
        if request == "SetSceneItemEnabled":
            self.enabled[data["sceneItemId"]] = data["sceneItemEnabled"]
        if request == "SetMediaInputCursor":
            self.cursor[name] = data["mediaCursor"]
        if request == "GetInputVolume":
            return {"inputVolumeDb": self.volume.get(name, 0.0)}
        if request == "SetInputVolume":
            self.volume[name] = data["inputVolumeDb"]
        return {}

    def disconnect(self):
        pass

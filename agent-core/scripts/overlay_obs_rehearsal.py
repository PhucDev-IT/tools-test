r"""Overlay rehearsal on the real local OBS (no TikTok LIVE): the real OverlayService, server and renderer bundle.

Usage (OBS running with obs-websocket, the Agent's saved OBS password, a scene "Scene" with "Sarae Video A/B" playing):
    .venv\Scripts\python.exe scripts\overlay_obs_rehearsal.py <output folder>

It creates (or adopts) the "Sarae Overlay" Browser Source in that scene, shows a test layout, checks positions by
pixel, compares OBS with a headless Chrome render of the same page, then drags, deletes, reconnects and reloads it.
The video is never touched; the overlay is hidden at the end. Never run it while streaming.
"""
import base64, hashlib, json, struct, sys, tempfile, threading, time, zlib
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cv2, numpy as np
from sarae_agent.config import ConfigStore
from sarae_agent.obs.controller import ObsController
from sarae_agent.overlay.cache import OverlayCache
from sarae_agent.overlay.service import OverlayService

OUT = Path(sys.argv[1]); OUT.mkdir(exist_ok=True)
MEDIA = "a898cc0ea6804a4faa9a898d3657418a"
VIDEO_SOURCES = ["Sarae Video", "Sarae Video A", "Sarae Video B"]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS" if ok else "FAIL"), name, detail, flush=True)


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


BLUE = png(64, 64, (0, 0, 255))


def overlay(**k):
    base = {"media_id": MEDIA, "template": "TEST", "label": "", "start_ms": 0, "duration_ms": 3_600_000, "product_id": None,
            "layout": {"x": 0.05, "y": 0.7, "width": 0.9, "height": 0.15, "z_index": 0, "opacity": 1},
            "animation_in": {"name": "none", "duration_ms": 0}, "animation_out": {"name": "none", "duration_ms": 0}, "elements": []}
    base.update(k)
    return base


def snapshot(revision, text="Sarae Overlay 123"):
    shape = {"kind": "shape", "shape": "rect", "fill": "#FF0000", "stroke": None, "stroke_width": 0, "radius": 0,
             "frame": {"x": 0, "y": 0, "width": 1, "height": 1}, "opacity": 1}
    label = {"kind": "text", "text": text, "frame": {"x": 0.05, "y": 0.1, "width": 0.9, "height": 0.8}, "opacity": 1,
             "style": {"font_size": 64, "font_weight": 700, "font_family": "", "color": "#FFFFFF", "background": None,
                       "align": "center", "max_lines": 0}}
    image = {"kind": "image", "source": "asset", "asset_id": "a" * 32, "product_id": None, "fit": "cover", "radius": 0,
             "frame": {"x": 0, "y": 0, "width": 1, "height": 1}, "opacity": 1}
    return {"revision": revision, "version": 1, "timeline_id": 1, "session_id": 1, "canvas": {"width": 1080, "height": 1920},
            "overlays": [overlay(id=1, elements=[shape, label]),
                         overlay(id=2, layout={"x": 0.6, "y": 0.1, "width": 0.35, "height": 0.2, "z_index": 1, "opacity": 1},
                                 elements=[image])],
            "products": {},
            "assets": {"a" * 32: {"url": "/api/live/overlay-assets/x/file?exp=1", "sha256": hashlib.sha256(BLUE).hexdigest(),
                                  "mime_type": "image/png", "width": 64, "height": 64, "size_bytes": len(BLUE)}}}


# Its own Browser Source and port: never the "Sarae Overlay" source of an Agent running on this PC.
TEST_SOURCE = "Sarae Overlay (test)"


class Cfg:
    def __init__(self):
        self.overlay = SimpleNamespace(enabled=True, port=17895, source_name=TEST_SOURCE, input_uuids={}, token="rehearsal-token")

    def get(self):
        return self

    def update(self, patch):
        for k, v in patch.get("overlay", {}).items():
            setattr(self.overlay, k, v)


obs = ObsController(ConfigStore(), lambda *a: None)
obs.connect()
if obs.request("GetStreamStatus").get("outputActive"):
    sys.exit("OBS is streaming: the rehearsal never runs during a broadcast")
current = {"snap": snapshot("rev-1")}
svc = OverlayService(Cfg(), obs.request, lambda: obs.status().get("connected", False), lambda: VIDEO_SOURCES,
                     lambda sid: current["snap"], lambda url: BLUE, OverlayCache(Path(tempfile.mkdtemp())))
svc.TICK_SECONDS = 0.5
svc.start()
stop = threading.Event()


def feed():  # the PlaybackEngine's role: report the active source's real cursor
    while not stop.wait(0.5):
        try:
            st = obs.request("GetMediaInputStatus", {"inputName": "Sarae Video A"})
            svc.observe(1, MEDIA, int(st.get("mediaCursor") or 0), st.get("mediaState"), 0)
        except Exception:
            svc.observe(1, MEDIA, 0, "OBS_MEDIA_STATE_PAUSED", 0)


threading.Thread(target=feed, daemon=True).start()


def wait(pred, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def video():
    st = obs.request("GetMediaInputStatus", {"inputName": "Sarae Video A"})
    return st.get("mediaState"), st.get("mediaCursor")


def items():
    return obs.request("GetSceneItemList", {"sceneName": "Scene"})["sceneItems"]


def ours():
    return [i for i in items() if i["sourceUuid"] == svc.source.input_uuid]


def shot(source, name):
    data = obs.request("GetSourceScreenshot", {"sourceName": source, "imageFormat": "png", "imageWidth": 1080, "imageHeight": 1920})["imageData"]
    raw = base64.b64decode(data.split(",", 1)[1])
    (OUT / name).write_bytes(raw)
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)


def bbox(mask):
    ys, xs = np.nonzero(mask)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if len(xs) else None


v0 = video()
print("video before", v0)
check("overlay shown after renderer READY", wait(lambda: svc.status()["state"] == "showing", 40), json.dumps(svc.status(), default=str)[:400])
mine = ours()
check("one scene item of our source in the video scene", len(mine) == 1, str([(i["sceneItemId"], i["sceneItemIndex"]) for i in mine]))
idx = {i["sourceName"]: i["sceneItemIndex"] for i in items()}
check("overlay above the video sources", bool(mine) and mine[0]["sceneItemIndex"] > max(idx.get("Sarae Video A", -1), idx.get("Sarae Video B", -1)), str(idx))
t = obs.request("GetSceneItemTransform", {"sceneName": "Scene", "sceneItemId": mine[0]["sceneItemId"]})["sceneItemTransform"]
got = (t["positionX"], t["positionY"], t["rotation"], t["cropLeft"] + t["cropRight"] + t["cropTop"] + t["cropBottom"],
       t["boundsType"], t["alignment"], round(t["width"]), round(t["height"]))
check("transform locked (read back)", got == (0, 0, 0, 0, "OBS_BOUNDS_NONE", 5, 1080, 1920), str(got))
time.sleep(1.5)
img = shot(TEST_SOURCE, "obs_source.png")
red = (img[:, :, 2] > 200) & (img[:, :, 1] < 60) & (img[:, :, 0] < 60) & (img[:, :, 3] > 200)
blue = (img[:, :, 0] > 200) & (img[:, :, 1] < 60) & (img[:, :, 2] < 60) & (img[:, :, 3] > 200)
rb, bb = bbox(red), bbox(blue)
check("red box at (54,1344)-(1026,1632) +-2px", bool(rb) and all(abs(a - b) <= 2 for a, b in zip(rb, (54, 1344, 1026, 1632))), str(rb))
check("image box at (648,192)-(1026,576) +-2px", bool(bb) and all(abs(a - b) <= 2 for a, b in zip(bb, (648, 192, 1026, 576))), str(bb))
check("background transparent", img[100, 100, 3] == 0 and img[1900, 10, 3] == 0, str((img[100, 100].tolist(), img[1900, 10].tolist())))
white = (img[:, :, :3].min(axis=2) > 200) & (img[:, :, 3] > 200)
check("text drawn inside the red box", white[1344:1632, 54:1026].sum() > 2000, str(int(white.sum())))
prog = shot("Scene", "obs_program.png")
check("program output shows the overlay over the video",
      ((prog[:, :, 2] > 200) & (prog[:, :, 1] < 60) & (prog[:, :, 0] < 60))[1400:1600, 100:1000].mean() > 0.6)

import subprocess
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
web_png = OUT / "chrome_renderer.png"
web_png.unlink(missing_ok=True)
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                "--window-size=1096,2015", "--screen-info={0,0 1096x2015}", "--default-background-color=00000000", "--virtual-time-budget=6000",
                f"--user-data-dir={tempfile.mkdtemp()}", f"--screenshot={web_png}", svc.url], timeout=60,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
web = cv2.imread(str(web_png), cv2.IMREAD_UNCHANGED)
web = None if web is None else web[:1920, :1080]  # headless window frame: the page viewport is the top-left 1080x1920
if web is not None and web.shape[2] == 3:
    web = np.dstack([web, np.full(web.shape[:2], 255, np.uint8)])
same_size = web is not None and web.shape[:2] == img.shape[:2]
check("Chrome renders the same page at 1080x1920", same_size, str(None if web is None else web.shape))
if same_size:
    visible = (img[:, :, 3] > 0) | (web[:, :, 3] > 0)
    diff = np.abs(img.astype(int) - web.astype(int)).max(axis=2)
    bad = (diff > 64) & visible
    ratio = bad.sum() / max(visible.sum(), 1)
    cv2.imwrite(str(OUT / "diff.png"), (bad * 255).astype(np.uint8))
    wred = (web[:, :, 2] > 200) & (web[:, :, 1] < 60) & (web[:, :, 0] < 60) & (web[:, :, 3] > 200)
    check("web and OBS boxes identical", bbox(wred) == rb, f"{bbox(wred)} vs {rb}")
    check("web vs OBS pixels differ < 1% (font anti-aliasing tolerance)", ratio < 0.01, f"{ratio:.4%} of {int(visible.sum())} drawn px")

samples = []
watcher = ObsController(ConfigStore(), lambda *a: None)
watcher.connect()
def sample():
    while not stop.is_set():
        try:
            enabled = {i["sourceName"]: i["sceneItemEnabled"] for i in watcher.request("GetSceneItemList", {"sceneName": "Scene"})["sceneItems"]}
            shown = "Sarae Video A" if enabled.get("Sarae Video A") else "Sarae Video B"
            samples.append((time.time(), shown, watcher.request("GetMediaInputStatus", {"inputName": shown}).get("mediaState")))
        except Exception as e:
            samples.append((time.time(), "?", f"ERR {e}"))
        time.sleep(0.3)
threading.Thread(target=sample, daemon=True).start()

obs.request("SetSceneItemTransform", {"sceneName": "Scene", "sceneItemId": mine[0]["sceneItemId"],
                                       "sceneItemTransform": {"positionX": 200.0, "scaleX": 0.5, "scaleY": 0.5, "cropTop": 40}})


def locked():
    t = obs.request("GetSceneItemTransform", {"sceneName": "Scene", "sceneItemId": ours()[0]["sceneItemId"]})["sceneItemTransform"]
    return t["positionX"] == 0 and abs(t["scaleX"] - 1) < 1e-3 and t["cropTop"] == 0


check("moved/scaled/cropped source is locked again", wait(locked, 10))
obs.request("SetSceneItemIndex", {"sceneName": "Scene", "sceneItemId": ours()[0]["sceneItemId"], "sceneItemIndex": 0})
check("source moved under the video goes back on top", wait(lambda: ours()[0]["sceneItemIndex"] == len(items()) - 1, 10))

old_uuid = svc.source.input_uuid
obs.request("RemoveInput", {"inputUuid": old_uuid})
check("deleted source is created again and shown",
      wait(lambda: svc.source.input_uuid not in (None, old_uuid) and ours() and ours()[0]["sceneItemEnabled"], 40), str(svc.source.input_uuid))

obs.reconnect_later()
time.sleep(2)
check("after an OBS reconnect the overlay is shown",
      wait(lambda: obs.status()["connected"] and ours() and ours()[0]["sceneItemEnabled"] and svc.status()["state"] == "showing", 20))

svc.ready_revision = None
obs.request("PressInputPropertiesButton", {"inputUuid": svc.source.input_uuid, "propertyName": "refreshnocache"})
check("reloaded renderer reports READY again", wait(lambda: svc.ready_revision == "rev-1", 20))

current["snap"] = snapshot("rev-2", text="Phien ban 2")
svc.last_sync = 0
seen_hidden = []


def watch_new():
    if not ours()[0]["sceneItemEnabled"]:
        seen_hidden.append(time.time())
    return svc.status()["revision"] == "rev-2"


check("new version applied after READY", wait(watch_new, 30), json.dumps(svc.status(), default=str)[:200])
check("overlay never hidden while switching version", not seen_hidden, str(len(seen_hidden)))
time.sleep(1)
shot(TEST_SOURCE, "obs_source_v2.png")

# Timing against the real OBS cursor: an overlay from cursor+4 s for 3 s must appear and disappear on time.
state, cursor, duration = (lambda st: (st.get("mediaState"), st.get("mediaCursor") or 0, st.get("mediaDuration") or 0))(
    obs.request("GetMediaInputStatus", {"inputName": "Sarae Video A"}))
if state == "OBS_MEDIA_STATE_PLAYING" and duration - cursor > 15_000:
    start = int(cursor) + 4_000
    timed = snapshot("rev-3")
    timed["overlays"] = [overlay(id=7, start_ms=start, duration_ms=3_000, elements=timed["overlays"][0]["elements"])]
    current["snap"] = timed
    svc.request_sync()
    wait(lambda: svc.status()["revision"] == "rev-3", 20)
    # Measure from the moment the renderer says (ACK) it draws rev-3, not the frame of rev-2 still on screen.
    wait(lambda: (svc.status()["renderer_ack"] or {}).get("revision") == "rev-3", 10)
    seen = []
    end = time.time() + 12
    while time.time() < end:
        cur = obs.request("GetMediaInputStatus", {"inputName": "Sarae Video A"}).get("mediaCursor") or 0
        frame = shot(TEST_SOURCE, "timing.png")
        red_now = ((frame[:, :, 2] > 200) & (frame[:, :, 1] < 60) & (frame[:, :, 3] > 200))[1400:1600, 100:1000].mean() > 0.5
        seen.append((int(cur) - start, bool(red_now)))
        time.sleep(0.15)
    shown_at = [d for d, red in seen if red]
    first, last = (min(shown_at), max(shown_at)) if shown_at else (None, None)
    # Tolerance: one screenshot round trip + one position report (500 ms here; the engine reports every 1 s).
    check("timed overlay appears at its start (+-700 ms)", first is not None and -700 <= first <= 700, f"first red at {first} ms")
    check("timed overlay disappears at its end (+-700 ms)", last is not None and 2_300 <= last <= 3_700, f"last red at {last} ms")
    check("nothing shown well before or after", not any(red for d, red in seen if d < -800 or d > 3_800), str(seen[:3]))
else:
    print("SKIP timing: Sarae Video A is not playing with 15 s left", state, cursor, duration)

v1 = video()
print("video after", v1)
check("video kept its state (not restarted by the overlay)",
      v0[0] == v1[0] and (v0[0] != "OBS_MEDIA_STATE_PLAYING" or (v1[1] or 0) > (v0[1] or 0)), f"{v0} -> {v1}")
gaps = [s for s in samples if s[2] != "OBS_MEDIA_STATE_PLAYING"]
if v0[0] == "OBS_MEDIA_STATE_PLAYING":
    check("visible video source PLAYING in every sample during the operations", not gaps and len(samples) > 10,
          f"{len(samples)} samples, not playing: {gaps[:5]}")
else:
    print("SKIP video continuity: the video was not playing when the rehearsal started", v0)
stop.set()
svc.close()
check("hidden when the Agent closes", not ours()[0]["sceneItemEnabled"])
print(json.dumps({"pass": sum(ok for _, ok, _ in results), "total": len(results), "fail": [n for n, ok, _ in results if not ok]}))
obs.close()

r"""Overlay soak on the real local OBS (no TikTok LIVE): performance and robustness before release.

Usage (OBS running, not streaming; the "Sarae Overlay" setup of overlay_obs_rehearsal.py):
    .venv\Scripts\python.exe scripts\overlay_obs_soak.py <output folder> [minutes]

Measures, with the real OverlayService / server / renderer:
- OBS FPS and skipped render frames while the overlay is toggled on and off every second;
- CPU / RAM of OBS and of its browser processes (obs-browser-page), and of this Agent process, before and after;
- the Backend lost after the overlay was prepared: the overlay keeps showing what was loaded;
- a long run where the overlay content changes every 5 s (new revision each time): every revision READY, no errors,
  memory growth reported;
- the image cache stays within its budget.
The video is never touched; the overlay is hidden at the end. It refuses to run while OBS is streaming.
"""
import hashlib
import json
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import psutil  # noqa: E402

from sarae_agent.config import ConfigStore  # noqa: E402
from sarae_agent.obs.controller import ObsController  # noqa: E402
from sarae_agent.overlay.cache import OverlayCache  # noqa: E402
from sarae_agent.overlay.service import OverlayService  # noqa: E402

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
MINUTES = float(sys.argv[2]) if len(sys.argv) > 2 else 3
MEDIA = "a898cc0ea6804a4faa9a898d3657418a"
results = []


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS" if ok else "FAIL"), name, detail, flush=True)


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


IMAGES = [png(256, 256, (i * 40 % 256, 120, 255 - i * 40 % 256)) for i in range(6)]


def snapshot(n):
    image = IMAGES[n % len(IMAGES)]
    aid = f"{n % len(IMAGES):032x}"
    def overlay(oid, x, y, w, h, elements, z=0):
        return {"id": oid, "media_id": MEDIA, "template": "SOAK", "start_ms": 0, "duration_ms": 3_600_000, "enabled": True,
                "layout": {"x": x, "y": y, "width": w, "height": h, "z_index": z, "opacity": 1},
                "animation_in": {"name": "fade", "duration_ms": 300}, "animation_out": {"name": "fade", "duration_ms": 300},
                "elements": elements}
    text = {"kind": "text", "text": f"Phiên bản {n} · Áo sơ mi preppy", "frame": {"x": 0.05, "y": 0.1, "width": 0.9, "height": 0.8}, "opacity": 1,
            "style": {"font_size": 54, "font_weight": 700, "font_family": "Georgia", "color": "#1C1A17", "background": None, "align": "center", "max_lines": 2}}
    card = {"kind": "shape", "shape": "rect", "fill": "#F7F1E8F2", "stroke": None, "stroke_width": 0, "radius": 0.2,
            "frame": {"x": 0, "y": 0, "width": 1, "height": 1}, "opacity": 1}
    img = {"kind": "image", "source": "asset", "asset_id": aid, "product_id": None, "fit": "cover", "radius": 0.08,
           "frame": {"x": 0, "y": 0, "width": 1, "height": 1}, "opacity": 1}
    return {"revision": f"soak-{n}", "version": n, "timeline_id": n, "products": {},
            "overlays": [overlay(1, 0.05, 0.72, 0.9, 0.12, [card, text]), overlay(2, 0.6, 0.12, 0.35, 0.2, [img], 1)],
            "assets": {aid: {"url": f"/api/x/{aid}", "sha256": hashlib.sha256(image).hexdigest()}}}


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


def processes():
    """OBS and its browser (CEF) processes: CPU % over 1 s and RSS MB."""
    found = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() in ("obs64.exe", "obs-browser-page.exe")]
    for p in found:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(1)
    out = {}
    for p in found:
        try:
            name = p.info["name"].lower()
            entry = out.setdefault(name, {"cpu": 0.0, "rss_mb": 0.0, "count": 0})
            entry["cpu"] += p.cpu_percent(None)
            entry["rss_mb"] += p.memory_info().rss / 1024 / 1024
            entry["count"] += 1
        except psutil.Error:
            pass
    return {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in out.items()}


obs = ObsController(ConfigStore(), lambda *a: None)
obs.connect()
if obs.request("GetStreamStatus").get("outputActive"):
    sys.exit("OBS is streaming: the soak never runs during a broadcast")
current = {"n": 1, "offline": False}
cache_dir = Path(tempfile.mkdtemp())


def fetch_snapshot(_sid):
    if current["offline"]:
        raise ConnectionError("Backend unreachable (simulated)")
    return snapshot(current["n"])


def fetch_image(url):
    if current["offline"]:
        raise ConnectionError("Backend unreachable (simulated)")
    return IMAGES[int(url.rsplit("/", 1)[-1], 16) % len(IMAGES)]


svc = OverlayService(Cfg(), obs.request, lambda: obs.status().get("connected", False),
                     lambda: ["Sarae Video", "Sarae Video A", "Sarae Video B"], fetch_snapshot, fetch_image, OverlayCache(cache_dir))
svc.TICK_SECONDS = 0.5
svc.SYNC_SECONDS = 2.0
svc.start()
stop = threading.Event()


def feed():
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


me = psutil.Process()
check("overlay prepared and shown", wait(lambda: svc.status()["state"] == "showing", 40), svc.status()["state"])

# ----- FPS while toggling the overlay ------------------------------------------------------------------------------
fps = float(obs.request("GetVideoSettings")["fpsNumerator"]) / float(obs.request("GetVideoSettings")["fpsDenominator"] or 1)
before_stats, before_proc = obs.request("GetStats"), processes()
items = svc.source.items
for i in range(20):
    for scene, item_id in items.items():
        obs.request("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": i % 2 == 1})
    time.sleep(1)
after_stats, after_proc = obs.request("GetStats"), processes()
rendered = after_stats["renderTotalFrames"] - before_stats["renderTotalFrames"]
skipped = after_stats["renderSkippedFrames"] - before_stats["renderSkippedFrames"]
check("OBS keeps its FPS while the overlay toggles on/off (20 s)", after_stats["activeFps"] >= fps - 1 and skipped <= max(2, rendered * 0.005),
      f"activeFps {after_stats['activeFps']:.1f}/{fps:g}, skipped {skipped}/{rendered} render frames, avg render {after_stats['averageFrameRenderTime']:.2f} ms")
wait(lambda: svc.status()["state"] == "showing", 10)

# ----- Backend lost after preload ----------------------------------------------------------------------------------
current["offline"] = True
time.sleep(12)
check("Backend lost after preload: the loaded overlay keeps showing", svc.status()["state"] == "showing" and svc.status()["revision"] == "soak-1",
      json.dumps({k: svc.status()[k] for k in ("state", "revision", "error")}, ensure_ascii=False))
current["offline"] = False

# ----- long run, content changing every 5 s ------------------------------------------------------------------------
start_proc, start_rss = processes(), me.memory_info().rss / 1024 / 1024
start_obs = obs.request("GetStats")
deadline = time.time() + MINUTES * 60
applied, missed = 0, []
while time.time() < deadline:
    current["n"] += 1
    svc.request_sync()
    if wait(lambda: svc.status()["revision"] == f"soak-{current['n']}", 15):
        applied += 1
    else:
        missed.append(current["n"])
    time.sleep(max(0.0, 5 - 0))
end_obs, end_proc, end_rss = obs.request("GetStats"), processes(), me.memory_info().rss / 1024 / 1024
check(f"{applied} content changes in {MINUTES:g} min, each applied after READY", not missed and applied > 0, f"missed {missed[:10]}")
check("still shown at the end, no error", svc.status()["state"] == "showing" and not svc.status()["error"], json.dumps(svc.status()["error"]))
skipped = end_obs["renderSkippedFrames"] - start_obs["renderSkippedFrames"]
rendered = end_obs["renderTotalFrames"] - start_obs["renderTotalFrames"]
check("OBS render frames skipped during the long run < 0.5 %", skipped <= max(2, rendered * 0.005), f"{skipped}/{rendered}")
browser_growth = end_proc.get("obs-browser-page.exe", {}).get("rss_mb", 0) - start_proc.get("obs-browser-page.exe", {}).get("rss_mb", 0)
check("OBS browser processes memory growth < 150 MB", browser_growth < 150, f"{browser_growth:+.1f} MB")
check("Agent process memory growth < 50 MB", end_rss - start_rss < 50, f"{start_rss:.0f} -> {end_rss:.0f} MB")
cache_mb = sum(p.stat().st_size for p in cache_dir.iterdir()) / 1024 / 1024
check("image cache within budget", cache_mb < 300, f"{cache_mb:.2f} MB, {len(list(cache_dir.iterdir()))} files")

stop.set()
svc.close()
report = {"results": results, "fps": fps, "obs_before": before_stats, "obs_after_toggle": after_stats, "obs_end": end_obs,
          "processes_before": before_proc, "processes_after_toggle": after_proc, "processes_soak_start": start_proc,
          "processes_soak_end": end_proc, "agent_rss_mb": [round(start_rss, 1), round(end_rss, 1)], "minutes": MINUTES}
(OUT / "soak_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"pass": sum(r["ok"] for r in results), "total": len(results), "fail": [r["check"] for r in results if not r["ok"]]}))
print("processes", json.dumps({"before": before_proc, "toggle": after_proc, "soak_end": end_proc}))
obs.close()

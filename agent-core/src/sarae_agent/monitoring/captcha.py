"""Verification monitor for TikTok LIVE Studio: inactivity warning and CAPTCHA challenge detection.

Captures only the Studio window rectangle and matches gradient maps of complete UI regions, one
template per interface layout (resources/templates.json records each crop's source, region, ignored
dynamic content and purpose). Classification keeps three event types apart:

  INACTIVITY_WARNING    the "LIVE will end in" popup with the inactivity paragraph (it may also carry a challenge)
  CAPTCHA_CHALLENGE     a challenge layout (shapes / puzzle) without that warning
  UNKNOWN_VERIFICATION  close to a known layout but below the threshold, ambiguous, or an unclassified Cloud image

An alert is raised after two consecutive positive frames. It never clicks, solves or submits.
Screen capture handles (mss) live only inside the worker thread and are closed on stop.
"""
from __future__ import annotations

import hashlib
import base64
import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable

from sarae_agent.config import ConfigStore, MonitorConfig, paths
from sarae_agent.errors import AgentError
from sarae_agent.monitoring.studio import find_studio_window, studio_process_running
from sarae_agent.windows import win32

log = logging.getLogger("sarae.monitor")

TEMPLATE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_TEMPLATE_BYTES = 3_000_000
CONFIRM_FRAMES = 2
BUILTIN_DIR = Path(__file__).with_name("resources")
WORK_SCALE = 0.5            # frames and templates are matched at half resolution
SCALES = tuple(round(0.6 + 0.08 * step, 2) for step in range(13))   # coarse UI scale search 0.6-1.56
REFINE = (-0.06, -0.04, -0.02, 0.0, 0.02, 0.04, 0.06)
AMBIGUITY_MARGIN = 0.08     # shapes vs puzzle closer than this: the challenge type is left undecided
MIN_FRAME_VARIANCE = 25.0   # gradient variance below this under a template: flat area, never a match
EVENT_LABELS = {"INACTIVITY_WARNING": "Cảnh báo LIVE không hoạt động, TikTok yêu cầu xác minh",
                "CAPTCHA_CHALLENGE": "Hộp thoại CAPTCHA",
                "UNKNOWN_VERIFICATION": "Giao diện nghi là xác minh, chưa phân loại được"}
CHALLENGE_LABELS = {"SHAPES": "chọn 2 vật cùng hình dạng", "PUZZLE": "kéo mảnh ghép"}


@lru_cache(maxsize=4)
def _load_specs(folder: str) -> dict[str, dict]:
    try:
        data = json.loads((Path(folder) / "templates.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {t["file"]: t for t in data.get("templates", []) if isinstance(t, dict) and t.get("file")}


def template_specs() -> dict[str, dict]:
    """Built-in template metadata keyed by file name (written by scripts/build_captcha_templates.py)."""
    return _load_specs(str(BUILTIN_DIR))


def mask_name(name: str) -> str:
    return name[:-4] + "_mask.png"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cv():
    if not win32.IS_WINDOWS:
        raise AgentError("MONITOR_UNSUPPORTED", "CAPTCHA Monitor cần Windows Desktop")
    try:
        import cv2
        import mss
        import numpy
    except ImportError as exc:
        raise AgentError("MONITOR_DEPENDENCY_MISSING", f"Thiếu thư viện giám sát: {exc.name}") from exc
    return cv2, numpy, mss


def gradient_maps(cv2, np, gray) -> dict:
    """Sobel magnitude in two domains: "gray" saturates at 255, "edges" wraps like the reference solver's
    uint8 cast, so its edge-map sample can be matched exactly as supplied."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(gx, gy)
    return {"gray": np.minimum(magnitude, 255), "edges": (magnitude.astype(np.int64) & 255).astype(np.float32)}


def _level(cv2, np, image) -> dict:
    total, squares = cv2.integral2(image, sdepth=cv2.CV_64F, sqdepth=cv2.CV_64F)
    return {"image": image, "sum": total, "sqsum": squares}


def frame_maps(cv2, np, gray, *, edge_input: bool = False) -> dict:
    """Gradient maps of one frame at half (fine) and quarter (coarse) resolution, per domain.
    edge_input: the frame already is a reference-solver edge map."""
    maps = {"edges": gray.astype(np.float32)} if edge_input else gradient_maps(cv2, np, gray)
    result = {}
    for domain, image in maps.items():
        fine = cv2.resize(image, None, fx=WORK_SCALE, fy=WORK_SCALE, interpolation=cv2.INTER_AREA)
        coarse = cv2.resize(fine, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        result[domain] = {"fine": _level(cv2, np, fine),
                          "coarse": _level(cv2, np, coarse) if min(coarse.shape) >= 120 else None}
    return result


def build_mask(np, shape, spec: dict | None):
    """Mask (255 = compare) from the spec's ignored regions, only for the built-in crop size."""
    if not spec or spec.get("ignore") is None:
        return None
    left, top, right, bottom = spec["crop"]
    if shape != (bottom - top, right - left):
        return None
    mask = np.full(shape, 255, np.uint8)
    for l, t, r, b in spec["ignore"]:
        mask[t:b, l:r] = 0
    for l, t, r, b, w in spec.get("bands", []):
        half = w // 2
        mask[max(0, t - half):t + half + 1, l:r + 1] = 0
        mask[max(0, b - half):b + half + 1, l:r + 1] = 0
        mask[t:b + 1, max(0, l - half):l + half + 1] = 0
        mask[t:b + 1, max(0, r - half):r + half + 1] = 0
    return mask


def prepare_template(cv2, np, image, mask=None, domain: str = "gray") -> dict:
    """Gradient maps of one template; an "edges" template already is an edge map and has no "gray" map."""
    if mask is not None and (mask.shape != image.shape or cv2.countNonZero(mask) < 25):
        raise ValueError("Mask không hợp lệ")
    maps = {"edges": image.astype(np.float32)} if domain == "edges" else gradient_maps(cv2, np, image)
    detail = maps.get("gray", maps["edges"]) > 40
    if mask is not None:
        detail &= mask > 0
    if int(detail.sum()) < 25:
        raise ValueError("Ảnh mẫu thiếu chi tiết cạnh")
    return {"maps": maps, "mask": mask}


def _best(cv2, np, level: dict, template, mask, scales, work: float) -> tuple[float, float, tuple]:
    """(score, scale, (x, y, w, h) of the best window in level pixels) over the given UI scales."""
    frame = level["image"]
    best_score, best_scale, best_box = -1.0, 1.0, (0, 0, 0, 0)
    for scale in scales:
        w, h = round(template.shape[1] * scale * work), round(template.shape[0] * scale * work)
        if w < 24 or h < 16 or w > frame.shape[1] or h > frame.shape[0]:
            continue
        resized = cv2.resize(template, (w, h), interpolation=cv2.INTER_AREA)
        scaled_mask = None if mask is None else cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
        response = cv2.matchTemplate(frame, resized, cv2.TM_CCOEFF_NORMED, mask=scaled_mask)
        # Flat screen areas (almost no gradient under the template) make the normalized score meaningless.
        total, squares, n = level["sum"], level["sqsum"], float(w * h)
        mean = (total[h:, w:] - total[:-h, w:] - total[h:, :-w] + total[:-h, :-w]) / n
        variance = (squares[h:, w:] - squares[:-h, w:] - squares[h:, :-w] + squares[:-h, :-w]) / n - mean * mean
        response[(variance < MIN_FRAME_VARIANCE) | ~np.isfinite(response)] = -1.0
        _, score, _, (x, y) = cv2.minMaxLoc(response)
        if score > best_score:
            best_score, best_scale, best_box = score, scale, (x, y, w, h)
    return best_score, best_scale, best_box


def match_prepared(cv2, np, frame: dict, template: dict) -> tuple[float, float]:
    """Best (score, UI scale) of one prepared template on frame_maps(): coarse scale search at quarter
    resolution, then refinement at half resolution around the best coarse scale."""
    domain = "gray" if "gray" in frame and "gray" in template["maps"] else "edges"
    image, mask, levels = template["maps"][domain], template["mask"], frame[domain]
    if levels["coarse"] is None or min(image.shape) * SCALES[0] * WORK_SCALE / 2 < 16:
        # small frame or small template: the coarse level cannot hold it, search the fine level directly
        score, scale, _ = _best(cv2, np, levels["fine"], image, mask, SCALES, WORK_SCALE)
        if score < 0:
            return 0.0, 1.0
        refined = _best(cv2, np, levels["fine"], image, mask, [round(scale + d, 2) for d in REFINE if d], WORK_SCALE)
        return max((score, scale), refined[:2])
    score, scale, (x, y, w, h) = _best(cv2, np, levels["coarse"], image, mask, SCALES, WORK_SCALE / 2)
    if score < 0:
        return 0.0, 1.0
    # Refine only around the coarse window: full scale step of slack plus a few pixels.
    fine = levels["fine"]["image"]
    margin_x, margin_y = int(w * 0.2) + 8, int(h * 0.2) + 8
    left, top = max(0, 2 * x - margin_x), max(0, 2 * y - margin_y)
    right, bottom = min(fine.shape[1], 2 * (x + w) + margin_x), min(fine.shape[0], 2 * (y + h) + margin_y)
    roi = _level(cv2, np, fine[top:bottom, left:right])
    refined = _best(cv2, np, roi, image, mask, [round(scale + d, 2) for d in REFINE], WORK_SCALE)
    return refined[:2] if refined[0] >= 0 else (score, scale)


def match_reference(cv2, np, frame_gray, reference_gray, mask=None) -> tuple[float, float]:
    """Best normalized match of one grayscale reference on one grayscale frame across display scales."""
    template = prepare_template(cv2, np, reference_gray, mask)
    if (reference_gray.shape[0] * SCALES[0] > frame_gray.shape[0] or
            reference_gray.shape[1] * SCALES[0] > frame_gray.shape[1]):
        raise ValueError("Không thể so khớp ảnh mẫu với màn hình")
    return match_prepared(cv2, np, frame_maps(cv2, np, frame_gray), template)


def classify_scores(scores: dict[str, tuple[float, float]], threshold: float,
                    specs: dict[str, dict] | None = None) -> dict:
    """Turn per-template (score, scale) into one event. Warning and challenge evidence are independent;
    a score below the threshold, a shapes/puzzle tie or an unclassified Cloud image never becomes a known type."""
    specs = template_specs() if specs is None else specs
    passed: dict[str, float] = {}
    grey: list[str] = []
    for name, (score, _) in scores.items():
        spec = specs.get(name)
        if spec is None:          # Cloud image without metadata: evidence for UNKNOWN only
            if score >= threshold:
                grey.append(name)
        elif score >= max(threshold, spec["min_score"]):
            passed[name] = score
        elif spec.get("unknown_floor") is not None and score >= spec["unknown_floor"]:
            grey.append(name)
    warnings = [n for n in passed if specs[n]["role"] == "warning"]
    by_type: dict[str, str] = {}
    for name in passed:
        kind = specs[name].get("challenge_type")
        if specs[name]["role"] == "challenge" and kind and (kind not in by_type or passed[name] > passed[by_type[kind]]):
            by_type[kind] = name
    ranked = sorted(by_type.items(), key=lambda item: passed[item[1]], reverse=True)
    ambiguous = len(ranked) > 1 and passed[ranked[0][1]] - passed[ranked[1][1]] < AMBIGUITY_MARGIN
    challenge_type = ranked[0][0] if ranked and not ambiguous else None
    if warnings:
        kind, deciding = "INACTIVITY_WARNING", max(warnings, key=passed.get)
    elif challenge_type:
        kind, deciding = "CAPTCHA_CHALLENGE", by_type[challenge_type]
    elif ambiguous or grey:
        kind = "UNKNOWN_VERIFICATION"
        deciding = ranked[0][1] if ambiguous else max(grey, key=lambda n: scores[n][0])
    else:
        kind, deciding = "NONE", max(scores, key=lambda n: scores[n][0], default=None)
    return {"kind": kind, "challenge_type": challenge_type, "ambiguous": ambiguous,
            "score": scores[deciding][0] if deciding else 0.0, "scale": scores[deciding][1] if deciding else 1.0,
            "template": deciding, "challenge_template": by_type.get(challenge_type) if challenge_type else None}


def describe(finding: dict) -> str:
    """Operator-facing text naming the actual event type; a warning popup is never called a CAPTCHA box."""
    text = EVENT_LABELS.get(finding["kind"], finding["kind"])
    if finding.get("challenge_type"):
        prefix = "kèm CAPTCHA" if finding["kind"] == "INACTIVITY_WARNING" else "dạng"
        text += f" ({prefix} {CHALLENGE_LABELS.get(finding['challenge_type'], finding['challenge_type'])})"
    elif finding["kind"] == "INACTIVITY_WARNING":
        text += " (chưa nhận ra dạng CAPTCHA)"
    if finding.get("ambiguous"):
        text += "; không phân biệt được ghép hình hay chọn hình"
    return f"{text}, mẫu {finding['template']}, điểm {round(finding['score'], 3)}"


def analyze_frame(cv2, np, gray, templates, threshold: float, *, edge_input: bool = False) -> dict:
    """Score (name, prepared template) pairs on one grayscale frame and classify. Shared by the monitor loop
    and the offline tests on supplied screenshots."""
    maps = frame_maps(cv2, np, gray, edge_input=edge_input)
    scores = {name: match_prepared(cv2, np, maps, template) for name, template in templates}
    return {**classify_scores(scores, threshold), "scores": scores}


def builtin_templates(cv2, np) -> list[tuple[str, dict]]:
    """(name, prepared template) for every bundled template, ignoring local overrides."""
    result = []
    for name, spec in template_specs().items():
        image = cv2.imread(str(BUILTIN_DIR / name), cv2.IMREAD_GRAYSCALE)
        if image is not None:
            result.append((name, prepare_template(cv2, np, image, build_mask(np, image.shape, spec),
                                                  spec.get("domain", "gray"))))
    return result


class CaptchaMonitor:
    def __init__(self, config: ConfigStore, emit: Callable[[str, dict], None]):
        self._config = config
        self._emit = emit
        self._guard = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.events: deque[dict] = deque(maxlen=100)
        self.state: dict = {"running": False, "status": "idle", "window_found": False, "window_visible": False,
                            "window_title": "", "window_info": None, "process_running": None,
                            "last_scan": None, "error": None, "events_count": 0, "match_score": None,
                            "matched_template": None, "monitors_detected": None, "capture_bounds": None}
        self._detected = False
        self._detected_kind: str | None = None
        self._template_cache: dict[Path, tuple[int, int, object | None]] = {}
        self._mask_cache: dict[Path, tuple[int, int, object | None]] = {}

    # --- templates ------------------------------------------------------------------------
    def _read_template(self, path: Path):
        """Cache decoded grayscale images by file identity; corrupt files stay unavailable."""
        try:
            stat = path.stat()
        except OSError:
            self._template_cache.pop(path, None)
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._template_cache.get(path)
        if cached and cached[:2] == key:
            return cached[2]
        cv2, _, _ = load_cv()
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is not None and (min(image.shape) < 20 or max(image.shape) > 1800 or
                                  cv2.countNonZero(cv2.Canny(image, 70, 160)) < 25):
            image = None
        self._template_cache[path] = (*key, image)
        return image

    def _read_mask(self, path: Path, shape):
        try:
            stat = path.stat()
        except OSError:
            self._mask_cache.pop(path, None)
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._mask_cache.get(path)
        if cached and cached[:2] == key:
            image = cached[2]
        else:
            cv2, _, _ = load_cv()
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            self._mask_cache[path] = (*key, image)
        cv2, _, _ = load_cv()
        return image if image is not None and image.shape == shape and cv2.countNonZero(image) >= 25 else None

    def _candidates(self):
        """(name, source, prepared template): custom override, else bundled, for each built-in layout, then the
        Cloud images. A custom image keeps the built-in ignore regions only when it has the same size."""
        cv2, np, _ = load_cv()
        custom = paths.monitor_templates / "custom"
        for name, spec in template_specs().items():
            for source, folder in (("custom", custom), ("built-in", BUILTIN_DIR)):
                image = self._read_template(folder / name)
                if image is None:
                    continue
                mask = self._read_mask(folder / mask_name(name), image.shape)
                if mask is None:
                    mask = build_mask(np, image.shape, spec)
                try:
                    prepared = prepare_template(cv2, np, image, mask,
                                                spec.get("domain", "gray") if source == "built-in" else "gray")
                except ValueError:
                    continue
                yield name, source, prepared
                break
        for path in sorted(paths.monitor_templates.glob("sarae_*.png")):
            image = self._read_template(path)
            if image is not None:
                try:
                    yield path.name, "cloud", prepare_template(cv2, np, image)
                except ValueError:
                    continue

    def templates(self) -> list[dict]:
        result = []
        custom = paths.monitor_templates / "custom"
        for name in template_specs():
            default = BUILTIN_DIR / name
            override = custom / name
            active = override if self._read_template(override) is not None else default
            image = self._read_template(active)
            result.append({"name": name, "bytes": active.stat().st_size if image is not None else 0,
                           "source": "custom" if active == override else "built-in",
                           "status": "Ready" if image is not None else
                           "Invalid" if active.exists() or override.exists() else "Missing"})
            custom_mask = custom / mask_name(name)
            if custom_mask.exists():
                mask = self._read_mask(custom_mask, image.shape) if image is not None else None
                result.append({"name": custom_mask.name, "bytes": custom_mask.stat().st_size, "source": "custom",
                               "status": "Ready" if mask is not None else "Invalid"})
        for path in sorted(paths.monitor_templates.glob("sarae_*.png")):
            image = self._read_template(path)
            result.append({"name": path.name, "bytes": path.stat().st_size,
                           "source": "cloud", "status": "Ready" if image is not None else "Invalid"})
        return result

    @staticmethod
    def _active_count(templates: list[dict]) -> int:
        return sum(t["status"] == "Ready" and not t["name"].endswith("_mask.png") for t in templates)

    def set_custom_template(self, name: str, encoded: str) -> dict:
        if name not in (*template_specs(), *map(mask_name, template_specs())):
            raise AgentError("INVALID_TEMPLATE", "Tên ảnh mẫu không hợp lệ")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise AgentError("INVALID_TEMPLATE", "Dữ liệu ảnh mẫu không hợp lệ") from exc
        if name.endswith("_mask.png"):
            if not raw or len(raw) > MAX_TEMPLATE_BYTES:
                raise AgentError("INVALID_TEMPLATE", "Mask tối đa 3 MB")
            cv2, np, _ = load_cv()
            mask = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            base_name = name[:-len("_mask.png")] + ".png"
            reference = self._read_template(paths.monitor_templates / "custom" / base_name)
            if reference is None:
                reference = self._read_template(BUILTIN_DIR / base_name)
            if mask is None or reference is None or mask.shape != reference.shape or cv2.countNonZero(mask) < 25:
                raise AgentError("INVALID_TEMPLATE", "Mask phải cùng kích thước với ảnh mẫu và có vùng hợp lệ")
            normalized = cv2.imencode(".png", mask)[1].tobytes()
        else:
            normalized = self._normalize(raw)
        folder = paths.monitor_templates / "custom"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        temp = folder / (name + ".tmp")
        temp.write_bytes(normalized)
        temp.replace(target)
        self._template_cache.pop(target, None)
        self._mask_cache.pop(target, None)
        self._status_changed()
        return {"templates": self.templates()}

    def reset_custom_template(self, name: str) -> dict:
        if name not in (*template_specs(), *map(mask_name, template_specs())):
            raise AgentError("INVALID_TEMPLATE", "Tên ảnh mẫu không hợp lệ")
        (paths.monitor_templates / "custom" / name).unlink(missing_ok=True)
        self._template_cache.pop(paths.monitor_templates / "custom" / name, None)
        self._mask_cache.pop(paths.monitor_templates / "custom" / name, None)
        self._status_changed()
        return {"templates": self.templates()}

    @staticmethod
    def _normalize(raw: bytes) -> bytes:
        """Decode and check one reference image; returns it re-encoded as PNG."""
        if not raw or len(raw) > MAX_TEMPLATE_BYTES:
            raise AgentError("INVALID_TEMPLATE", "Ảnh mẫu tối đa 3 MB")
        cv2, np, _ = load_cv()
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or min(image.shape[:2]) < 20 or max(image.shape[:2]) > 1800:
            raise AgentError("INVALID_TEMPLATE", "Ảnh phải hợp lệ, cạnh từ 20 đến 1800 px")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if cv2.countNonZero(cv2.Canny(gray, 70, 160)) < 25:
            raise AgentError("INVALID_TEMPLATE", "Ảnh mẫu thiếu chi tiết")
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise AgentError("INVALID_TEMPLATE", "Không lưu được ảnh mẫu")
        return encoded.tobytes()

    def sync_templates(self, remote: list[dict], fetch: Callable[[str], bytes]) -> dict:
        """Mirror the Cloud set ``[{id, sha256}]``: download the new ones, delete every other image.

        A download whose sha256 differs from the listing, or that does not decode, is skipped and retried on the
        next sync. Returns {added, removed, failed, total}.
        """
        wanted = {f"sarae_{t['id']}.png": t for t in remote
                  if isinstance(t, dict) and TEMPLATE_ID_RE.fullmatch(str(t.get("id", "")))}
        folder = paths.monitor_templates
        existing = {p.name for p in folder.glob("sarae_*.png")}
        added = failed = 0
        for name, item in wanted.items():
            if name in existing:
                continue
            try:
                raw = fetch(item["id"])
                if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
                    raise AgentError("INVALID_TEMPLATE", "Ảnh mẫu tải về không khớp mã kiểm tra")
                temp = folder / (name + ".tmp")
                temp.write_bytes(self._normalize(raw))
                temp.replace(folder / name)
                added += 1
            except Exception as exc:  # noqa: BLE001 - one bad image must not block the others
                failed += 1
                log.warning("Không tải được ảnh mẫu CAPTCHA %s: %s", item["id"][:8], exc,
                            extra={"code": getattr(exc, "code", "TEMPLATE_DOWNLOAD_FAILED")})
        removed = 0
        for name in existing - set(wanted):
            try:
                (folder / name).unlink()
                removed += 1
            except OSError:
                pass
        if added or removed:
            log.info("Đã cập nhật ảnh mẫu CAPTCHA từ Sarae: thêm %s, bỏ %s", added, removed)
            self._status_changed()
        return {"added": added, "removed": removed, "failed": failed,
                "total": self._active_count(self.templates())}

    # --- config --------------------------------------------------------------------------
    def configure(self, values: dict) -> dict:
        allowed = {k: v for k, v in values.items() if k in MonitorConfig.model_fields}
        updated = self._config.update({"monitor": allowed})
        self._status_changed()
        return updated.monitor.model_dump()

    # --- lifecycle -----------------------------------------------------------------------
    def start(self) -> dict:
        with self._guard:
            if self._thread and self._thread.is_alive():
                return self.status()
            load_cv()
            if not self._active_count(self.templates()):
                raise AgentError("TEMPLATE_MISSING", "Không có ảnh mẫu hợp lệ: kiểm tra Built-in Templates hoặc ảnh tùy chỉnh")
            self._stop = threading.Event()
            self._detected = False
            self._detected_kind = None
            self.state.update(running=True, status="starting", error=None)
            self._thread = threading.Thread(target=self._loop, args=(self._stop,), name="captcha-monitor",
                                            daemon=True)
            self._thread.start()
        self._event("monitor_started", "Đã bắt đầu giám sát")
        self._status_changed()
        return self.status()

    def stop(self, *, wait: float = 5.0) -> dict:
        with self._guard:
            thread = self._thread
            self._stop.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=wait)
        with self._guard:
            was_running = self.state["running"]
            self.state.update(running=False, status="stopped" if was_running or thread else self.state["status"])
        if thread:
            self._event("monitor_stopped", "Đã dừng giám sát")
        self._status_changed()
        return self.status()

    def status(self) -> dict:
        with self._guard:
            templates = self.templates()
            return {"state": dict(self.state), "config": self._config.get().monitor.model_dump(),
                    "events": list(reversed(self.events))[:30],
                    "template_status": [t for t in templates if t["source"] != "cloud"],
                    "builtins_ready": bool(template_specs()) and all(
                        self._read_template(BUILTIN_DIR / name) is not None for name in template_specs()),
                    "templates": self._active_count(templates)}

    def heartbeat_view(self) -> dict:
        data = self.status()
        data["events"] = [{k: e[k] for k in ("at", "kind", "detail")} for e in data["events"][:10]]
        return data

    # --- internals -----------------------------------------------------------------------
    def _event(self, kind: str, detail: str, image: str | None = None) -> dict:
        event = {"id": int(time.time() * 1_000_000), "at": _now(), "kind": kind, "detail": detail, "image": image}
        with self._guard:
            self.events.append(event)
            self.state["events_count"] += 1
        return event

    def _status_changed(self) -> None:
        self._emit("monitor.status_changed", self.status())

    def _set(self, **changes) -> None:
        with self._guard:
            before = self.state.get("status")
            self.state.update(changes)
            changed = before != self.state.get("status")
        if changed:
            self._status_changed()

    def _loop(self, stop: threading.Event) -> None:
        positive = negative = 0
        capture = None
        try:
            cv2, np, mss = load_cv()
            capture = (getattr(mss, "MSS", None) or mss.mss)()
            while not stop.is_set():
                interval = self._config.get().monitor.interval_seconds
                threshold = self._config.get().monitor.threshold
                next_delay = interval
                try:
                    target = find_studio_window()
                    self._set(window_found=target is not None, window_title=target["title"] if target else "",
                              window_visible=bool(target and not target["minimized"]), window_info=target,
                              error=None)
                    if not target or target["minimized"]:
                        positive = negative = 0
                        self._set(status="window_unavailable", process_running=studio_process_running())
                        stop.wait(interval)
                        continue
                    rect = win32.window_rect(target["hwnd"])
                    if rect is None:
                        stop.wait(interval)
                        continue
                    left, top, right, bottom = rect
                    virtual = capture.monitors[0]
                    vx, vy = int(virtual["left"]), int(virtual["top"])
                    left, top = max(left, vx), max(top, vy)
                    right = min(right, vx + int(virtual["width"]))
                    bottom = min(bottom, vy + int(virtual["height"]))
                    if right - left < 120 or bottom - top < 100:
                        raise ValueError("Cửa sổ TikTok nằm ngoài vùng màn hình đang hiển thị")
                    bounds = {"left": left, "top": top, "width": right - left, "height": bottom - top}
                    shot = np.array(capture.grab(bounds))
                    self._set(capture_bounds=bounds, monitors_detected=len(capture.monitors) - 1,
                              process_running=True, last_scan=_now())
                    gray = cv2.cvtColor(shot, cv2.COLOR_BGRA2GRAY)
                    templates = [(name, prepared) for name, _, prepared in self._candidates()]
                    if not templates:
                        self._set(status="template_missing_or_invalid",
                                  error="Không có ảnh mẫu hợp lệ phù hợp kích thước cửa sổ Studio")
                        stop.wait(interval)
                        continue
                    finding = analyze_frame(cv2, np, gray, templates, threshold)
                    kind = finding["kind"]
                    score, scale, matched = finding["score"], finding["scale"], finding["template"]
                    hit = kind != "NONE"
                    positive = positive + 1 if hit and not self._detected else 0
                    negative = negative + 1 if not hit else 0
                    if hit and not self._detected and positive < CONFIRM_FRAMES:
                        next_delay = 2.5
                    self._set(match_score=round(score, 3), match_scale=scale, matched_template=matched,
                              event_type=self._detected_kind, challenge_type=finding["challenge_type"],
                              status=self._detected_kind.lower() if self._detected_kind else "watching")
                    if hit and self._detected and self._detected_kind != kind:
                        self._detected_kind = kind  # same continuous appearance; no second alert
                        self._set(status=kind.lower(), event_type=kind)
                    if hit and positive >= CONFIRM_FRAMES and not self._detected:
                        self._detected = True
                        self._detected_kind = kind
                        image_name = "detected_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".png"
                        cv2.imwrite(str(paths.monitor_captures / image_name), shot)
                        self._prune_captures()
                        event = self._event("verification_detected", describe(finding), image_name)
                        self._set(status=kind.lower(), event_type=kind)
                        log.warning("Phát hiện %s trên TikTok LIVE Studio: %s", kind, describe(finding),
                                    extra={"code": "VERIFICATION_DETECTED"})
                        self._emit("monitor.verification_detected", {
                            "at": event["at"], "kind": kind, "challenge_type": finding["challenge_type"],
                            "score": round(score, 3), "template": matched,
                            "image_path": str(paths.monitor_captures / image_name),
                            "window_title": target["title"]})
                    elif self._detected and negative >= CONFIRM_FRAMES:
                        self._detected = False
                        old_kind = self._detected_kind
                        self._detected_kind = None
                        event = self._event("verification_cleared",
                                            "Không còn thấy giao diện xác minh; chưa xác minh đã giải thành công.")
                        self._set(status="watching", event_type=None, challenge_type=None)
                        self._emit("monitor.verification_cleared", {"at": event["at"], "kind": old_kind})
                except AgentError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep watching after transient capture errors
                    self._set(status="error", error=str(exc)[:250])
                stop.wait(next_delay)
        except AgentError as exc:
            self._set(status="error", error=exc.message, running=False)
            self._event("monitor_error", exc.message)
        except Exception as exc:  # noqa: BLE001
            log.exception("CAPTCHA Monitor dừng do lỗi")
            self._set(status="error", error=str(exc)[:250], running=False)
            self._event("monitor_error", str(exc)[:250])
        finally:
            if capture is not None:
                try:
                    capture.close()
                except Exception:  # noqa: BLE001
                    pass
            with self._guard:
                self.state["running"] = False
            self._status_changed()

    @staticmethod
    def _prune_captures(keep: int = 50) -> None:
        files = sorted(paths.monitor_captures.glob("detected_*.png"))
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

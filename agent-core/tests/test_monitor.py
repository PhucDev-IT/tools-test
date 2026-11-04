import hashlib
import base64
import os
from pathlib import Path
import cv2
import numpy as np
import pytest

from conftest import windows_only
from sarae_agent.config import ConfigStore, paths
from sarae_agent.errors import AgentError
from sarae_agent.monitoring.captcha import CaptchaMonitor, match_reference, classify_scores
import sarae_agent.monitoring.captcha as captcha_module
REAL_BUILTIN_DIR = captcha_module.BUILTIN_DIR
from sarae_agent.monitoring.studio import is_studio_executable


def synthetic_dialog() -> np.ndarray:
    """A fake CAPTCHA dialog: framed box with a title bar and text-like strokes."""
    img = np.full((140, 260), 235, np.uint8)
    cv2.rectangle(img, (4, 4), (255, 135), 40, 3)
    cv2.rectangle(img, (4, 4), (255, 34), 90, -1)
    cv2.putText(img, "Verify you are human", (14, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, 255, 1)
    cv2.putText(img, "Drag the slider", (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 20, 2)
    cv2.rectangle(img, (30, 100), (230, 122), 60, 2)
    return img


def screen_with(dialog: np.ndarray | None, scale: float = 1.0) -> np.ndarray:
    rng = np.random.default_rng(7)
    screen = rng.integers(0, 40, (720, 1280), dtype=np.uint8)  # dark, noisy LIVE preview
    cv2.rectangle(screen, (100, 600), (500, 680), 120, -1)
    if dialog is not None:
        d = cv2.resize(dialog, None, fx=scale, fy=scale)
        screen[300:300 + d.shape[0], 500:500 + d.shape[1]] = d
    return screen


def test_matcher_detects_dialog_and_tolerates_scale():
    template = synthetic_dialog()[4:60, 4:255]  # title + first line, as an operator would crop
    present, _ = match_reference(cv2, np, screen_with(synthetic_dialog()), template)
    scaled, scale = match_reference(cv2, np, screen_with(synthetic_dialog(), 1.1), template)
    absent, _ = match_reference(cv2, np, screen_with(None), template)
    assert present > 0.82  # same display scaling as the template: above the default threshold
    assert absent < 0.6  # noisy synthetic preview; real Studio negatives stay below 0.71 (offline test)
    # 10 % scale drift still separates from "no dialog", but falls below the default 0.82:
    # templates must be captured at the Studio's actual Windows display scaling (documented).
    assert scaled > absent + 0.2


def test_template_without_detail_rejected():
    with pytest.raises(ValueError):
        match_reference(cv2, np, screen_with(None), np.full((40, 40), 128, np.uint8))


def test_classifier_keeps_warning_challenge_and_ambiguity_separate():
    specs = captcha_module.template_specs()
    assert classify_scores({"inactivity_modal_top.png": (0.94, 1.0)}, 0.82)["kind"] == "INACTIVITY_WARNING"
    both = classify_scores({"inactivity_modal_top.png": (0.94, 1.0), "puzzle_challenge_popup.png": (0.9, 1.0)}, 0.82)
    assert (both["kind"], both["challenge_type"]) == ("INACTIVITY_WARNING", "PUZZLE")
    challenge = classify_scores({"shapes_challenge_popup.png": (0.9, 1.0), "inactivity_modal_top.png": (0.5, 1.0)}, 0.82)
    assert (challenge["kind"], challenge["challenge_type"]) == ("CAPTCHA_CHALLENGE", "SHAPES")
    tie = classify_scores({"shapes_challenge_popup.png": (0.88, 1.0), "puzzle_challenge_popup.png": (0.86, 1.0)}, 0.82)
    assert (tie["kind"], tie["challenge_type"], tie["ambiguous"]) == ("UNKNOWN_VERIFICATION", None, True)
    grey = classify_scores({"inactivity_modal_top.png": (0.74, 1.0)}, 0.82)
    assert grey["kind"] == "UNKNOWN_VERIFICATION"
    # A component (frame + button) below the threshold is too generic to raise UNKNOWN.
    assert specs["shapes_challenge_component.png"]["unknown_floor"] is None
    assert classify_scores({"shapes_challenge_component.png": (0.78, 1.0)}, 0.82)["kind"] == "NONE"
    assert classify_scores({"sarae_" + "a" * 32 + ".png": (0.9, 1.0)}, 0.82)["kind"] == "UNKNOWN_VERIFICATION"
    assert classify_scores({}, 0.82)["kind"] == "NONE"
    assert "kèm CAPTCHA kéo mảnh ghép" in captcha_module.describe(both)
    assert "Hộp thoại CAPTCHA" not in captcha_module.describe(both)


def test_builtin_templates_have_complete_metadata():
    specs = captcha_module.template_specs()
    assert len(specs) == 8
    roles = {(s["role"], s["challenge_type"]) for s in specs.values()}
    assert roles == {("warning", None), ("challenge", "SHAPES"), ("challenge", "PUZZLE")}
    for name, spec in specs.items():
        image = cv2.imread(str(captcha_module.BUILTIN_DIR / name), cv2.IMREAD_GRAYSCALE)
        left, top, right, bottom = spec["crop"]
        assert image is not None and image.shape == (bottom - top, right - left), name
        assert spec["source"] and len(spec["source_sha256"]) == 64 and spec["purpose"], name
        mask = captcha_module.build_mask(np, image.shape, spec)
        assert mask is not None and 0 < cv2.countNonZero(mask) < mask.size, name
        frame = image if spec["domain"] == "gray" else None
        if frame is not None:  # a template finds itself
            assert match_reference(cv2, np, cv2.copyMakeBorder(frame, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=37),
                                   image, mask)[0] > 0.85, name


FIXTURES = os.environ.get("SARAE_CAPTCHA_FIXTURES")
STUDIO_WITHOUT_POPUP = {  # real Studio UI next to the popup; the popup itself is outside these crops
    "image1.png": [(0, 0, 625, 855), (975, 0, 1600, 855)],
    "shapes_captcha_windows.png": [(0, 0, 752, 1080), (1170, 0, 1920, 1080)],
    "shapes_captcha_windows_2.png": [(0, 0, 778, 1080), (1142, 0, 1920, 1080)],
    "puzzle_slide.png": [(0, 0, 486, 958)],
}
# Challenge part of popups whose screenshot was not a template source: CAPTCHA without the inactivity text.
CHALLENGE_ONLY = {"image2.png": (585, 410, 995, 770), "image3.png": (515, 435, 875, 755),
                  "shapes_captcha_windows_2.png": (770, 450, 1150, 810)}
EXPECTED = {  # file: (kind, challenge type, input is an edge map)
    "image1.png": ("INACTIVITY_WARNING", "SHAPES", False),
    "image2.png": ("INACTIVITY_WARNING", "SHAPES", False),
    "image3.png": ("INACTIVITY_WARNING", "SHAPES", False),
    "shapes_captcha_windows.png": ("INACTIVITY_WARNING", "SHAPES", False),
    "shapes_captcha_windows_2.png": ("INACTIVITY_WARNING", "SHAPES", False),
    "puzzle_slide.png": ("INACTIVITY_WARNING", "PUZZLE", False),
    "extracted.png": ("CAPTCHA_CHALLENGE", "SHAPES", False),
    "test_extracted_captcha_box.png": ("CAPTCHA_CHALLENGE", "PUZZLE", True),
    "test_extracted_slide_button.png": ("CAPTCHA_CHALLENGE", "PUZZLE", True),
    "test_shapes_template.png": ("CAPTCHA_CHALLENGE", "SHAPES", True),
    "resources/shapes_template.png": ("CAPTCHA_CHALLENGE", "SHAPES", False),
    "resources/puzzle_template.png": ("CAPTCHA_CHALLENGE", "PUZZLE", False),
    "resources/puzzle_template_large.png": ("CAPTCHA_CHALLENGE", "PUZZLE", False),
    # Fragments without any surrounding UI (picture, blank canvas, lone button): not a verification screen.
    "test_extract_canvas.png": ("NONE", None, False),
    "test_draw_rectangle.png": ("NONE", None, False),
    "test_extract_piece.png": ("NONE", None, False),
    "resources/puzzle_canvas_template.png": ("NONE", None, False),
    "resources/puzzle_canvas_template_large.png": ("NONE", None, False),
    "resources/shapes_captcha_sample.png": ("NONE", None, False),
    "resources/confirm.png": ("NONE", None, False),
    "resources/slide_button_template.png": ("NONE", None, False),
    "unrelated.png": ("NONE", None, False),
}


def offline_cases():
    folder = Path(FIXTURES)
    for name, (kind, subtype, edges) in EXPECTED.items():
        yield name, cv2.imread(str(folder / name), cv2.IMREAD_GRAYSCALE), edges, (kind, subtype)
    for name, boxes in STUDIO_WITHOUT_POPUP.items():
        for left, top, right, bottom in boxes:
            image = cv2.imread(str(folder / name), cv2.IMREAD_GRAYSCALE)
            yield f"{name}[{left}:{right}] no popup", image[top:bottom, left:right], False, ("NONE", None)
    for name, (left, top, right, bottom) in CHALLENGE_ONLY.items():
        image = cv2.imread(str(folder / name), cv2.IMREAD_GRAYSCALE)
        yield f"{name} challenge only", image[top:bottom, left:right], False, ("CAPTCHA_CHALLENGE", "SHAPES")
    # Uncertain: another window covers 40 % of the warning popup. Must not be guessed as a known type.
    image = cv2.imread(str(folder / "image2.png"), cv2.IMREAD_GRAYSCALE)[178:420, 583:994]
    covered = image.copy()
    covered[:, int(image.shape[1] * 0.6):] = 60
    yield "image2.png warning 40% covered", covered, False, ("UNKNOWN_VERIFICATION", None)


@pytest.mark.skipif(not FIXTURES, reason="Owner screenshots are not committed (set SARAE_CAPTCHA_FIXTURES)")
def test_every_supplied_image_is_classified_offline():
    templates = captcha_module.builtin_templates(cv2, np)
    assert len(templates) == 8
    failures = []
    for label, image, edges, wanted in offline_cases():
        assert image is not None, label
        result = captcha_module.analyze_frame(cv2, np, image, templates, 0.82, edge_input=edges)
        got = (result["kind"], result["challenge_type"])
        top = sorted(result["scores"].items(), key=lambda item: -item[1][0])[:3]
        print(f"{'PASS' if got == wanted else 'FAIL'} {label}: {got} expected {wanted} | "
              + ", ".join(f"{n}={s:.3f}" for n, (s, _) in top))
        if got != wanted:
            failures.append((label, got, wanted))
    assert not failures, failures


@windows_only
def test_builtin_custom_fallback_and_reset(home, events, tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(captcha_module, "BUILTIN_DIR", bundled)
    (bundled / "templates.json").write_bytes((REAL_BUILTIN_DIR / "templates.json").read_bytes())
    name = "puzzle_challenge_popup.png"
    good = cv2.imencode(".png", synthetic_dialog())[1].tobytes()
    (bundled / name).write_bytes(good)
    monitor = CaptchaMonitor(ConfigStore(), events)
    assert next(t for t in monitor.templates() if t["name"] == name)["source"] == "built-in"
    assert monitor.status()["templates"] == 1
    alternative = cv2.rotate(synthetic_dialog(), cv2.ROTATE_180)
    encoded = base64.b64encode(cv2.imencode(".png", alternative)[1]).decode()
    monitor.set_custom_template(name, encoded)
    assert next(t for t in monitor.templates() if t["name"] == name)["source"] == "custom"
    assert next(monitor._candidates())[1] == "custom"
    mask_name = "puzzle_challenge_popup_mask.png"
    mask = np.full(alternative.shape, 255, np.uint8)
    mask_encoded = base64.b64encode(cv2.imencode(".png", mask)[1]).decode()
    monitor.set_custom_template(mask_name, mask_encoded)
    assert next(monitor._candidates())[2]["mask"] is not None
    with pytest.raises(AgentError):
        monitor.set_custom_template(mask_name, base64.b64encode(cv2.imencode(".png", mask[:30])[1]).decode())
    custom = paths.monitor_templates / "custom" / name
    custom.write_bytes(b"corrupt")
    assert next(monitor._candidates())[1] == "built-in"
    monitor.reset_custom_template(name)
    assert not custom.exists()
    assert next(monitor._candidates())[1] == "built-in"
    assert (bundled / name).read_bytes() == good


@windows_only
def test_invalid_and_missing_templates_never_start(home, events, tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(captcha_module, "BUILTIN_DIR", bundled)
    (bundled / "templates.json").write_bytes((REAL_BUILTIN_DIR / "templates.json").read_bytes())
    (bundled / "shapes_challenge_popup.png").write_bytes(b"broken")
    monitor = CaptchaMonitor(ConfigStore(), events)
    statuses = {t["name"]: t["status"] for t in monitor.templates()}
    assert statuses["shapes_challenge_popup.png"] == "Invalid"
    assert statuses["inactivity_modal_top.png"] == "Missing"
    assert monitor.status()["builtins_ready"] is False
    assert monitor.status()["templates"] == 0
    with pytest.raises(AgentError, match="Không có ảnh mẫu"):
        monitor.start()


@windows_only
def test_scan_immediate_fast_confirmation_then_normal_interval(home, events, monkeypatch):
    from types import SimpleNamespace
    capture = SimpleNamespace(monitors=[{"left": 0, "top": 0, "width": 800, "height": 600}, {}],
                              grab=lambda _: np.full((300, 400, 4), 180, np.uint8), close=lambda: None)
    monkeypatch.setattr(captcha_module, "load_cv", lambda: (cv2, np, SimpleNamespace(MSS=lambda: capture)))
    monkeypatch.setattr(captcha_module, "find_studio_window", lambda: {
        "hwnd": 7, "pid": 1, "title": "TikTok LIVE Studio", "minimized": False})
    monkeypatch.setattr(captcha_module.win32, "window_rect", lambda _: (0, 0, 400, 300))
    monkeypatch.setattr(captcha_module, "analyze_frame", lambda *_a, **_k: {
        "kind": "INACTIVITY_WARNING", "challenge_type": "SHAPES", "ambiguous": False, "score": 0.92,
        "scale": 1.0, "template": "inactivity_modal_top.png", "scores": {}})
    monkeypatch.setattr(cv2, "imwrite", lambda *_: True)
    monitor = CaptchaMonitor(ConfigStore(), events)
    monkeypatch.setattr(monitor, "_candidates", lambda: iter([("sample.png", "built-in", {})]))
    waits = []
    class Stop:
        def is_set(self): return len(waits) >= 3
        def wait(self, seconds): waits.append(seconds)
    monitor._loop(Stop())
    assert waits == [2.5, 240.0, 240.0]
    detected = [e[1] for e in events.captured if e[0] == "monitor.verification_detected"]
    assert len(detected) == 1 and detected[0]["kind"] == "INACTIVITY_WARNING"
    assert monitor.status()["state"]["status"] == "inactivity_warning"
    detail = next(e["detail"] for e in monitor.events if e["kind"] == "verification_detected")
    assert detail.startswith("Cảnh báo LIVE không hoạt động") and "kèm CAPTCHA chọn 2 vật" in detail


@pytest.mark.parametrize("name,expected", [
    ("TikTok LIVE Studio.exe", True), ("TikTokLiveStudio.exe", True), ("tiktok_live_studio.EXE", True),
    ("TikTok.exe", False), ("obs64.exe", False), ("TikTok LIVE Studio", False),
])
def test_studio_executable_identity(name, expected):
    assert is_studio_executable(name) is expected


def test_custom_executable_names():
    assert is_studio_executable("LiveStudioBeta.exe", "livestudiobeta")


@windows_only
def test_templates_and_start_guard(home, events, tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(captcha_module, "BUILTIN_DIR", bundled)
    monitor = CaptchaMonitor(ConfigStore(), events)
    with pytest.raises(AgentError) as exc:
        monitor.start()
    assert exc.value.code == "TEMPLATE_MISSING"
    ok, good = cv2.imencode(".png", synthetic_dialog())
    good = good.tobytes()
    ok, flat = cv2.imencode(".png", np.full((60, 60, 3), 200, np.uint8))
    flat = flat.tobytes()
    files = {"a" * 32: good, "b" * 32: flat}
    remote = [{"id": i, "sha256": hashlib.sha256(raw).hexdigest()} for i, raw in files.items()]
    (paths.monitor_templates / "custom_old.png").write_bytes(good)  # unrelated files are preserved
    result = monitor.sync_templates(remote + [{"id": "../../config", "sha256": "x"}], files.__getitem__)
    assert result == {"added": 1, "removed": 0, "failed": 1, "total": 1}  # the flat image has no detail
    assert [t["name"] for t in monitor.templates() if t["status"] == "Ready"] == ["sarae_" + "a" * 32 + ".png"]
    tampered = monitor.sync_templates([{"id": "c" * 32, "sha256": "0" * 64}] + remote[:1], lambda _id: good)
    assert tampered["failed"] == 1 and tampered["total"] == 1  # sha256 mismatch is never installed
    assert monitor.sync_templates([], files.__getitem__)["total"] == 0  # Sarae removed every template
    monitor.sync_templates(remote[:1], files.__getitem__)
    config = monitor.configure({"interval_seconds": 180, "threshold": 0.9, "unknown": 1})
    assert config["interval_seconds"] == 180 and config["threshold"] == 0.9
    # Starting with a template runs the loop; with no TikTok window it reports that state.
    monitor.start()
    try:
        import time
        deadline = time.monotonic() + 10
        while monitor.status()["state"]["status"] in ("starting",) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert monitor.status()["state"]["status"] in ("window_unavailable", "watching", "captcha_detected")
    finally:
        stopped = monitor.stop()
    assert stopped["state"]["running"] is False
    assert any(e["kind"] == "monitor_stopped" for e in stopped["events"])


@windows_only
def test_approved_builtin_assets_load_on_first_run(home, events):
    monitor = CaptchaMonitor(ConfigStore(), events)
    templates = {t["name"]: t for t in monitor.templates()}
    for name in captcha_module.template_specs():
        assert templates[name]["status"] == "Ready"
        assert templates[name]["source"] == "built-in"
        assert templates[name]["bytes"] > 0
    assert len(list(monitor._candidates())) == 8
    assert monitor.status()["builtins_ready"] is True
    assert monitor.status()["templates"] == 8
    monitor.start()
    monitor.stop()

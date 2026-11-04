"""Recreate the bundled verification templates and their metadata (resources/templates.json).

Each template is a complete UI region cropped from one owner-supplied screenshot: popup header and
paragraph for the inactivity warning, prompt + canvas frame + control + footer for each challenge
layout. Dynamic content (countdown, puzzle picture, shapes, audio id) is listed in ``ignore`` and is
masked out at match time. Source screenshots are never modified and are not committed (they contain
LIVE and account data); the source SHA-256 is checked before cropping. Run manually, never at startup:

    python scripts/build_captcha_templates.py C:/Users/Phuc/Downloads/images
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2

SOURCE_HASHES = {
    "image1.png": "5dbb9f343d1c",
    "puzzle_slide.png": "829dce5805bd",
    "shapes_captcha_windows.png": "7b426bfa3607",
    "resources/puzzle_template_large.png": "6190b080ad3c",
    "test_extracted_captcha_box.png": "f1ba822102aa",
}

# crop and ignore boxes are (left, top, right, bottom) in source pixels; ignore boxes are converted to
# template-relative coordinates in templates.json. band = (left, top, right, bottom, thickness).
TEMPLATES = [
    {"file": "inactivity_modal_top.png", "role": "warning", "challenge_type": None,
     "variant": "live_will_end_popup", "source": "image1.png", "crop": (636, 170, 966, 348),
     "ignore": [(745, 210, 860, 252)], "min_score": 0.75, "unknown_floor": 0.70,
     "purpose": "INACTIVITY_WARNING: top of the 'LIVE will end in' popup - header, countdown slot "
                "(ignored) and the full inactivity paragraph on the popup background."},
    {"file": "shapes_challenge_popup.png", "role": "challenge", "challenge_type": "SHAPES",
     "variant": "single_line_prompt", "source": "image1.png", "crop": (636, 375, 966, 660),
     "ignore": [(673, 414, 928, 569), (720, 633, 890, 650)], "min_score": 0.75, "unknown_floor": 0.70,
     "purpose": "CAPTCHA_CHALLENGE/SHAPES: prompt 'Select 2 objects...', canvas frame, Confirm button and "
                "Audio footer. Shapes inside the canvas and the audio id are ignored."},
    {"file": "shapes_challenge_popup_wrapped.png", "role": "challenge", "challenge_type": "SHAPES",
     "variant": "wrapped_prompt_loading", "source": "shapes_captcha_windows.png",
     "crop": (762, 452, 1150, 812), "ignore": [(803, 521, 1112, 709), (860, 786, 1068, 804), (793, 464, 824, 496)],
     "min_score": 0.75, "unknown_floor": 0.70,
     "purpose": "CAPTCHA_CHALLENGE/SHAPES: layout where the prompt wraps to two lines next to a loading "
                "spinner (taller canvas). Canvas content, spinner and audio id are ignored."},
    {"file": "puzzle_challenge_popup.png", "role": "challenge", "challenge_type": "PUZZLE",
     "variant": "drag_piece_prompt", "source": "puzzle_slide.png", "crop": (495, 358, 840, 645),
     "ignore": [(533, 398, 801, 561), (582, 623, 762, 640)], "min_score": 0.75, "unknown_floor": 0.70,
     "purpose": "CAPTCHA_CHALLENGE/PUZZLE: prompt 'Drag the puzzle piece into place', picture frame, "
                "slider with arrow knob and Audio footer. Picture and audio id are ignored."},
    {"file": "shapes_challenge_component.png", "role": "challenge", "challenge_type": "SHAPES",
     "variant": "canvas_and_confirm", "source": "image1.png", "crop": (664, 406, 937, 623),
     "ignore": [(673, 414, 928, 569)], "min_score": 0.80, "unknown_floor": None,
     "purpose": "CAPTCHA_CHALLENGE/SHAPES without surrounding popup text: canvas frame directly above the "
                "Confirm button. No grey zone: a lone rectangle with a button is too generic for UNKNOWN."},
    {"file": "puzzle_challenge_component.png", "role": "challenge", "challenge_type": "PUZZLE",
     "variant": "canvas_and_slider", "source": "puzzle_slide.png", "crop": (525, 390, 809, 615),
     "ignore": [(533, 398, 801, 561)], "min_score": 0.80, "unknown_floor": None,
     "purpose": "CAPTCHA_CHALLENGE/PUZZLE without surrounding popup text: picture frame directly above "
                "the slider bar and knob."},
    {"file": "puzzle_challenge_large.png", "role": "challenge", "challenge_type": "PUZZLE",
     "variant": "large_canvas_and_slider", "source": "resources/puzzle_template_large.png",
     "crop": (0, 0, 362, 290), "ignore": [(13, 13, 347, 215)], "min_score": 0.80, "unknown_floor": None,
     "purpose": "CAPTCHA_CHALLENGE/PUZZLE, large layout supplied by the reference solver (not covered by "
                "the screenshots): picture frame and slider. Picture is ignored."},
    {"file": "puzzle_verify_box_edges.png", "role": "challenge", "challenge_type": "PUZZLE",
     "variant": "verify_to_continue_box", "source": "test_extracted_captcha_box.png", "domain": "edges",
     "crop": (0, 0, 391, 398), "ignore": [(26, 76, 362, 286), (70, 350, 280, 368)],
     "bands": [(20, 68, 368, 338, 7)], "min_score": 0.75, "unknown_floor": 0.70,
     "purpose": "CAPTCHA_CHALLENGE/PUZZLE 'Verify to continue:' box without the inactivity popup. The only "
                "supplied sample is an edge map, so it is matched in the same edge domain. Picture, id and "
                "the debug rectangle drawn by the reference solver are ignored."},
]


def build(source_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = []
    for spec in TEMPLATES:
        source = source_dir / spec["source"]
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if not digest.startswith(SOURCE_HASHES[spec["source"]]):
            raise ValueError(f"Nguồn {spec['source']} không khớp SHA-256 đã duyệt")
        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        left, top, right, bottom = spec["crop"]
        if image is None or right > image.shape[1] or bottom > image.shape[0]:
            raise ValueError(f"Không đọc được vùng crop của {spec['source']}")
        crop = image[top:bottom, left:right]
        if not cv2.imwrite(str(output_dir / spec["file"]), crop):
            raise OSError(f"Không lưu được {spec['file']}")
        meta.append({
            "file": spec["file"], "role": spec["role"], "challenge_type": spec["challenge_type"],
            "variant": spec["variant"], "domain": spec.get("domain", "gray"),
            "source": spec["source"], "source_sha256": digest, "crop": list(spec["crop"]),
            "ignore": [[a - left, b - top, c - left, d - top] for a, b, c, d in spec["ignore"]],
            "bands": [[a - left, b - top, c - left, d - top, w] for a, b, c, d, w in spec.get("bands", [])],
            "min_score": spec["min_score"], "unknown_floor": spec["unknown_floor"], "purpose": spec["purpose"],
        })
    (output_dir / "templates.json").write_text(json.dumps({"templates": meta}, indent=2, ensure_ascii=False) + "\n",
                                               encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path, help="Thư mục screenshot đã được phép sử dụng")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "src/sarae_agent/monitoring/resources")
    args = parser.parse_args()
    build(args.source_dir, args.output_dir)

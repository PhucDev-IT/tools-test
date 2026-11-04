# Verification UI templates

`templates.json` is the machine-readable metadata for every template here (source file and SHA-256, crop box,
ignored regions, role, challenge type, variant, thresholds, purpose). It is written by
`agent-core/scripts/build_captcha_templates.py`, which checks each source hash before cropping. The original
screenshots stay unchanged in the owner's folder and are not committed (they contain LIVE and account data).

Each template is a complete UI region, never a single sentence. Dynamic content (countdown digits, shapes,
puzzle picture, audio id, spinner) is listed under `ignore` and masked out when matching. Matching runs on Sobel
gradient maps at half resolution over UI scales 0.6–1.6.

| Template | Event type | Source (crop `l,t,r,b`) | Why this region |
| --- | --- | --- | --- |
| `inactivity_modal_top.png` | `INACTIVITY_WARNING` | `image1.png` (636,170,966,348) | Top of the "LIVE will end in" popup: header, countdown slot (ignored) and the whole inactivity paragraph on the popup background. Present in all six full screenshots. |
| `shapes_challenge_popup.png` | `CAPTCHA_CHALLENGE` / SHAPES | `image1.png` (636,375,966,660) | Prompt "Select 2 objects…", canvas frame, Confirm button and Audio footer. |
| `shapes_challenge_popup_wrapped.png` | `CAPTCHA_CHALLENGE` / SHAPES | `shapes_captcha_windows.png` (762,452,1150,812) | Different layout: the prompt wraps to two lines next to a loading spinner and the canvas is taller; the single-line template scores only 0.60 on it. |
| `puzzle_challenge_popup.png` | `CAPTCHA_CHALLENGE` / PUZZLE | `puzzle_slide.png` (495,358,840,645) | Prompt "Drag the puzzle piece into place", picture frame, slider with arrow knob and Audio footer. |
| `shapes_challenge_component.png` | `CAPTCHA_CHALLENGE` / SHAPES | `image1.png` (664,406,937,623) | Canvas frame directly above Confirm, for a challenge shown without the popup text (e.g. `extracted.png`). No grey zone. |
| `puzzle_challenge_component.png` | `CAPTCHA_CHALLENGE` / PUZZLE | `puzzle_slide.png` (525,390,809,615) | Picture frame directly above the slider, same purpose for the puzzle. No grey zone. |
| `puzzle_challenge_large.png` | `CAPTCHA_CHALLENGE` / PUZZLE | `resources/puzzle_template_large.png` (whole image) | Large puzzle layout supplied by the reference solver; not present in any screenshot and scores only 0.63 with the standard component. |
| `puzzle_verify_box_edges.png` | `CAPTCHA_CHALLENGE` / PUZZLE | `test_extracted_captcha_box.png` (whole image) | "Verify to continue:" box: a challenge without the inactivity popup. The only sample is an edge map, so it is matched in the same edge domain; the debug rectangle drawn by the reference solver is ignored. Not yet verified on a real color screenshot. |

Classification (`classify_scores`): the warning template decides `INACTIVITY_WARNING` (the challenge type found in
the same frame is reported with it); otherwise a challenge template decides `CAPTCHA_CHALLENGE`; a full-context
template between `unknown_floor` (0.70) and the threshold (default 0.82), a shapes/puzzle tie closer than 0.08, or a
Cloud image without metadata gives `UNKNOWN_VERIFICATION`. Component templates never raise UNKNOWN on their own.

Images reviewed and not used as templates: `image2.png`, `image3.png`, `shapes_captcha_windows_2.png` (same layouts
as the sources above; kept as independent test inputs), `extracted.png` and the reference `shapes_template.png` /
`puzzle_template.png` (covered by the component templates; test inputs), `test_extract_canvas.png`,
`test_draw_rectangle.png`, `test_extract_piece.png`, `puzzle_canvas_template*.png`, `shapes_captcha_sample.png`,
`confirm.png`, `slide_button_template*.png` (fragments without surrounding UI: a picture, a blank canvas or a lone
button is not evidence of a verification screen), `unrelated.png` (negative), and the `*.xcf` editor files.

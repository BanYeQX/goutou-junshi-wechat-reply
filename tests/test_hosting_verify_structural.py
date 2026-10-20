"""Offline tests for the structural delivery check (`verify_delivery`).

Synthetic frames only. The input box has the calibrated size (2087x74) so the
old "pixels changed > 1%" rule can be shown failing on the same frames. These
prove the decision rule; they do NOT prove the tolerances fit real WeChat
glyphs - that still needs a live run.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from hosting_run import draft_width, record_verify_trace, verify_delivery
from input_readback_fixtures import FONT_PATH, fixture as input_fixture
from wechat_capture import PixelImage

SIZE = (2100, 300)
MESSAGES = (6, 0, 2093, 210)
INPUT = (6, 220, 2093, 294)
DARK = {"background": (30, 30, 30), "ink": (220, 220, 220), "bubble": (59, 201, 115),
        "incoming": (44, 44, 44)}
LIGHT = {"background": (255, 255, 255), "ink": (20, 20, 20), "bubble": (149, 236, 105),
         "incoming": (237, 237, 237)}


def glyph(draw, x, theme, kind="box"):
    """A 22px-wide character. "chuan" is three thin strokes 6px apart, like 川."""
    ink = theme["ink"]
    if kind == "chuan":
        for offset in (2, 10, 18):
            draw.rectangle((x + offset, 240, x + offset + 1, 266), fill=ink)
    else:
        draw.rectangle((x, 240, x + 21, 266), outline=ink, width=2)
    return x + 24


def frame(theme=DARK, *, chars=0, kind="box", caret=None, bubble=False, incoming=False,
          overlay=False):
    image = Image.new("RGB", SIZE, theme["background"])
    draw = ImageDraw.Draw(image)
    x = 20
    for _ in range(chars):
        x = glyph(draw, x, theme, kind)
    if caret == "end":
        draw.rectangle((x + 2, 238, x + 3, 268), fill=theme["ink"])
    elif caret == "start":
        draw.rectangle((20, 238, 21, 268), fill=theme["ink"])
    if overlay:
        # An IME candidate strip drawn over the box while the draft is still there.
        draw.rectangle((300, 228, 900, 288), fill=(80, 80, 120))
    if bubble:
        draw.rectangle((1800, 150, 2080, 200), fill=theme["bubble"])
    if incoming:
        draw.rectangle((20, 150, 300, 200), fill=theme["incoming"])
    return image


def pixel(image):
    """The type live capture really returns: wechat_capture.PixelImage (BGRA bytes).

    The first draft of these tests fed PIL images straight in; production has no
    PIL API on its frames, and the first live commit crashed after Enter.
    """
    rgba = image.convert("RGBA")
    return PixelImage(rgba.width, rgba.height, rgba.tobytes("raw", "BGRA"))


def run(baseline, frames, timeout=0.5):
    baseline = pixel(baseline)
    sequence = [pixel(f) for f in frames]

    def capture():
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    trace = {}
    delivered = verify_delivery("收到", baseline=baseline, capture=capture, box=MESSAGES,
                                input_box=INPUT, threshold=0.004, timeout=timeout,
                                trace=trace)
    return delivered, trace


class DraftWidthTests(unittest.TestCase):
    def crop(self, **options):
        return frame(**options).crop(INPUT)

    def test_empty_box_has_no_draft(self):
        self.assertEqual(draft_width(self.crop()), 0)

    def test_caret_alone_is_not_text(self):
        self.assertEqual(draft_width(self.crop(caret="start")), 0)

    def test_thin_stroked_glyph_counts_as_one_character(self):
        self.assertGreaterEqual(draft_width(self.crop(chars=1, kind="chuan")), 18)

    def test_live_frame_type_measures_the_same_as_pil(self):
        for options in ({}, {"caret": "start"}, {"chars": 1, "kind": "chuan"}, {"chars": 7}):
            with self.subTest(**options):
                image = frame(**options)
                self.assertEqual(draft_width(pixel(image).crop(INPUT)),
                                 draft_width(image.crop(INPUT)))

    def test_width_grows_with_reply_length(self):
        self.assertGreater(draft_width(self.crop(chars=31)), 20 * draft_width(self.crop(chars=1)))


class StructuralVerifyTests(unittest.TestCase):
    def test_one_character_reply_is_delivered(self):
        delivered, trace = run(frame(chars=1, kind="chuan", caret="end"),
                               [frame(caret="start", bubble=True)])
        self.assertTrue(delivered)
        self.assertEqual(trace["draft_left_min"], 0)
        # The first live rule ("input pixels changed > 1%") would have frozen here.
        self.assertLess(trace["peak_input"], 0.01)

    def test_one_character_reply_with_caret_blinking_on_after_enter(self):
        delivered, _ = run(frame(chars=1), [frame(caret="start", bubble=True)])
        self.assertTrue(delivered)

    def test_long_reply_is_delivered(self):
        delivered, trace = run(frame(chars=31, caret="end"), [frame(bubble=True)])
        self.assertTrue(delivered)
        self.assertGreater(trace["draft_before"], 600)

    def test_light_theme_one_character_is_delivered(self):
        delivered, _ = run(frame(LIGHT, chars=1, caret="end"),
                           [frame(LIGHT, caret="start", bubble=True)])
        self.assertTrue(delivered)

    def test_enter_did_nothing_is_not_delivered(self):
        delivered, trace = run(frame(chars=1, caret="end"), [frame(chars=1)])
        self.assertFalse(delivered)
        self.assertGreater(trace["draft_left_min"],
                           trace["max_draft_left"] * trace["draft_before"])

    def test_draft_still_present_under_overlay_is_not_delivered(self):
        # The old rule called this "cleared": lots of input pixels changed.
        delivered, trace = run(frame(chars=5, caret="end"),
                               [frame(chars=5, overlay=True, bubble=True)])
        self.assertFalse(delivered)
        self.assertGreater(trace["peak_input"], 0.01)

    def test_incoming_message_alone_is_not_our_delivery(self):
        delivered, trace = run(frame(chars=3, caret="end"), [frame(chars=3, incoming=True)])
        self.assertFalse(delivered)
        self.assertGreater(trace["peak_message"], trace["threshold"])

    def test_box_cleared_without_conversation_moving_is_not_delivered(self):
        delivered, trace = run(frame(chars=3), [frame()])
        self.assertFalse(delivered)
        self.assertEqual(trace["draft_left_min"], 0)
        self.assertEqual(trace["peak_message"], 0.0)

    def test_baseline_without_visible_draft_cannot_confirm(self):
        delivered, trace = run(frame(caret="end"), [frame(bubble=True)])
        self.assertFalse(delivered)
        self.assertEqual(trace["draft_before"], 0)

    def test_slow_window_is_confirmed_once_it_catches_up(self):
        # Generous timeout: each sample costs ~0.2 s here, and verify returns as
        # soon as it confirms, so this only stops the test racing the clock.
        delivered, trace = run(frame(chars=2),
                               [frame(chars=2), frame(chars=2), frame(bubble=True)], timeout=5)
        self.assertTrue(delivered)
        self.assertEqual(trace["samples"], 3)

    def test_trace_is_optional(self):
        self.assertTrue(verify_delivery("收到", baseline=pixel(frame(chars=2)),
                                        capture=lambda: pixel(frame(bubble=True)), box=MESSAGES,
                                        input_box=INPUT, threshold=0.004, timeout=0.5))


@unittest.skipUnless(Path(FONT_PATH).exists(), "fixture CJK font not installed")
class RealFontShortReplyTests(unittest.TestCase):
    """Real glyphs from the input-readback fixture font (msyh 28px, 2087x74 box).

    With this font the old ratio is ~0.002-0.003 per character, matching the
    live 0.0026; so these are the replies the first live rule would freeze on.
    """

    def frame_with(self, input_image, *, bubble=False):
        image = frame(bubble=bubble)
        image.paste(input_image, INPUT[:2])
        return image

    def test_common_one_and_two_character_replies_are_delivered(self):
        for text in ("行", "好", "嗯", "川", "一", "好的"):
            with self.subTest(text=text):
                caret = 22 + 28 * len(text) + 2
                delivered, trace = run(self.frame_with(input_fixture(text=text, caret=caret)),
                                       [self.frame_with(input_fixture(text="", caret=22),
                                                        bubble=True)])
                self.assertTrue(delivered)
                self.assertLess(trace["peak_input"], 0.01)

    def test_caret_shaped_glyph_cannot_be_told_from_the_caret(self):
        # Rare, and it fails toward UNKNOWN (freeze), never toward "sent".
        delivered, trace = run(self.frame_with(input_fixture(text="丨")),
                               [self.frame_with(input_fixture(text=""), bubble=True)])
        self.assertFalse(delivered)
        self.assertEqual(trace["draft_before"], 0)


class VerifyTraceFileTests(unittest.TestCase):
    def test_trace_is_written_beside_the_ledger_without_text(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = SimpleNamespace(path=str(Path(folder) / "ledger.jsonl"))
            record_verify_trace(ledger, "abc123", {"delivered": False, "draft_before": 0})
            rows = (Path(folder) / "verify-trace.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            row = json.loads(rows[0])
            self.assertEqual(row["commit_id"], "abc123")
            self.assertFalse(row["delivered"])
            self.assertNotIn("text", row)
            self.assertFalse((Path(folder) / "ledger.jsonl").exists())

    def test_empty_trace_or_pathless_ledger_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            record_verify_trace(SimpleNamespace(path=str(Path(folder) / "l.jsonl")), "x", {})
            record_verify_trace(object(), "x", {"delivered": True})
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image, ImageDraw

from message_ocr import LayoutLine, LocalMessageOCR
from visual_recognition import assign_layout_lines, timestamp
from visual_timestamps import prepare_time_line, refine_time_lines
from visual_types import Box, VisualPause


BOX = Box(230, 90, 320, 115)


def time_image(light=False):
    background, ink = ((245, 245, 245), (140, 140, 140)) if light else ((30, 30, 30), (128, 128, 128))
    image = Image.new("RGB", (550, 400), background)
    draw = ImageDraw.Draw(image)
    draw.line((238, 100, 311, 100), fill=ink, width=2)
    draw.line((238, 103, 311, 103), fill=(190, 190, 190) if light else (79, 79, 79))
    return image


def proposal(**kwargs):
    return dataclasses.replace(LayoutLine("", BOX, "两尺度文字不一致", geometry_verified=True,
                                          uncertainty_code="text_disagreement"), **kwargs)


class TimestampRefinementTests(unittest.TestCase):
    def test_located_time_requires_strict_local_confirmation(self):
        ocr = SimpleNamespace(read_line_consistent=Mock(return_value="昨天21:59"))
        with time_image() as image:
            result = refine_time_lines(image, (proposal(),), (), ocr, timestamp)
        self.assertTrue(result[0].timestamp_verified)
        self.assertEqual(result[0].text, "昨天21:59")
        rows, systems, _ = assign_layout_lines([], [], result, 550, 400)
        self.assertFalse(rows)
        self.assertEqual(systems, [BOX])
        self.assertEqual(ocr.read_line_consistent.call_count, 1)
        with self.assertRaises(ValueError):
            ocr.read_line_consistent.call_args.args[0].getpixel((0, 0))

    def test_unverified_global_timestamp_is_not_silently_discarded(self):
        line = proposal(text="昨天21:59", uncertainty="", uncertainty_code="")
        rows, systems, _ = assign_layout_lines([], [], (line,), 550, 400)
        self.assertFalse(systems)
        self.assertFalse(rows[0].safe)

    def test_original_global_time_is_not_fallback_after_local_rejection(self):
        for failure in (VisualPause("static"), "昨天99:99", "昨夭21:59", "ordinary text"):
            reader = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            with time_image() as image:
                result = refine_time_lines(image, (proposal(text="昨天21:59", uncertainty=""),), (),
                                           SimpleNamespace(read_line_consistent=reader), timestamp)
            self.assertFalse(result[0].timestamp_verified)
            self.assertEqual(result[0].text, "")
            rows, systems, _ = assign_layout_lines([], [], result, 550, 400)
            self.assertFalse(systems)
            self.assertFalse(rows[0].safe)
            self.assertIn("局部校验", rows[0].uncertainty)

    def test_bad_geometry_off_center_frame_edge_and_large_line_never_call_local(self):
        cases = (proposal(geometry_verified=False), proposal(box=Box(30, 90, 120, 115)),
                 proposal(box=Box(230, 0, 320, 25)), proposal(box=Box(230, 90, 320, 160)))
        for line in cases:
            ocr = SimpleNamespace(read_line_consistent=Mock())
            with time_image() as image:
                result = refine_time_lines(image, (line,), (), ocr, timestamp)
            self.assertEqual(result, (line,))
            ocr.read_line_consistent.assert_not_called()

    def test_time_inside_or_crossing_a_bubble_is_not_reclassified(self):
        for covered in ((Box(220, 80, 330, 125),), (Box(210, 80, 240, 120),), (Box(210, 80, 225, 120),)):
            ocr = SimpleNamespace(read_line_consistent=Mock(return_value="12:15"))
            with time_image() as image:
                result = refine_time_lines(image, (proposal(),), covered, ocr, timestamp)
            self.assertFalse(result[0].timestamp_verified)
            ocr.read_line_consistent.assert_not_called()

    def test_media_card_fill_colored_pixels_or_bad_ring_cannot_be_time(self):
        for mode in ("card", "color", "ring"):
            with time_image() as image:
                draw = ImageDraw.Draw(image)
                if mode == "card":
                    draw.rectangle((222, 82, 327, 122), fill=(50, 50, 50))
                elif mode == "color":
                    image.putpixel((250, 101), (0, 128, 0))
                else:
                    image.putpixel((222, 82), (128, 128, 128))
                ocr = SimpleNamespace(read_line_consistent=Mock(return_value="12:15"))
                result = refine_time_lines(image, (proposal(),), (), ocr, timestamp)
            self.assertFalse(result[0].timestamp_verified)
            self.assertTrue(result[0].uncertainty)
            ocr.read_line_consistent.assert_not_called()

    def test_contrast_preserves_antialias_in_both_themes_without_new_pixels_saved(self):
        for light in (False, True):
            with time_image(light) as image:
                before = image.tobytes()
                prepared = prepare_time_line(image, BOX, ())
                try:
                    self.assertEqual(prepared.size, (106, 41))
                    values = {pixel[0] for pixel in prepared.get_flattened_data()}
                    self.assertIn(0, values)
                    self.assertIn(255, values)
                    self.assertTrue(any(0 < value < 255 for value in values))
                    self.assertEqual(image.tobytes(), before)
                finally:
                    prepared.close()

    def test_uncertain_text_label_does_not_claim_spacing_and_frame_clip_is_explicit(self):
        rows, _, _ = assign_layout_lines([], [], (proposal(),), 550, 400)
        self.assertIn("两尺度文字不一致", rows[0].uncertainty)
        self.assertNotIn("间距", rows[0].uncertainty)
        clipped = proposal(box=Box(230, 0, 320, 25))
        rows, _, _ = assign_layout_lines([], [], (clipped,), 550, 400)
        self.assertIn("可能被裁切", rows[0].uncertainty)


class DirectLineReaderTests(unittest.TestCase):
    def make(self, results=None, error=None):
        class Engine:
            use_det, use_cls, use_rec = True, False, True
            def __init__(self):
                self.calls = []
            def __call__(self, pixels, **kwargs):
                self.calls.append((pixels.shape, kwargs))
                self.use_det, self.use_cls, self.use_rec = False, False, True
                if error:
                    raise error
                return results[len(self.calls)-1]
        return LocalMessageOCR(engine=Engine())

    def result(self, text="昨天21:59", score=0.99):
        return SimpleNamespace(txts=(text,), scores=(score,))

    def test_two_fixed_scales_and_engine_flags_restored(self):
        ocr = self.make([self.result(), self.result()])
        with Image.new("RGB", (100, 30)) as image:
            self.assertEqual(ocr.read_line_consistent(image), "昨天21:59")
        self.assertEqual([call[0] for call in ocr.engine.calls], [(60,200,3),(90,300,3)])
        self.assertTrue(all(call[1] == {"use_det":False,"use_cls":False,"use_rec":True} for call in ocr.engine.calls))
        self.assertEqual((ocr.engine.use_det, ocr.engine.use_cls, ocr.engine.use_rec), (True,False,True))

    def test_one_good_scale_never_overrides_low_or_different_second_scale(self):
        for second in (self.result(score=.949), self.result("昨天21:58"), self.result(score=float("nan")), self.result("昨天\ufffd")):
            ocr = self.make([self.result(), second])
            with Image.new("RGB", (100, 30)) as image, self.assertRaises(VisualPause):
                ocr.read_line_consistent(image)
            self.assertEqual((ocr.engine.use_det, ocr.engine.use_cls, ocr.engine.use_rec), (True,False,True))

    def test_native_failure_restores_flags_and_does_not_return_global_text(self):
        ocr = self.make(error=RuntimeError("synthetic"))
        with Image.new("RGB", (100, 30)) as image, self.assertRaises(RuntimeError):
            ocr.read_line_consistent(image)
        self.assertEqual((ocr.engine.use_det, ocr.engine.use_cls, ocr.engine.use_rec), (True,False,True))

    def test_unknown_engine_flags_or_invalid_input_are_rejected(self):
        ocr = self.make([self.result(), self.result()])
        ocr.engine.use_det = False
        with Image.new("RGB", (100,30)) as image, self.assertRaises(VisualPause):
            ocr.read_line_consistent(image)
        self.assertFalse(ocr.engine.calls)
        for value in ("https://example.invalid/image", b"data", None):
            with self.assertRaises(VisualPause):
                ocr.read_line_consistent(value)


if __name__ == "__main__":
    unittest.main()

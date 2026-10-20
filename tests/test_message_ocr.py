import dataclasses
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from local_ocr import OcrLine
from message_ocr import LocalMessageOCR, MIN_MESSAGE_SCORE
from visual_recognition import VisualReader, assign_layout_lines, timestamp
from visual_types import Box, Candidate, VisualPause


def line(text="哈哈哈", box=Box(10, 10, 80, 35), uncertainty=""):
    return OcrLine(text, box, uncertainty)


class MessageOCRTests(unittest.TestCase):
    def ocr(self):
        ocr = object.__new__(LocalMessageOCR)
        ocr.engine = Mock(return_value=SimpleNamespace(
            txts=("哈哈哈",), scores=(0.99,), boxes=(((20, 20), (160, 20), (160, 70), (20, 70)),)))
        return ocr

    def test_uses_pinned_local_engine_without_expected_message_hint(self):
        engine = Mock()
        with patch("message_ocr.build_offline_engine", return_value=engine) as build:
            self.assertIs(LocalMessageOCR().engine, engine)
            build.assert_called_once()
        with patch("message_ocr.build_offline_engine") as build:
            self.assertIs(LocalMessageOCR(engine=engine).engine, engine)
            build.assert_not_called()
        self.assertEqual(list(inspect.signature(LocalMessageOCR.read_consistent).parameters), ["self", "image"])

    def test_fixed_scales_and_multiline_content_are_preserved(self):
        ocr = self.ocr()
        rows = (line(), line("第二行", Box(10, 40, 80, 65)))
        with Image.new("RGB", (150, 80)) as image, patch.object(ocr, "recognize", return_value=rows) as recognize:
            self.assertEqual(ocr.read_consistent(image), ("哈哈哈\n第二行", rows))
        self.assertEqual([call.args[1] for call in recognize.call_args_list], [2.0, 3.0])

    def test_empty_result_stays_empty_not_a_fabricated_message(self):
        ocr = self.ocr()
        ocr.engine.return_value = SimpleNamespace(txts=None, scores=None, boxes=None)
        with Image.new("RGB", (150, 80)) as image:
            self.assertEqual(ocr.read_consistent(image), ("", ()))

    def test_low_missing_or_nonfinite_score_cannot_be_text(self):
        for score in (MIN_MESSAGE_SCORE - 0.001, float("nan"), float("inf")):
            ocr = self.ocr()
            ocr.engine.return_value.scores = (score,)
            with Image.new("RGB", (150, 80)) as image:
                with self.assertRaises(VisualPause):
                    ocr.read_consistent(image)
                located = ocr.scan_layout(image)
            self.assertTrue(located[0].uncertainty)
            self.assertEqual(located[0].text, "")
        ocr.engine.return_value.scores = ()
        with Image.new("RGB", (150, 80)) as image, self.assertRaises(VisualPause):
            ocr.scan_layout(image)

    def test_no_url_path_encoded_image_or_too_large_input(self):
        ocr = self.ocr()
        for value in ("https://example.invalid/a.png", b"image", 123):
            with self.assertRaises(VisualPause):
                ocr.read_consistent(value)
        with Image.new("RGB", (1366, 20)) as image, self.assertRaises(VisualPause):
            ocr.read_consistent(image)
        ocr.engine.assert_not_called()

    def test_bgr_memory_only_and_both_raw_scales_used(self):
        ocr = self.ocr()
        with Image.new("RGB", (150, 80), (10, 20, 30)) as image:
            ocr.recognize(image, 2.0)
            ocr.recognize(image, 3.0)
        for call, shape in zip(ocr.engine.call_args_list, ((160, 300, 3), (240, 450, 3))):
            self.assertEqual(call.args[0].shape, shape)
            self.assertEqual(tuple(call.args[0][0, 0]), (30, 20, 10))

    def test_cross_scale_disagreement_or_missing_line_pauses_no_fallback(self):
        ocr = self.ocr()
        for second in ((line("哈啥哈"),), (), (line(box=Box(10, 20, 80, 45)),)):
            with Image.new("RGB", (150, 80)) as image, patch.object(ocr, "recognize", side_effect=[(line(),), second]):
                with self.assertRaises(VisualPause):
                    ocr.read_consistent(image)
        self.assertEqual(ocr.engine.call_count, 0)

    def test_internal_spaces_are_not_repaired(self):
        ocr = self.ocr()
        with Image.new("RGB", (150, 80)) as image, patch.object(ocr, "recognize", return_value=(line("哈 哈哈"),)):
            self.assertEqual(ocr.read_consistent(image)[0], "哈 哈哈")

    def test_unsupported_geometry_and_characters_do_not_pass(self):
        for corners in ((((0, 0),),), (((-1, 0), (10, 0), (10, 10), (0, 10)),),
                        (((0, 0), (float("nan"), 0), (10, 10), (0, 10)),)):
            ocr = self.ocr()
            ocr.engine.return_value.boxes = corners
            with Image.new("RGB", (150, 80)) as image, self.assertRaises(VisualPause):
                ocr.recognize(image, 2)
        for text in ("", "哈\n哈", "哈\ufffd", "哈\x00"):
            ocr = self.ocr()
            ocr.engine.return_value.txts = (text,)
            with Image.new("RGB", (150, 80)) as image, self.assertRaises(VisualPause):
                ocr.read_consistent(image)

    def test_layout_disagreement_retains_uncertain_location_not_text(self):
        ocr = self.ocr()
        first = (line("昨天21:59", Box(220, 30, 300, 50)),)
        second = (line("昨天21:58", Box(221, 31, 300, 51)),)
        with Image.new("RGB", (550, 540)) as image, patch.object(ocr, "_recognize", side_effect=[first, second]) as recognize:
            located = ocr.scan_layout(image)
        self.assertEqual([call.args[1] for call in recognize.call_args_list], [2.0, 3.0])
        self.assertTrue(all(call.kwargs["allow_uncertain"] for call in recognize.call_args_list))
        self.assertEqual(located[0].text, "")
        self.assertTrue(located[0].uncertainty)
        self.assertEqual(located[0].box, Box(220, 30, 300, 51))
        result, systems, _ = assign_layout_lines([], [], located, 550, 540)
        self.assertFalse(systems)
        self.assertFalse(result[0].safe)

    def test_layout_missing_line_does_not_silently_discard_it(self):
        ocr = self.ocr()
        with Image.new("RGB", (150, 80)) as image, patch.object(ocr, "_recognize", side_effect=[(line(),), ()]):
            with self.assertRaises(VisualPause):
                ocr.scan_layout(image)

    def test_empty_ocr_is_unknown_not_a_claim_that_it_is_media(self):
        from test_visual import calibration
        cal = calibration()
        bubble = SimpleNamespace(box=Box(60, 30, 200, 80), direction="friend", uncertainty="")
        ocr = SimpleNamespace(read_consistent=Mock(return_value=("", ())), recognize=Mock(return_value=()))
        title_ocr = SimpleNamespace(read_consistent=Mock(return_value=(cal.expected_title, ())))
        def capture(regions):
            return tuple(Image.new("RGB", (box.width, box.height), (240, 240, 240)) for box in regions)
        with patch("visual_recognition.find_bubbles", return_value=(bubble,)):
            _, rows = VisualReader(capture, ocr, title_ocr).poll(cal, lambda _: False)
        self.assertEqual(rows[0].kind, "unknown")
        self.assertIn("未识别到完整文字", rows[0].uncertainty)
        self.assertFalse(rows[0].safe)


class TimestampGuardTests(unittest.TestCase):
    def test_exact_centered_time_is_system_only_outside_bubbles(self):
        item = line("昨天21:59", Box(230, 30, 320, 50))
        result, systems, unknown = assign_layout_lines([], [], [item], 550, 540)
        self.assertFalse(result or unknown)
        self.assertEqual(systems, [item.box])
        for box in (Box(200, 20, 340, 80), Box(210, 20, 250, 80)):
            row = Candidate("", "unknown", "unknown", box, "synthetic", "unknown")
            result, systems, _ = assign_layout_lines([row], [box], [item], 550, 540)
            self.assertFalse(systems)
            self.assertTrue(result)

    def test_off_center_oversized_and_uncertain_time_remain_unknown(self):
        for item in (line("12:15", Box(50, 30, 130, 50)),
                     line("12:15", Box(230, 30, 320, 100)),
                     line("12:15", Box(230, 30, 320, 50), "uncertain")):
            result, systems, _ = assign_layout_lines([], [], [item], 550, 540)
            self.assertFalse(systems)
            self.assertFalse(result[0].safe)

    def test_invalid_time_similar_characters_and_fullwidth_colon_not_repaired(self):
        for text in ("昨天99:99", "昨天21:60", "昨夭21:59", "昨天21：59", "2026年13月1日"):
            self.assertFalse(timestamp(text))
        for text in ("昨天21:59", "昨天 21:59", "12:15", "2026年10月17日 01:19"):
            self.assertTrue(timestamp(text))


if __name__ == "__main__":
    unittest.main()

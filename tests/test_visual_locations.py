import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from local_ocr import OcrLine, WindowsOCR, located_line
from visual_locations import LocationPreview, location_rows
from visual_recognition import VisualReader, assign_layout_lines
from visual_types import Box, Candidate, VisualPause


def row(box=Box(40, 20, 340, 400), safe=False):
    return Candidate("confirmed synthetic text" if safe else "", "friend" if safe else "self",
                     "text_candidate" if safe else "unknown", box, "synthetic", "" if safe else "过大/复杂块")


class LayoutLocationTests(unittest.TestCase):
    def test_strict_body_still_rejects_ambiguous_han_gap(self):
        words = [("张", Box(10, 10, 30, 35)), ("三", Box(60, 10, 80, 35))]
        with self.assertRaises(VisualPause):
            located_line(words, Box(10, 10, 80, 35))
        located = located_line(words, Box(10, 10, 80, 35), True)
        self.assertEqual(located.text, "")
        self.assertTrue(located.uncertainty)
        self.assertEqual(located.box, Box(10, 10, 80, 35))

    def test_locating_good_line_does_not_change_text(self):
        words = [("文", Box(10, 10, 30, 35)), ("件", Box(33, 10, 53, 35))]
        self.assertEqual(located_line(words, Box(10, 10, 53, 35), True).text, "文件")

    def test_unsafe_bubble_owns_all_contained_bad_lines_and_stays_unsafe(self):
        item = row()
        boxes = (Box(50, 40, 280, 60), Box(50, 100, 320, 122))
        lines = [OcrLine("", box, "gap") for box in boxes]
        result, systems, unknown = assign_layout_lines([item], [item.box], lines, 550, 540)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].safe)
        self.assertEqual(result[0].evidence_boxes, boxes)
        self.assertFalse(systems or unknown)

    def test_good_identical_messages_remain_separate_next_to_unsafe_block(self):
        items = [row(), row(Box(40, 410, 170, 460), True), row(Box(40, 475, 170, 525), True)]
        line = OcrLine("", Box(50, 40, 280, 60), "gap")
        result, _, _ = assign_layout_lines(items[:], [item.box for item in items], [line], 550, 540)
        self.assertEqual(result[1:], items[1:])
        self.assertEqual(len(result), 3)

    def test_uncertain_line_in_safe_bubble_invalidates_text(self):
        item = row(safe=True)
        result, _, _ = assign_layout_lines([item], [item.box], [OcrLine("", Box(50, 40, 280, 60), "gap")], 550, 540)
        self.assertFalse(result[0].safe)
        self.assertEqual(result[0].text, "")
        self.assertEqual(result[0].kind, "unknown")

    def test_outside_uncertain_line_is_not_silently_discarded(self):
        item = row()
        bad = OcrLine("", Box(360, 40, 520, 60), "gap")
        result, _, unknown = assign_layout_lines([item], [item.box], [bad], 550, 540)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1].box, bad.box)
        self.assertFalse(result[1].safe)
        self.assertEqual(unknown, [bad.box])

    def test_center_inside_but_box_crossing_bubble_is_not_hidden(self):
        item = row()
        for uncertainty in ("", "gap"):
            bad = OcrLine("synthetic", Box(20, 40, 280, 60), uncertainty)
            result, _, _ = assign_layout_lines([item], [item.box], [bad], 550, 540)
            self.assertEqual(len(result), 2)
            self.assertFalse(result[-1].safe)

    def test_gap_inside_two_overlapping_blocks_stays_unassigned(self):
        items = [row(), row(Box(45, 25, 330, 390))]
        result, _, _ = assign_layout_lines(items[:], [item.box for item in items],
                                          [OcrLine("", Box(50, 40, 280, 60), "gap")], 550, 540)
        self.assertEqual(len(result), 3)
        self.assertFalse(result[-1].safe)

    def test_out_of_region_layout_box_remains_whole_region_uncertainty(self):
        result, _, _ = assign_layout_lines([], [], [OcrLine("synthetic", Box(-2, 10, 30, 30))], 550, 540)
        self.assertEqual(result[0].box, Box(0, 0, 550, 540))
        self.assertFalse(result[0].safe)

    def test_timestamp_inside_bubble_does_not_hide_message(self):
        item = dataclasses.replace(row(safe=True), text="12:15")
        result, systems, _ = assign_layout_lines([item], [item.box], [OcrLine("12:15", Box(50, 40, 100, 60))], 550, 540)
        self.assertEqual(result, [item])
        self.assertFalse(systems)

    def test_bad_timestamp_looking_line_is_not_ignored(self):
        result, systems, _ = assign_layout_lines([], [], [OcrLine("12:15", Box(50, 40, 100, 60), "gap")], 550, 540)
        self.assertFalse(systems)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].safe)

    def test_scan_layout_is_separate_from_strict_bubble_reading(self):
        ocr = object.__new__(WindowsOCR)
        image = Image.new("RGB", (100, 50))
        with patch.object(ocr, "_recognize", return_value=()) as recognize:
            ocr.scan_layout(image)
            self.assertTrue(recognize.call_args.kwargs["allow_uncertain"])
            ocr.recognize(image, 2.0)
            self.assertFalse(recognize.call_args.kwargs["allow_uncertain"])

    def test_schematic_has_no_text_fingerprint_or_identity(self):
        item = dataclasses.replace(row(safe=True), identity="secret-id", fingerprint="secret-fingerprint")
        result = location_rows([item], 550, 540)
        rendered = str(result)
        self.assertNotIn(item.text, rendered)
        self.assertNotIn(item.fingerprint, rendered)
        self.assertNotIn(item.identity, rendered)
        self.assertEqual(set(result[0]), {"index", "safe", "box", "evidence"})

    def test_schematic_rejects_invalid_region_or_evidence(self):
        for width, height, item in ((0, 540, row()), (550, 540, dataclasses.replace(row(), evidence_boxes=(Box(0, 0, 600, 20),)))):
            with self.assertRaises(ValueError):
                location_rows([item], width, height)

    def test_stale_schematic_closes_without_capture_or_input(self):
        preview = object.__new__(LocationPreview)
        preview.closed = False
        preview.is_current = lambda: False
        preview.window = Mock()
        preview.check()
        self.assertTrue(preview.closed)
        preview.window.destroy.assert_called_once_with()
        preview.window.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()

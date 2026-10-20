import dataclasses
import inspect
import json
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_ocr import OcrLine, WindowsOCR, line_text
from visual_capture import WindowsCapture
from visual_monitor import ReadOnlyMonitor
from visual_recognition import VisualReader, find_bubbles, frames_stable, media_or_system
from visual_types import Box, Calibration, Candidate, ObservationTracker, RevisionGate, VisualPause, WindowBinding, exact_title, save_calibration


def calibration():
    return Calibration(WindowBinding(123, 456, "C:/Program Files/Tencent/Weixin/Weixin.exe", "test", 700, 700, 144),
                       "test:filehelper", "文件传输助手", Box(100, 10, 450, 50), Box(100, 80, 650, 620), True, "filehelper")


def candidate(text="你好", y=30, fingerprint="pixels-hello", direction="friend"):
    return Candidate(text, direction, "text_candidate", Box(60, y, 180, y + 42), fingerprint)


class FakeOCR:
    def __init__(self, title="文件传输助手"):
        self.actual_title = title
    def read_consistent(self, image):
        return self.actual_title, ()
    def recognize(self, image):
        return ()


class VisualContractsTests(unittest.TestCase):
    def test_local_ocr_two_passes_must_still_agree(self):
        ocr = object.__new__(WindowsOCR)
        first = (OcrLine("文件传输助手", Box(0, 0, 150, 30)),)
        second = (OcrLine("文件传输助于", Box(0, 0, 150, 30)),)
        with patch.object(ocr, "recognize", side_effect=[first, second]):
            with self.assertRaisesRegex(VisualPause, "不一致"):
                ocr.read_consistent(Image.new("RGB", (200, 50), "white"))

    def test_local_ocr_replacement_character_is_not_accepted(self):
        ocr = object.__new__(WindowsOCR)
        bad = (OcrLine("你\ufffd", Box(0, 0, 50, 30)),)
        with patch.object(ocr, "recognize", return_value=bad):
            with self.assertRaises(VisualPause):
                ocr.read_consistent(Image.new("RGB", (200, 50), "white"))

    def test_local_normalization_removes_green_but_preserves_gray_strokes(self):
        ocr = object.__new__(WindowsOCR)
        pixels = []
        def recognize(image, scale):
            pixels.append((image.getpixel((20, 20)), image.getpixel((25, 25)), scale))
            return (OcrLine("你好", Box(0, 0, 50, 30)),)
        source = Image.new("RGB", (150, 50), (80, 210, 120))
        source.putpixel((5, 5), (64, 64, 64))
        with patch.object(ocr, "recognize", side_effect=recognize):
            self.assertEqual(ocr.read_consistent(source)[0], "你好")
        self.assertEqual([entry[2] for entry in pixels], [2.0, 3.0])
        self.assertEqual(pixels[0][0], (255, 255, 255))
        self.assertTrue(0 < pixels[0][1][0] < 255)

    def test_local_dark_theme_is_normalized_in_memory(self):
        ocr = object.__new__(WindowsOCR)
        samples = []
        def recognize(image, scale):
            samples.append((image.getpixel((20, 20)), image.getpixel((25, 25))))
            return (OcrLine("你好", Box(0, 0, 50, 30)),)
        source = Image.new("RGB", (150, 50), (50, 50, 50))
        source.putpixel((5, 5), (255, 255, 255))
        with patch.object(ocr, "recognize", side_effect=recognize):
            ocr.read_consistent(source)
        self.assertEqual(samples[0], ((255, 255, 255), (0, 0, 0)))

    def test_full_title_only_no_fuzzy_substring_or_space_removal(self):
        self.assertTrue(exact_title(" 文件传输助手 ", "文件传输助手"))
        for wrong in ("文件传输助于", "文件传输助手(2)", "文件传输助手A", "文件 传输助手", "文件传输助", "文件传输助手\n别名"):
            self.assertFalse(exact_title(wrong, "文件传输助手"))

    def test_calibration_requires_explicit_single_chat_confirmation(self):
        with self.assertRaises(VisualPause):
            dataclasses.replace(calibration(), single_chat_confirmed=False).validate()

    def test_calibration_rejects_overlap_outside_and_empty_regions(self):
        for box in (Box(0, 20, 600, 500), Box(-1, 100, 600, 600), Box(100, 100, 200, 100)):
            with self.assertRaises(VisualPause):
                dataclasses.replace(calibration(), messages=box).validate()

    def test_calibration_metadata_roundtrip_has_no_image_or_chat(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            path = Path(folder) / "calibration.json"
            save_calibration(path, calibration())
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(Calibration.from_dict(raw), calibration())
            self.assertEqual(set(raw), {"binding", "profile_id", "expected_title", "title", "messages", "single_chat_confirmed", "target_kind", "version"})

    def test_bound_executable_cannot_be_arbitrary_program(self):
        self.assertTrue(WindowsCapture.trusted_exe("C:/Program Files/Tencent/Weixin/Weixin.exe"))
        self.assertFalse(WindowsCapture.trusted_exe("C:/Temp/Weixin.exe"))

    def test_geometry_not_fuzzy_normalization_rebuilds_han(self):
        self.assertEqual(line_text([("文", Box(0, 0, 28, 32)), ("件", Box(34, 0, 62, 32))]), "文件")
        with self.assertRaises(VisualPause):
            line_text([("张", Box(0, 0, 28, 32)), ("三", Box(50, 0, 78, 32))])

    def test_latin_words_keep_recognized_word_separation(self):
        self.assertEqual(line_text([("hello", Box(0, 0, 60, 30)), ("world", Box(70, 0, 130, 30))]), "hello world")

    def test_messages_with_unknown_direction_are_not_safe(self):
        self.assertFalse(dataclasses.replace(candidate(), direction="unknown").safe)
        self.assertFalse(dataclasses.replace(candidate(), uncertainty="识别不确定").safe)
        self.assertFalse(dataclasses.replace(candidate(), kind="non_text").safe)

    def test_media_markers_not_ordinary_text(self):
        for text in ('8"', "[语音]", "微信转账", "附件.pdf", "13.2 MB", "[图片]", "[表情]", "红包"):
            self.assertTrue(media_or_system(text), text)
        self.assertFalse(media_or_system("今天怎么样呀？"))

    def test_pixel_change_is_not_a_stable_frame(self):
        first = Image.new("RGB", (100, 100), "white")
        second = first.copy()
        self.assertTrue(frames_stable(first, second))
        second.putpixel((50, 50), (0, 0, 0))
        self.assertFalse(frames_stable(first, second))

    def test_header_mismatch_never_captures_message_roi(self):
        calls = []
        def capture(regions):
            calls.append(regions)
            return (Image.new("RGB", (350, 40), "white"),)
        reader = VisualReader(capture, FakeOCR("文件传输助于"))
        with self.assertRaisesRegex(VisualPause, "标题不匹配"):
            reader.poll(calibration(), lambda _: False)
        self.assertEqual(calls, [(calibration().title,)])

    def test_unstable_frames_pause_before_message_ocr(self):
        count = [0]
        def capture(regions):
            image = Image.new("RGB", (regions[0].width, regions[0].height), "white")
            if regions[0] == calibration().messages:
                count[0] += 1
                if count[0] == 2:
                    image.putpixel((50, 50), (0, 0, 0))
            return (image,)
        with self.assertRaisesRegex(VisualPause, "不稳定"):
            VisualReader(capture, FakeOCR()).poll(calibration(), lambda _: False)

    def test_tail_less_picture_block_does_not_get_a_direction(self):
        image = Image.new("RGB", (600, 320), (240, 240, 240))
        ImageDraw.Draw(image).rectangle((90, 70, 280, 190), fill="white")
        rows = find_bubbles(image)
        self.assertTrue(rows)
        self.assertTrue(all(row.direction == "unknown" for row in rows))

    def test_left_neutral_and_right_green_tails_classify_geometry(self):
        image = Image.new("RGB", (700, 320), (240, 240, 240))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((90, 50, 270, 106), radius=8, fill="white")
        draw.polygon(((90, 64), (80, 72), (90, 80)), fill="white")
        draw.rounded_rectangle((440, 160, 610, 216), radius=8, fill=(80, 210, 120))
        draw.polygon(((610, 174), (620, 182), (610, 190)), fill=(80, 210, 120))
        self.assertEqual([row.direction for row in find_bubbles(image)], ["friend", "self"])

    def test_image_only_without_ocr_is_not_silently_accepted(self):
        def capture(regions):
            image = Image.new("RGB", (regions[0].width, regions[0].height), (240, 240, 240))
            if regions[0] == calibration().messages:
                ImageDraw.Draw(image).rectangle((150, 100, 340, 250), fill=(200, 30, 70))
            return (image,)
        _, rows = VisualReader(capture, FakeOCR()).poll(calibration(), lambda _: False)
        self.assertTrue(rows)
        self.assertTrue(any(row.kind == "non_text" and not row.safe for row in rows))

    def test_stopping_after_title_prevents_message_capture(self):
        calls = []
        def capture(regions):
            calls.append(regions)
            return (Image.new("RGB", (regions[0].width, regions[0].height), "white"),)
        with self.assertRaises(VisualPause):
            VisualReader(capture, FakeOCR()).poll(calibration(), lambda _: True)
        self.assertEqual(len(calls), 2)  # title and first message, no later frame


class OccurrenceAndDraftTests(unittest.TestCase):
    def test_starting_messages_are_baseline_only(self):
        tracker = ObservationTracker()
        rows, added = tracker.update((candidate(), candidate(y=90)))
        self.assertEqual(len(added), 0)
        self.assertTrue(all(row.origin == "启动基线" for row in rows))

    def test_identical_text_occurrences_have_separate_ids(self):
        tracker = ObservationTracker()
        rows, _ = tracker.update((candidate(), candidate(y=90)))
        self.assertNotEqual(rows[0].identity, rows[1].identity)
        refreshed, added = tracker.update((candidate(), candidate(y=90), candidate(y=150)))
        self.assertEqual(len(added), 1)
        self.assertEqual(rows[0].identity, refreshed[0].identity)
        self.assertEqual(rows[1].identity, refreshed[1].identity)
        self.assertNotIn(added[0].identity, {row.identity for row in rows})

    def test_same_refresh_is_not_new(self):
        tracker = ObservationTracker()
        first, _ = tracker.update((candidate(),))
        second, added = tracker.update((candidate(),))
        self.assertFalse(added)
        self.assertEqual(first[0].identity, second[0].identity)

    def test_scroll_or_reflow_does_not_become_a_new_message(self):
        for updated in ((candidate(y=10),), (dataclasses.replace(candidate(), box=Box(60, 30, 200, 80)),), ()):
            tracker = ObservationTracker()
            tracker.update((candidate(),))
            with self.assertRaises(VisualPause):
                tracker.update(updated)

    def test_same_text_changed_pixels_is_not_text_only_dedup(self):
        tracker = ObservationTracker()
        tracker.update((candidate(),))
        with self.assertRaises(VisualPause):
            tracker.update((candidate(fingerprint="different-image"),))

    def test_multiline_message_remains_one_occurrence(self):
        tracker = ObservationTracker()
        rows, added = tracker.update((candidate(text="第一行\n第二行"),))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text, "第一行\n第二行")
        self.assertFalse(added)

    def test_new_message_switch_manual_reply_and_pause_invalidate_token(self):
        for action in ("new-message", "switch-contact", "manual-reply", "pause"):
            gate = RevisionGate()
            token = gate.start()
            self.assertTrue(gate.accepts(token))
            gate.stop() if action == "pause" else gate.invalidate()
            self.assertFalse(gate.accepts(token))

    def test_visual_connector_has_no_send_path(self):
        monitor = ReadOnlyMonitor(lambda _: ("test:filehelper", "文件传输助手"))
        with self.assertRaises(PermissionError):
            monitor.send("test")
        source = inspect.getsource(WindowsCapture)
        for forbidden in ("SendInput", "keybd_event", "mouse_event", "SetForegroundWindow", "SendMessage", "PostMessage"):
            self.assertNotIn(forbidden, source)

    def test_pause_discards_inflight_result_and_prevents_further_capture(self):
        reached, release = threading.Event(), threading.Event()
        captures = []
        class Backend:
            def capture(self, *_):
                captures.append("capture")
                return ()
        class Reader:
            def __init__(self, capture, _, title_ocr=None):
                self.capture = capture
            def poll(self, c, wait):
                self.capture((c.title,))
                reached.set()
                release.wait(3)
                self.capture((c.messages,))
                return "文件传输助手", (candidate(),)
        monitor = ReadOnlyMonitor(lambda _: ("test:filehelper", "文件传输助手"), Backend, object, object)
        with patch("visual_recognition.VisualReader", Reader):
            monitor.start(calibration(), once=True, delay=0)
            self.assertTrue(reached.wait(3))
            monitor.stop()
            release.set()
            monitor.thread.join(3)
        self.assertEqual(captures, ["capture"])
        self.assertTrue(monitor.events.empty())
        self.assertFalse(monitor.running)


class CaptureGuardTests(unittest.TestCase):
    def backend(self):
        binding = calibration().binding
        backend = object.__new__(WindowsCapture)
        backend.user = SimpleNamespace(SetThreadDpiAwarenessContext=lambda _: 1)
        backend.binding = lambda _: binding
        backend._desktop_unlocked = lambda: True
        backend.api = SimpleNamespace(GetSystemMetrics=lambda code: {76: 0, 77: 0, 78: 1920, 79: 1080}[code])
        backend.gui = SimpleNamespace(IsIconic=lambda _: False, IsWindowVisible=lambda _: True,
            GetForegroundWindow=lambda: binding.hwnd, ClientToScreen=lambda *_: (10, 10),
            GetTopWindow=lambda _: binding.hwnd, GetAncestor=lambda *_: binding.hwnd,
            WindowFromPoint=lambda _: binding.hwnd, GetWindow=lambda *_: binding.hwnd,
            GetWindowRect=lambda _: (0, 0, 600, 600), error=OSError)
        return backend

    def test_locked_desktop_does_not_capture(self):
        backend = self.backend()
        backend._desktop_unlocked = lambda: False
        with patch.object(backend, "_grab_region") as capture:
            with self.assertRaisesRegex(VisualPause, "锁定"):
                backend.capture(calibration().binding, (calibration().title,))
            capture.assert_not_called()

    def test_minimized_window_does_not_capture(self):
        backend = self.backend()
        backend.gui.IsIconic = lambda _: True
        with patch.object(backend, "_grab_region") as capture:
            with self.assertRaisesRegex(VisualPause, "最小化"):
                backend.capture(calibration().binding, (calibration().title,))
            capture.assert_not_called()

    def test_focus_change_does_not_capture(self):
        backend = self.backend()
        backend.gui.GetForegroundWindow = lambda: 999
        with patch.object(backend, "_grab_region") as capture:
            with self.assertRaisesRegex(VisualPause, "不在前台"):
                backend.capture(calibration().binding, (calibration().title,))
            capture.assert_not_called()

    def test_size_or_dpi_change_requires_recalibration(self):
        for changed in (dataclasses.replace(calibration().binding, width=701), dataclasses.replace(calibration().binding, dpi=96)):
            backend = self.backend()
            backend.binding = lambda _: changed
            with self.assertRaisesRegex(VisualPause, "DPI"):
                backend.verify(calibration().binding, (calibration().title,))

    def test_occlusion_does_not_capture(self):
        backend = self.backend()
        backend.gui.GetTopWindow = lambda _: 789
        with patch.object(backend, "_grab_region") as capture:
            with self.assertRaisesRegex(VisualPause, "遮挡"):
                backend.capture(calibration().binding, (calibration().title,))
            capture.assert_not_called()

    def test_region_ownership_must_be_exact(self):
        backend = self.backend()
        backend.gui.GetAncestor = lambda *_: 789
        with self.assertRaisesRegex(VisualPause, "区域归属"):
            backend.verify(calibration().binding, (calibration().title,))

    def test_capture_implementation_does_not_use_full_desktop_pillow_grab(self):
        source = inspect.getsource(WindowsCapture.capture)
        self.assertNotIn("ImageGrab", source)
        self.assertIn("self._grab_region(box)", source)

    def test_capture_requests_only_the_exact_bound_roi(self):
        backend = self.backend()
        requested = []
        def region(box):
            requested.append(box)
            return Image.new("RGB", (box.width, box.height), "white")
        with patch.object(backend, "_grab_region", side_effect=region):
            result = backend.capture(calibration().binding, (calibration().title,))
        self.assertEqual(requested, [Box(110, 20, 460, 60)])
        self.assertEqual(result[0].size, (350, 40))
        result[0].close()

    def test_focus_change_during_capture_discards_image(self):
        backend = self.backend()
        image = Image.new("RGB", (350, 40), "white")
        def region(_):
            backend.gui.GetForegroundWindow = lambda: 999
            return image
        with patch.object(backend, "_grab_region", side_effect=region):
            with self.assertRaises(VisualPause):
                backend.capture(calibration().binding, (calibration().title,))
        with self.assertRaises(ValueError):
            image.getpixel((0, 0))


if __name__ == "__main__":
    unittest.main()

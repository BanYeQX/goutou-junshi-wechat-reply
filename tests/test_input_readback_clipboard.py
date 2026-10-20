"""Offline tests for the clipboard readback. No real clipboard, no keyboard."""
import unittest

from input_readback import Verdict
from input_readback_clipboard import (
    PortError,
    make_sentinel,
    read_input_by_clipboard,
)

TEXT_FORMAT = 13
IMAGE_FORMAT = 8


class FakeClipboard:
    def __init__(self, items=None):
        self.items = list(items if items is not None else [(TEXT_FORMAT, "旧内容\x00".encode("utf-16-le"))])
        self.snapshot_calls = 0
        self.restore_calls = 0
        self.restored = None
        self.fail_snapshot = False
        self.fail_restore = False
        self.fail_write = False

    # --- port surface -------------------------------------------------
    def snapshot(self):
        self.snapshot_calls += 1
        if self.fail_snapshot:
            raise PortError("snapshot_scripted_failure")
        return tuple(self.items)

    def write_text(self, text):
        if self.fail_write:
            raise PortError("write_scripted_failure")
        self.items = [(TEXT_FORMAT, (text + "\x00").encode("utf-16-le"))]

    def read_text(self):
        for fmt, data in self.items:
            if fmt == TEXT_FORMAT:
                return data.decode("utf-16-le").rstrip("\x00")
        return None

    def restore(self, items):
        self.restore_calls += 1
        if self.fail_restore:
            raise PortError("restore_scripted_failure")
        self.items = list(items)
        self.restored = tuple(items)


class FakeKeyboard:
    def __init__(self, clipboard, box_text):
        self.clipboard = clipboard
        self.box_text = box_text
        self.select_all_calls = 0
        self.copy_calls = 0
        self.raise_timeout = False

    def select_all(self):
        self.select_all_calls += 1

    def copy(self, timeout=2.0):
        self.copy_calls += 1
        if self.raise_timeout:
            raise TimeoutError()
        if self.box_text is None:
            return              # empty box: the clipboard is NOT rewritten
        if self.box_text == "":
            return
        self.clipboard.write_text(self.box_text)


def run(box_text, expected, clipboard=None):
    clipboard = clipboard or FakeClipboard()
    keyboard = FakeKeyboard(clipboard, box_text)
    return read_input_by_clipboard(expected, keyboard=keyboard, clipboard=clipboard), clipboard


class ClipboardReadbackTests(unittest.TestCase):
    def test_exact_text_matches(self):
        result, clipboard = run("收到", "收到")
        self.assertIs(result.verdict, Verdict.MATCH)
        self.assertEqual(result.reason, "exact_clipboard_text_match")
        self.assertEqual(clipboard.restore_calls, 1)

    def test_extra_text_is_mismatch(self):
        result, _ = run("收到好的", "收到")
        self.assertIs(result.verdict, Verdict.MISMATCH)

    def test_trailing_space_is_mismatch_not_stripped(self):
        result, _ = run("收到 ", "收到")
        self.assertIs(result.verdict, Verdict.MISMATCH)
        self.assertEqual(result.ocr_text, "收到 ")

    def test_empty_input_reports_empty_never_match(self):
        result, clipboard = run(None, "收到")
        self.assertIs(result.verdict, Verdict.UNCERTAIN)
        self.assertEqual(result.reason, "input_empty")
        self.assertEqual(clipboard.restore_calls, 1)

    def test_sentinel_detects_that_copy_did_nothing(self):
        clipboard = FakeClipboard()
        keyboard = FakeKeyboard(clipboard, None)
        result = read_input_by_clipboard("收到", keyboard=keyboard, clipboard=clipboard)
        self.assertEqual(result.reason, "input_empty")
        # the clipboard must be the restored original, not the sentinel
        self.assertEqual(clipboard.read_text(), "旧内容")

    def test_image_clipboard_is_restored_byte_for_byte(self):
        payload = bytes(range(64))
        clipboard = FakeClipboard([(IMAGE_FORMAT, payload)])
        result, _ = run("收到", "收到", clipboard)
        self.assertIs(result.verdict, Verdict.MATCH)
        self.assertEqual(clipboard.restored, ((IMAGE_FORMAT, payload),))

    def test_restore_failure_is_uncertain_even_when_text_matched(self):
        clipboard = FakeClipboard()
        clipboard.fail_restore = True
        result, _ = run("收到", "收到", clipboard)
        self.assertIs(result.verdict, Verdict.UNCERTAIN)
        self.assertTrue(result.reason.startswith("clipboard_restore_failed"))

    def test_copy_timeout_is_uncertain_and_still_restores(self):
        clipboard = FakeClipboard()
        keyboard = FakeKeyboard(clipboard, "收到")
        keyboard.raise_timeout = True
        result = read_input_by_clipboard("收到", keyboard=keyboard, clipboard=clipboard)
        self.assertIs(result.verdict, Verdict.UNCERTAIN)
        self.assertEqual(result.reason, "copy_timeout")
        self.assertEqual(clipboard.restore_calls, 1)

    def test_wrong_focus_is_rejected(self):
        result, _ = run("第一条消息\n第二条消息\n第三条消息", "收到")
        self.assertIs(result.verdict, Verdict.UNCERTAIN)
        self.assertEqual(result.reason, "possible_wrong_focus")

    def test_long_single_line_is_also_wrong_focus(self):
        result, _ = run("收" * 40, "收到")
        self.assertEqual(result.reason, "possible_wrong_focus")

    def test_snapshot_failure_does_not_touch_the_keyboard(self):
        clipboard = FakeClipboard()
        clipboard.fail_snapshot = True
        keyboard = FakeKeyboard(clipboard, "收到")
        result = read_input_by_clipboard("收到", keyboard=keyboard, clipboard=clipboard,
                                         policy="strict")
        self.assertIs(result.verdict, Verdict.UNCERTAIN)
        self.assertEqual(result.reason, "clipboard_snapshot_failed:snapshot_scripted_failure")
        self.assertEqual(keyboard.copy_calls, 0)

    def test_caret_is_not_an_input_so_verdicts_are_identical(self):
        verdicts = set()
        for _ in range(4):          # the API has no caret parameter at all
            result, _ = run("收到", "收到")
            verdicts.add(result.verdict)
        self.assertEqual(verdicts, {Verdict.MATCH})

    def test_sentinel_is_unique(self):
        self.assertNotEqual(make_sentinel(), make_sentinel())
        self.assertTrue(make_sentinel().startswith("wxreadback-"))



class TextOnlyPolicyTests(unittest.TestCase):
    """Clipboard holding formats we cannot preserve: strict refuses, text_only degrades."""

    def _clipboard(self):
        clip = FakeClipboard()
        clip.fail_snapshot = True
        return clip

    def test_strict_refuses(self):
        clip = self._clipboard()
        keyboard = FakeKeyboard(clip, "收到")
        result = read_input_by_clipboard("收到", keyboard=keyboard, clipboard=clip,
                                         policy="strict")
        self.assertIs(result.verdict, Verdict.UNCERTAIN)
        self.assertTrue(result.reason.startswith("clipboard_snapshot_failed"))
        self.assertEqual(keyboard.copy_calls, 0)

    def test_text_only_matches_and_marks_degraded(self):
        clip = self._clipboard()
        clip.items = [(7, b"old")]          # something readable as text
        keyboard = FakeKeyboard(clip, "收到")
        result = read_input_by_clipboard("收到", keyboard=keyboard, clipboard=clip,
                                         policy="text_only")
        self.assertIs(result.verdict, Verdict.MATCH)
        self.assertTrue(result.reason.endswith("_text_only_restore"))

    def test_text_only_restores_the_old_text(self):
        clip = self._clipboard()
        clip.items = [(13, "原来的内容\x00".encode("utf-16-le"))]
        keyboard = FakeKeyboard(clip, "收到")
        read_input_by_clipboard("收到", keyboard=keyboard, clipboard=clip, policy="text_only")
        self.assertEqual(clip.read_text(), "原来的内容")

    def test_default_policy_is_text_only(self):
        clip = self._clipboard()
        keyboard = FakeKeyboard(clip, "收到")
        result = read_input_by_clipboard("收到", keyboard=keyboard, clipboard=clip)
        self.assertIs(result.verdict, Verdict.MATCH)
        self.assertTrue(result.reason.endswith("_text_only_restore"))

    def test_unknown_policy_is_refused(self):
        clip = FakeClipboard()
        result = read_input_by_clipboard("收到", keyboard=FakeKeyboard(clip, "收到"),
                                         clipboard=clip, policy="whatever")
        self.assertEqual(result.reason, "invalid_clipboard_policy")

if __name__ == "__main__":
    unittest.main()

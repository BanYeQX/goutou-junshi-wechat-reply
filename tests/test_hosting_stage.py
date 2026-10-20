"""Offline tests for staging: draft protection, paste, readback."""
import unittest

from hosting_stage import StageOutcome, commit_and_verify, stage_reply
from input_readback_clipboard import PortError

TEXT = 13


class FakeClipboard:
    def __init__(self, text=""):
        self.text = text
        self.writes = []

    def read_text(self):
        return self.text

    def write_text(self, text):
        self.text = text
        self.writes.append(text)


class FakeKeyboard:
    def __init__(self, clipboard, box, copy_works=True):
        self.clipboard = clipboard
        self.box = box              # what is "in the input box"
        self.copy_works = copy_works
        self.pasted = []

    def select_all(self):
        pass

    def copy(self):
        if not self.copy_works:
            return                  # clipboard unchanged
        if self.box:
            self.clipboard.write_text(self.box)
        # empty box: no clipboard write at all - that is the real behaviour

    def paste(self):
        self.box = self.clipboard.read_text()
        self.pasted.append(self.box)


def run(box, reply, saved="原来的剪贴板"):
    clipboard = FakeClipboard(saved)
    keyboard = FakeKeyboard(clipboard, box)
    return stage_reply(reply, keyboard=keyboard, clipboard=clipboard), clipboard, keyboard


class StageTests(unittest.TestCase):
    def test_empty_box_is_staged(self):
        result, clipboard, keyboard = run("", "在的")
        self.assertIs(result.outcome, StageOutcome.STAGED)
        self.assertEqual(keyboard.box, "在的")
        self.assertEqual(result.text, "在的")

    def test_existing_draft_is_never_overwritten(self):
        result, clipboard, keyboard = run("我自己写了一半", "在的")
        self.assertIs(result.outcome, StageOutcome.DRAFT_PRESENT)
        self.assertEqual(keyboard.box, "我自己写了一半")
        self.assertEqual(keyboard.pasted, [])
        self.assertEqual(clipboard.text, "原来的剪贴板")

    def test_non_text_draft_is_a_draft_not_a_probe_failure(self):
        # Live 14:05: an image sat in the box; Ctrl+C copied it, leaving no text.
        clipboard = FakeClipboard("原来的剪贴板")
        keyboard = FakeKeyboard(clipboard, "")
        keyboard.copy = lambda: setattr(clipboard, "text", None)
        result = stage_reply("在的", keyboard=keyboard, clipboard=clipboard)
        self.assertIs(result.outcome, StageOutcome.DRAFT_PRESENT)
        self.assertEqual(result.reason, "non_text_draft_in_input")
        self.assertEqual(keyboard.pasted, [])
        self.assertEqual(clipboard.text, "原来的剪贴板")

    def test_clipboard_is_restored_after_staging(self):
        _, clipboard, _ = run("", "在的")
        self.assertEqual(clipboard.text, "原来的剪贴板")

    def test_clipboard_is_restored_after_draft_refusal(self):
        _, clipboard, _ = run("草稿", "在的")
        self.assertEqual(clipboard.text, "原来的剪贴板")

    def test_paste_that_does_not_land_is_never_staged(self):
        clipboard = FakeClipboard("原来的剪贴板")
        keyboard = FakeKeyboard(clipboard, "")
        keyboard.paste = lambda: None        # the paste silently does nothing
        result = stage_reply("在的", keyboard=keyboard, clipboard=clipboard)
        # An empty box after the paste means the copy had nothing to read; the
        # only honest answer is "could not confirm", never STAGED.
        self.assertIs(result.outcome, StageOutcome.PROBE_FAILED)
        self.assertEqual(result.reason, "copy_did_not_run")

    def test_partial_paste_is_a_mismatch(self):
        clipboard = FakeClipboard("原来的剪贴板")
        keyboard = FakeKeyboard(clipboard, "")

        def half_paste():
            keyboard.box = "在的"[:-1]
        keyboard.paste = half_paste
        result = stage_reply("在的", keyboard=keyboard, clipboard=clipboard)
        self.assertIs(result.outcome, StageOutcome.READBACK_MISMATCH)
        self.assertEqual(result.text, "在")

    def test_copy_that_does_not_work_leaves_the_sentinel(self):
        clipboard = FakeClipboard("x")
        keyboard = FakeKeyboard(clipboard, "", copy_works=False)
        result = stage_reply("在的", keyboard=keyboard, clipboard=clipboard)
        self.assertIs(result.outcome, StageOutcome.PROBE_FAILED)
        self.assertEqual(result.reason, "copy_did_not_run")

    def test_clipboard_error_is_reported(self):
        class Broken(FakeClipboard):
            def write_text(self, text):
                raise PortError("clipboard_busy")
        clipboard = Broken("x")
        keyboard = FakeKeyboard(clipboard, "")
        result = stage_reply("在的", keyboard=keyboard, clipboard=clipboard)
        self.assertIs(result.outcome, StageOutcome.PROBE_FAILED)
        self.assertEqual(result.reason, "clipboard_busy")

    def test_lost_focus_aborts_before_any_key(self):
        clipboard = FakeClipboard("原来的剪贴板")
        keyboard = FakeKeyboard(clipboard, "")
        result = stage_reply("在的", keyboard=keyboard, clipboard=clipboard,
                             guard=lambda: False)
        self.assertIs(result.outcome, StageOutcome.FOCUS_LOST)
        self.assertEqual(keyboard.pasted, [])

    def test_focus_lost_after_probe_restores_the_clipboard(self):
        clipboard = FakeClipboard("原来的剪贴板")
        keyboard = FakeKeyboard(clipboard, "")
        calls = {"n": 0}

        def guard():
            calls["n"] += 1
            return calls["n"] == 1          # pass once, then fail

        result = stage_reply("在的", keyboard=keyboard, clipboard=clipboard, guard=guard)
        self.assertIs(result.outcome, StageOutcome.FOCUS_LOST)
        self.assertEqual(keyboard.pasted, [])
        self.assertEqual(clipboard.text, "原来的剪贴板")

    def test_empty_reply_is_refused(self):
        result, _, _ = run("", "")
        self.assertIs(result.outcome, StageOutcome.PROBE_FAILED)
        self.assertEqual(result.reason, "empty_reply")


class CommitTests(unittest.TestCase):
    def _ledger(self):
        import os, tempfile
        from hosting_ledger import Ledger
        directory = tempfile.mkdtemp(prefix="commit-test-")
        self.addCleanup(lambda: [os.remove(os.path.join(directory, n))
                                 for n in os.listdir(directory)] and None)
        return Ledger(os.path.join(directory, "ledger.jsonl"))

    def test_ledger_begin_happens_before_enter(self):
        events = []

        class LedgerSpy:
            def begin_commit(self, target, text):
                events.append(("begin", target, text))
                return "cid"

            def finish_commit(self, cid, delivered, note=""):
                events.append(("finish", cid, delivered))

        keyboard = FakeKeyboard(FakeClipboard(), "")

        def send_enter():
            events.append(("enter",))

        keyboard.send_enter = send_enter
        delivered, cid = commit_and_verify(reply="在的", target="小雨",
                                           ledger=LedgerSpy(), keyboard=keyboard,
                                           verify=lambda: True)
        self.assertTrue(delivered)
        self.assertEqual(cid, "cid")
        self.assertEqual([e[0] for e in events], ["begin", "enter", "finish"])

    def test_unconfirmed_delivery_is_recorded_as_failure(self):
        ledger = self._ledger()
        keyboard = FakeKeyboard(FakeClipboard(), "")
        keyboard.send_enter = lambda: None
        delivered, cid = commit_and_verify(reply="在的", target="小雨", ledger=ledger,
                                           keyboard=keyboard, verify=lambda: False)
        self.assertFalse(delivered)
        entry = ledger.read()[-1]
        self.assertFalse(entry.delivered)
        self.assertEqual(entry.note, "not_confirmed_after_enter")

    def test_confirmed_delivery_is_recorded(self):
        ledger = self._ledger()
        keyboard = FakeKeyboard(FakeClipboard(), "")
        keyboard.send_enter = lambda: None
        delivered, _ = commit_and_verify(reply="在的", target="小雨", ledger=ledger,
                                         keyboard=keyboard, verify=lambda: True)
        self.assertTrue(delivered)
        self.assertTrue(ledger.read()[-1].delivered)
        self.assertFalse(ledger.blocks_start())

    def test_enter_is_pressed_exactly_once(self):
        calls = []

        class Keyboard:
            def send_enter(self):
                calls.append(1)

        class LedgerSpy:
            def begin_commit(self, target, text):
                return "cid"

            def finish_commit(self, cid, delivered, note=""):
                pass

        commit_and_verify(reply="x", target="小雨", ledger=LedgerSpy(),
                          keyboard=Keyboard(), verify=lambda: True)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

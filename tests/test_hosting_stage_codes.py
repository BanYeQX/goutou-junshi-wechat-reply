"""A failed stage must say why in the window, and must never reach Enter.

Live 14:05: an image was already sitting in the input box. The stage correctly
refused without pasting, but the per-attempt reasons were printed to a console
the GUI does not have, and the wording blamed the clipboard. Reproduced 14:15
with tools/paste_repro.py. All offline here: fake frames and stage.
"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from hosting_ledger import Ledger
import hosting_run
from hosting_session import Reading, Session


class StageReasonCodeTests(unittest.TestCase):
    def run_with(self, outcome, reason, codes):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            ledger = Ledger(str(Path(folder) / "ledger.jsonl"))
            now = [0.0]
            events = []
            enters = []
            frames = {i: Image.new("RGBA", (50, 50), (i, i, i, 255)) for i in range(2)}

            def read(frame, changed):
                if not changed:
                    return Reading()
                return {0: Reading(text="旧消息", incoming=True),
                        1: Reading(text="我去洗澡了", incoming=True)}[frame.getpixel((0, 0))[0]]

            def stage(reply, *_, notes=None, **__):
                notes.extend(codes)
                return SimpleNamespace(outcome=SimpleNamespace(value=outcome),
                                       reason=reason, text=None), "not_staged"

            def advance(seconds):
                now[0] += seconds
                if now[0] > 10:
                    raise AssertionError("deadline exceeded")

            def log(message, **_):
                if message.startswith("@hosting "):
                    events.append(json.loads(message[len("@hosting "):]))

            stop = SimpleNamespace(is_set=lambda: False, wait=advance)
            calibration = {"title": "离线夹具", "window_rect": (0, 0, 50, 50),
                           "messages_frame": (0, 0, 50, 30), "input_frame": (0, 30, 50, 50)}
            with patch.object(hosting_run, "build_adapters",
                              return_value=(lambda: frames[0 if now[0] < 0.5 else 1], read, Mock(),
                                            lambda: [])), \
                    patch.object(hosting_run, "build_model", return_value=lambda *_: "行，去吧。"), \
                    patch.object(hosting_run, "Session",
                                 side_effect=lambda **kw: Session(**kw, clock=lambda: now[0])), \
                    patch.object(hosting_run, "stage_reply_real", side_effect=stage), \
                    patch.object(hosting_run.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(hosting_run.time, "sleep", side_effect=advance), \
                    patch("input_readback_clipboard.NativeKeyboard",
                          return_value=SimpleNamespace(send_enter=lambda: enters.append(1))):
                hosting_run.run_hosting(calibration, ledger, live=True, dry_run=False,
                                        seconds=10, log=log, stop_event=stop)

            paused = [e["text"] for e in events if e.get("state") == "已暂停"]
            self.assertEqual(len(paused), 1)
            self.assertEqual(enters, [])
            self.assertEqual(ledger.read(), [])
            return paused[0]

    def test_pause_text_carries_each_attempt_reason_and_nothing_is_committed(self):
        text = self.run_with("PROBE_FAILED", "copy_did_not_run",
                             ["1:PROBE_FAILED/copy_did_not_run", "2:PROBE_FAILED/copy_did_not_run"])
        self.assertIn("未通过暂存", text)
        self.assertIn("1:PROBE_FAILED/copy_did_not_run，2:PROBE_FAILED/copy_did_not_run", text)

    def test_draft_in_box_says_draft_protection_not_clipboard_failure(self):
        text = self.run_with("DRAFT_PRESENT", "non_text_draft_in_input",
                             ["1:DRAFT_PRESENT/non_text_draft_in_input"])
        self.assertIn("草稿保护生效", text)
        self.assertIn("未粘贴、未提交", text)
        self.assertIn("1:DRAFT_PRESENT/non_text_draft_in_input", text)


if __name__ == "__main__":
    unittest.main()

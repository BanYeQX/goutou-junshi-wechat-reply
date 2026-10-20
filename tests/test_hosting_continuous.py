"""Two complete rounds through the production runner/session/loop, all offline.

Only pixels/readings, model, keyboard, clock and adapter are fake. The actual
session, decision loop, pre-Enter ledger and verification loop are exercised.
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


class ContinuousTests(unittest.TestCase):
    def run_scenario(self, second_error=None):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            ledger = Ledger(str(Path(folder) / "ledger.jsonl"))
            now = [0.0]
            stopped = [False]
            sent = []
            models = []
            events = []
            frames = {i: Image.new("RGBA", (50, 50), (i, i, i, 255)) for i in range(5)}
            # Frames 1 and 3 are what the window shows right before Enter: the
            # pasted draft sits in the input box. After Enter (2, 4) it is gone.
            for i in (1, 3):
                frames[i].paste((255, 255, 255, 255), (5, 35, 25, 45))

            def visual():
                if len(sent) == 2:
                    return 4
                if len(sent) == 1:
                    return 2 if now[0] < 4 else 3
                return 0 if now[0] < 0.5 else 1

            def read(frame, changed):
                if not changed:
                    return Reading()
                value = frame.getpixel((0, 0))[0]
                return {
                    0: Reading(text="启动前旧消息", incoming=True),
                    1: Reading(text="第一句", incoming=True),
                    2: Reading(text="回复一", incoming=False),
                    3: Reading(text="第二句", incoming=True),
                    4: Reading(text="回复二", incoming=False),
                }[value]

            def generate(text, _context):
                models.append(text)
                if text == "第二句" and second_error is not None:
                    raise second_error
                return "回复一" if text == "第一句" else "回复二"

            def advance(seconds):
                now[0] += seconds
                if now[0] > 15:
                    raise AssertionError("synthetic two-round deadline exceeded")

            stop = SimpleNamespace(is_set=lambda: stopped[0], wait=advance)

            def log(message, **_):
                if message.startswith("@hosting "):
                    event = json.loads(message[len("@hosting "):])
                    events.append(event)
                    if event["kind"] == "verification" and len(sent) == 2:
                        stopped[0] = True

            def stage(reply, *_, **__):
                return SimpleNamespace(outcome=SimpleNamespace(value="STAGED"), reason="match", text=reply), ""

            def enter():
                # The actual production commit function must already have persisted BEGIN.
                pending = ledger.unfinished()
                self.assertEqual(len(pending), 1)
                sent.append(pending[0].text)

            adapter = Mock()
            calibration = {"title": "离线夹具", "window_rect": (0, 0, 50, 50), "messages_frame": (0, 0, 50, 30), "input_frame": (0, 30, 50, 50)}
            with patch.object(hosting_run, "build_adapters", return_value=(lambda: frames[visual()], read, adapter, lambda: [])), \
                    patch.object(hosting_run, "build_model", return_value=generate), \
                    patch.object(hosting_run, "Session", side_effect=lambda **kw: Session(**kw, clock=lambda: now[0])), \
                    patch.object(hosting_run, "stage_reply_real", side_effect=stage), \
                    patch.object(hosting_run.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(hosting_run.time, "sleep", side_effect=advance), \
                    patch("input_readback_clipboard.NativeKeyboard", return_value=SimpleNamespace(send_enter=enter)):
                code = hosting_run.run_hosting(calibration, ledger, live=True, dry_run=False,
                                               seconds=15, log=log, stop_event=stop)
            self.assertEqual(code, 0)
            self.assertEqual(models, ["第一句", "第二句"])
            self.assertEqual(sent, ["回复一"] if second_error else ["回复一", "回复二"])
            self.assertEqual([e["text"] for e in events if e["kind"] == "incoming"], ["第一句", "第二句"])
            self.assertEqual([e["delivered"] for e in events if e["kind"] == "verification"], [True] if second_error else [True, True])
            self.assertFalse(ledger.blocks_start())
            self.assertEqual([e.kind for e in ledger.read()], ["commit_begin", "commit_finish"] * (1 if second_error else 2))
            adapter.close.assert_called_once()
            return events

    def test_two_incoming_messages_produce_two_verified_replies(self):
        self.run_scenario()

    def test_second_model_failure_pauses_once_without_retry_or_second_send(self):
        events = self.run_scenario(ValueError("模型回复为空或过长，本条未发送。"))
        failures = [e for e in events if e.get("state") == "已暂停"]
        self.assertEqual(len(failures), 1)
        self.assertIn("model_reply_limits", failures[0]["text"])


if __name__ == "__main__":
    unittest.main()

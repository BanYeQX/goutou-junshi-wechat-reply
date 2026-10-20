"""A model "wait" must be asked once per message, through the real runner.

Live 15:3x the model answered "wait" for "okok" and was asked again on every
frame - eight calls in four seconds - and earlier gave in on "嗯" after three.
All offline: fake frames, model and adapter; real Session, Loop, run_hosting.
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


class ModelWaitRunnerTests(unittest.TestCase):
    def test_wait_is_asked_once_and_nothing_is_staged(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            ledger = Ledger(str(Path(folder) / "ledger.jsonl"))
            now = [0.0]
            asked = []
            staged = []
            events = []
            frames = {i: Image.new("RGBA", (50, 50), (i, i, i, 255)) for i in range(2)}

            def read(frame, changed):
                if not changed:
                    return Reading()
                return {0: Reading(text="旧消息", incoming=True),
                        1: Reading(text="okok", incoming=True)}[frame.getpixel((0, 0))[0]]

            def model(text, _context):
                asked.append(text)
                if len(asked) > 5:
                    # The regression re-asks on every pass without pacing; fail
                    # (the runner pauses on a model error) instead of hanging.
                    raise RuntimeError("asked again for the same message")
                return SimpleNamespace(action="wait", text="", reason="nothing_to_add")

            def advance(seconds):
                now[0] += seconds
                if now[0] > 12:
                    raise AssertionError("deadline exceeded")

            def log(message, **_):
                if message.startswith("@hosting "):
                    events.append(json.loads(message[len("@hosting "):]))

            stop = SimpleNamespace(is_set=lambda: now[0] > 10, wait=advance)
            calibration = {"title": "离线夹具", "window_rect": (0, 0, 50, 50),
                           "messages_frame": (0, 0, 50, 30), "input_frame": (0, 30, 50, 50)}
            with patch.object(hosting_run, "build_adapters",
                              return_value=(lambda: frames[0 if now[0] < 0.5 else 1], read, Mock(),
                                            lambda: [])), \
                    patch.object(hosting_run, "build_model", return_value=model), \
                    patch.object(hosting_run, "Session",
                                 side_effect=lambda **kw: Session(**kw, clock=lambda: now[0])), \
                    patch.object(hosting_run, "stage_reply_real",
                                 side_effect=lambda *a, **k: staged.append(a) or (None, "x")), \
                    patch.object(hosting_run.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(hosting_run.time, "sleep", side_effect=advance):
                hosting_run.run_hosting(calibration, ledger, live=True, dry_run=False,
                                        seconds=10, log=log, stop_event=stop)

            self.assertEqual(asked, ["okok"])
            self.assertEqual(staged, [])
            self.assertEqual(ledger.read(), [])
            waits = [e for e in events if e.get("text") == "模型选择等待，本轮未发送"]
            self.assertEqual(len(waits), 1)


if __name__ == "__main__":
    unittest.main()

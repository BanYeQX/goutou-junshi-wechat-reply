"""Offline tests for the session glue, using synthetic frames."""
import unittest

from PIL import Image

from hosting_loop import Decision
from hosting_session import Geometry, Reading, Session, passes_geometry


def frame(width, height, value=0):
    return Image.new("RGB", (width, height), (value, value, value))


class ScriptedCapture:
    def __init__(self, frames, readings):
        self.frames = list(frames)
        self.readings = list(readings)
        self.index = 0

    def __call__(self):
        index = min(self.index, len(self.frames) - 1)
        return self.frames[index]

    def read(self, _frame, _changed=False):
        index = min(self.index, len(self.readings) - 1)
        self.index += 1
        return self.readings[index]


def session(frames, readings, geometry=None, clock=None):
    capture = ScriptedCapture(frames, readings)
    geo = geometry or Geometry((0, 0, 100, 100), (0, 0, 100, 100), 0.01)
    times = iter(clock or [0.0] * 50)
    return Session(capture=capture, read=capture.read, geometry=geo,
                   clock=lambda: next(times))


class GeometryTests(unittest.TestCase):
    def test_exact_rect_matches(self):
        self.assertTrue(passes_geometry((0, 0, 100, 100), (0, 0, 100, 100)))

    def test_one_pixel_off_does_not_match(self):
        self.assertFalse(passes_geometry((0, 0, 100, 101), (0, 0, 100, 100)))


class ChangeDetectionTests(unittest.TestCase):
    def test_first_frame_has_no_change(self):
        s = session([frame(200, 200, 0)], [Reading()])
        s.step()
        self.assertEqual(s._change(frame(200, 200, 0)), 0.0)

    def test_identical_frames_report_zero_change(self):
        s = session([frame(200, 200, 10), frame(200, 200, 10)], [Reading(), Reading()])
        s.step()
        decision, _ = s.step()
        self.assertIs(decision, Decision.WAIT)

    def test_resize_counts_as_change_not_as_message(self):
        s = session([frame(200, 200, 0), frame(300, 300, 0)],
                    [Reading(), Reading(text="在吗", incoming=True)])
        s.step()
        s.step()
        self.assertEqual(s.history[-1][0], Decision.WAIT)   # caught by geometry guard upstream


class FlowTests(unittest.TestCase):
    def test_new_message_reaches_generate(self):
        readings = [Reading(), Reading(), Reading(text="在吗", incoming=True),
                    Reading(), Reading()]
        times = [0.0, 1.0, 2.0, 2.5, 4.0]
        capture = ScriptedCapture([frame(200, 200, i) for i in range(5)], readings)
        geo = Geometry((0, 0, 200, 200), (0, 0, 200, 200), 0.0001)
        it = iter(times)
        s = Session(capture=capture, read=capture.read, geometry=geo, clock=lambda: next(it))

        s.step()
        s.step()
        s.step()
        s.step()
        decision, detail = s.step()
        self.assertIs(decision, Decision.GENERATE)
        self.assertEqual(detail, "在吗")

    def test_unreadable_newest_block_pauses_instead_of_reusing_an_old_message(self):
        readings = [Reading(), Reading(unreadable=True)]
        s = session([frame(200, 200, 0), frame(200, 200, 9)], readings)
        s.step()
        decision, detail = s.step()
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "unreadable_new_message")

    def test_geometry_failure_pauses(self):
        readings = [Reading(), Reading(geometry_ok=False)]
        s = session([frame(200, 200, 0), frame(200, 200, 1)], readings)
        s.step()
        decision, detail = s.step()
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "geometry_changed")

    def test_resume_rebuilds_the_baseline(self):
        s = session([frame(200, 200, 5)], [Reading(geometry_ok=False)])
        s.step()
        s.resume()
        self.assertIsNone(s._last)


if __name__ == "__main__":
    unittest.main()

"""Scroll detection: scrolled up, the bottom-most block is history.

Before this, the real reader never set `Reading.scrolling`, so scrolling up made
an old incoming message look new (and get answered) and an old own bubble look
like a takeover. All offline. Frames are `wechat_capture.PixelImage` (BGRA
bytes, what live capture returns) and go through the real `read_frame`,
`Session` and `Loop`; only OCR is faked, by colour.

What this does NOT prove: that the bottom padding WeChat leaves under the newest
bubble is the same for incoming and outgoing bubbles on the real client. That
needs a measurement on real frames.
"""
import unittest
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from bubble_box import bottom_gap
from hosting_loop import MERGE_QUIET_S, Decision, Loop, Observation, State
from hosting_run import TITLE_FRAME, read_frame
from hosting_session import BottomAnchor, Geometry, Session
from wechat_capture import PixelImage

SIZE = (800, 620)
MESSAGES = (40, 150, 790, 600)            # strip 750 x 450
STRIP_H = MESSAGES[3] - MESSAGES[1]
BACKGROUND = (30, 30, 31)
TITLE_COLOUR = (60, 60, 200)
PAD = 20                                   # blank rows under the newest bubble
BUBBLE_H, SPACING = 40, 25
CALIBRATION = {"title": "小雨", "window_rect": (0, 0, 800, 620), "messages_frame": MESSAGES}

# Each message has its own fill colour; the fake OCR reads the colour back.
COLOURS = {"X": (200, 80, 80), "Y": (80, 200, 80), "A": (200, 200, 80),
           "B": (80, 200, 200), "C": (200, 80, 200), "D": (220, 140, 60),
           "嗯": (150, 150, 230)}
# One-character bubbles: live 15:06 "嗯" was 54 px wide.
NARROW = {"嗯": 54}
TEXTS = {colour: name for name, colour in COLOURS.items()}
TEXTS[TITLE_COLOUR] = "小雨"


def pixel(image):
    rgba = image.convert("RGBA")
    return PixelImage(rgba.width, rgba.height, rgba.tobytes("raw", "BGRA"))


def chat(messages, offset=0):
    """messages: [(name, incoming)], oldest first, laid out from the bottom up.

    offset > 0 is "scrolled up by that many pixels": everything moves down and
    the newest bubbles slide under (or off) the bottom edge.
    """
    image = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle(TITLE_FRAME[:2] + (TITLE_FRAME[2] - 1, TITLE_FRAME[3] - 1), fill=TITLE_COLOUR)
    left, top = MESSAGES[0], MESSAGES[1]
    bottom = top + STRIP_H - 1 - PAD + offset
    for name, incoming in reversed(messages):
        x0, x1 = (left + 60, left + 300) if incoming else (left + 450, left + 720)
        if name in NARROW:
            x0, x1 = (x0, x0 + NARROW[name]) if incoming else (x1 - NARROW[name], x1)
        y0 = bottom - BUBBLE_H + 1
        visible_top, visible_bottom = max(y0, top), min(bottom, top + STRIP_H - 1)
        if visible_top <= visible_bottom:          # fully below the edge: not drawn
            draw.rectangle((x0, visible_top, x1, visible_bottom), fill=COLOURS[name])
        bottom = y0 - SPACING - 1
        if bottom < top:
            break
    # Nothing may spill outside the message area into the input box below it.
    draw.rectangle((0, MESSAGES[3], SIZE[0], SIZE[1]), fill=BACKGROUND)
    return pixel(image)


class FakeOcr:
    """Returns the name of the dominant non-background colour of the crop."""

    def __init__(self):
        self.calls = []

    def __call__(self, rgb):
        colours, counts = np.unique(rgb.reshape(-1, 3), axis=0, return_counts=True)
        best = None
        for colour, count in zip(map(tuple, colours), counts):
            if colour != BACKGROUND and (best is None or count > best[1]):
                best = (colour, count)
        text = TEXTS.get(best[0], "") if best else ""
        self.calls.append(text)
        return SimpleNamespace(txts=[text] if text else [])


HISTORY = [("X", True), ("Y", False), ("A", True), ("B", False)]


def reader(anchor=None):
    ocr = FakeOcr()
    anchor = anchor or BottomAnchor()

    def read(frame, changed):
        return read_frame(frame, changed, calibration=CALIBRATION, ocr=ocr,
                          window_rect=CALIBRATION["window_rect"], anchor=anchor)
    return read, ocr, anchor


class BottomGapTests(unittest.TestCase):
    def strip(self, frame):
        crop = frame.crop(MESSAGES)
        return bottom_gap(crop.width, crop.height, crop.tobytes())

    def test_padding_at_the_bottom_is_measured(self):
        self.assertEqual(self.strip(chat(HISTORY)), PAD)

    def test_a_bubble_cut_by_the_edge_leaves_no_gap(self):
        self.assertEqual(self.strip(chat(HISTORY, offset=30)), 0)

    def test_an_empty_strip_has_no_gap(self):
        self.assertIsNone(self.strip(chat([])))


class BottomAnchorTests(unittest.TestCase):
    def test_learns_from_the_first_frame_only(self):
        anchor = BottomAnchor()
        self.assertFalse(anchor.scrolled(20))
        self.assertFalse(anchor.scrolled(24))      # within tolerance
        self.assertTrue(anchor.scrolled(40))       # padding grew: not the bottom
        self.assertFalse(anchor.scrolled(20))      # never re-learnt from 40

    def test_calibrated_padding_wins_over_the_first_frame(self):
        anchor = BottomAnchor(20)
        self.assertTrue(anchor.scrolled(60))
        self.assertFalse(anchor.scrolled(21))

    def test_cut_bubble_or_no_ink_reads_as_scrolled(self):
        anchor = BottomAnchor(20)
        self.assertTrue(anchor.scrolled(0))
        self.assertTrue(anchor.scrolled(None))

    def test_unusable_first_frame_leaves_every_frame_scrolled(self):
        anchor = BottomAnchor()
        self.assertTrue(anchor.scrolled(None))
        self.assertTrue(anchor.scrolled(20))


class ReadFrameTests(unittest.TestCase):
    def test_at_the_bottom_the_newest_block_is_read_with_its_direction(self):
        read, _, _ = reader()
        reading = read(chat(HISTORY), True)
        self.assertFalse(reading.scrolling)
        self.assertEqual((reading.text, reading.incoming), ("B", False))
        self.assertEqual(list(reading.above()), ["A", "Y", "X"])

    def test_scrolled_up_nothing_but_the_title_is_ocrd(self):
        read, ocr, _ = reader()
        read(chat(HISTORY), True)                  # learn the bottom
        ocr.calls.clear()
        reading = read(chat(HISTORY, offset=30), True)
        self.assertTrue(reading.scrolling)
        self.assertIsNone(reading.text)
        self.assertEqual(ocr.calls, ["小雨"])

    def test_unchanged_frame_still_reports_scrolled(self):
        read, ocr, _ = reader()
        read(chat(HISTORY), True)
        ocr.calls.clear()
        self.assertTrue(read(chat(HISTORY, offset=30), False).scrolling)
        self.assertEqual(ocr.calls, [])


class LoopHoldTests(unittest.TestCase):
    def started(self):
        loop = Loop()
        loop.note_baseline("B", False)
        loop.observe(Observation(now=0.0))
        return loop

    def test_old_incoming_seen_while_scrolled_is_not_accepted(self):
        loop = self.started()
        decision, detail = loop.observe(Observation(now=1.0, change=0.3, text="A",
                                                    incoming=True, scrolling=True))
        self.assertEqual((decision, detail), (Decision.WAIT, "scrolled"))
        self.assertIsNone(loop.pending)
        self.assertEqual(loop.newest_text, "B")

    def test_old_own_bubble_seen_while_scrolled_is_not_a_takeover(self):
        loop = self.started()
        decision, _ = loop.observe(Observation(now=1.0, change=0.3, text="Y", incoming=False,
                                               outgoing=True, scrolling=True))
        self.assertIs(decision, Decision.WAIT)
        self.assertIs(loop.state, State.RUNNING)

    def test_a_newest_block_that_does_not_follow_the_known_one_is_held(self):
        loop = self.started()
        decision, detail = loop.observe(Observation(now=1.0, change=0.3, text="A",
                                                    incoming=True, above=["Y", "X"]))
        self.assertEqual((decision, detail), (Decision.WAIT, "not_continuous"))
        self.assertIsNone(loop.pending)

    def test_a_new_message_right_below_the_known_one_is_accepted(self):
        loop = self.started()
        loop.observe(Observation(now=1.0, change=0.3, text="C", incoming=True,
                                 above=["B", "A"]))
        decision, detail = loop.observe(Observation(now=1.0 + MERGE_QUIET_S))
        self.assertEqual((decision, detail), (Decision.GENERATE, "C"))

    def test_a_message_pending_before_the_scroll_is_answered_after_it(self):
        loop = self.started()
        loop.observe(Observation(now=1.0, change=0.3, text="C", incoming=True, above=["B"]))
        loop.observe(Observation(now=1.2, change=0.3, scrolling=True))
        decision, _ = loop.observe(Observation(now=5.0, scrolling=True))
        self.assertIs(decision, Decision.WAIT)      # never generated while scrolled
        decision, detail = loop.observe(Observation(now=6.0, change=0.3, text="C",
                                                    incoming=True, above=["B"]))
        self.assertEqual(detail, "newest_unchanged")
        decision, detail = loop.observe(Observation(now=6.5))
        self.assertEqual((decision, detail), (Decision.GENERATE, "C"))


class EndToEndScrollTests(unittest.TestCase):
    """Real read_frame + Session + Loop over PixelImage frames."""

    def run_frames(self, frames):
        read, ocr, _ = reader()
        now = [0.0]
        sequence = list(frames)
        loop = Loop()
        initial = read(sequence[0], True)
        self.assertFalse(initial.scrolling)
        loop.note_baseline(initial.text, initial.incoming, initial.above)   # as run_hosting does
        session = Session(capture=lambda: sequence.pop(0), read=read,
                          geometry=Geometry(CALIBRATION["window_rect"], MESSAGES),
                          loop=loop, clock=lambda: now[0])
        decisions = []
        while sequence:
            decisions.append(session.step())
            now[0] += 0.6
        return decisions, loop

    def test_scrolling_through_history_and_back_answers_nothing(self):
        decisions, loop = self.run_frames([
            chat(HISTORY),                 # baseline: newest is our own B
            chat(HISTORY, offset=30),      # scrolled: B cut by the edge
            chat(HISTORY, offset=65),      # scrolled exactly one message: A sits at the padding
            chat(HISTORY, offset=65),
            chat(HISTORY, offset=130),     # Y (ours) at the padding
            chat(HISTORY),                 # back at the bottom
            chat(HISTORY),
            chat(HISTORY),
        ])
        kinds = [d for d, _ in decisions]
        self.assertNotIn(Decision.GENERATE, kinds)
        self.assertNotIn(Decision.PAUSE, kinds)
        self.assertIn((Decision.WAIT, "scrolled"), decisions)
        self.assertIn((Decision.WAIT, "not_continuous"), decisions)
        self.assertEqual(loop.held, "")
        self.assertIs(loop.state, State.RUNNING)

    def test_a_new_message_after_scrolling_back_is_answered_once(self):
        decisions, _ = self.run_frames([
            chat(HISTORY),
            chat(HISTORY, offset=65),
            chat(HISTORY),
            chat(HISTORY + [("C", True)]),
            chat(HISTORY + [("C", True)]),
            chat(HISTORY + [("C", True)]),
        ])
        self.assertEqual([detail for d, detail in decisions if d is Decision.GENERATE], ["C"])

    def test_a_one_character_message_is_seen_and_answered(self):
        decisions, _ = self.run_frames([
            chat(HISTORY),
            chat(HISTORY + [("嗯", True)]),
            chat(HISTORY + [("嗯", True)]),
            chat(HISTORY + [("嗯", True)]),
        ])
        self.assertEqual([detail for d, detail in decisions if d is Decision.GENERATE], ["嗯"])

    def test_the_same_one_character_sent_twice_is_answered(self):
        # Live 15:2x: started with an unanswered "嗯" at the bottom; the contact
        # sent "嗯" again and nothing happened.
        start = HISTORY + [("嗯", True)]
        decisions, _ = self.run_frames([
            chat(start),
            chat(start),                       # a repaint: not a message
            chat(start + [("嗯", True)]),
            chat(start + [("嗯", True)]),
            chat(start + [("嗯", True)]),
        ])
        self.assertEqual([detail for d, detail in decisions if d is Decision.GENERATE], ["嗯"])

    def test_starting_while_scrolled_is_detected_on_the_first_frame(self):
        read, _, _ = reader()
        self.assertTrue(read(chat(HISTORY, offset=30), True).scrolling)


if __name__ == "__main__":
    unittest.main()

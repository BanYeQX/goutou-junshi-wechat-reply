"""Synthetic pixels only. Never touches a desktop or a captured window."""
import unittest

from bubble_box import is_incoming, newest_bubble_box, text_blocks

BG = (30, 30, 30, 255)
BUBBLE = (46, 46, 46, 255)
INK = (232, 232, 232, 255)
DIM = (120, 120, 120, 255)
AVATAR = (70, 90, 120, 255)


def canvas(width, height, colour=BG):
    return bytearray(bytes(colour) * (width * height))


def rect(data, width, left, top, right, bottom, colour):
    for y in range(top, bottom):
        base = y * width * 4
        for x in range(left, right):
            o = base + x * 4
            data[o:o + 4] = bytes(colour)


def bubble(data, width, left, top, right, bottom, ink=12):
    rect(data, width, left, top, right, bottom, BUBBLE)
    for i in range(ink):
        x = left + 8 + i * 3
        if x + 1 < right:
            rect(data, width, x, top + 10, x + 2, top + 16, INK)


class BubbleBoxTests(unittest.TestCase):
    def test_single_bubble_is_located(self):
        w, h = 300, 200
        data = canvas(w, h)
        bubble(data, w, 20, 100, 160, 150)
        left, top, right, bottom = newest_bubble_box(w, h, bytes(data))
        self.assertAlmostEqual(left, 20, delta=3)
        self.assertAlmostEqual(top, 100, delta=3)
        self.assertAlmostEqual(right, 160, delta=4)
        self.assertAlmostEqual(bottom, 150, delta=4)

    def test_wide_bubble_covering_most_rows_still_finds_background(self):
        # Bubbles cover >60% of the strip; a whole-strip colour mode would fail.
        w, h = 300, 200
        data = canvas(w, h)
        bubble(data, w, 10, 20, 290, 70, ink=40)
        bubble(data, w, 10, 80, 290, 130, ink=40)
        _l, top, _r, bottom = newest_bubble_box(w, h, bytes(data))
        self.assertAlmostEqual(top, 80, delta=3)
        self.assertAlmostEqual(bottom, 130, delta=4)

    def test_avatar_block_is_excluded_from_the_crop(self):
        w, h = 300, 200
        data = canvas(w, h)
        rect(data, w, 10, 100, 50, 140, AVATAR)    # 40 px avatar
        bubble(data, w, 60, 100, 220, 150)         # 10 px gap, wider bubble
        left, _top, right, _bottom = newest_bubble_box(w, h, bytes(data))
        self.assertGreaterEqual(left, 58)
        self.assertLessEqual(right, 222)

    def test_adjacent_bubbles_are_not_merged(self):
        w, h = 300, 320
        data = canvas(w, h)
        bubble(data, w, 20, 40, 160, 90)
        bubble(data, w, 20, 100, 130, 150)
        _l, top, _r, bottom = newest_bubble_box(w, h, bytes(data))
        self.assertAlmostEqual(top, 100, delta=3)
        self.assertAlmostEqual(bottom, 150, delta=4)

    def test_centred_time_label_is_rejected_by_height(self):
        w, h = 300, 220
        data = canvas(w, h)
        rect(data, w, 120, 170, 180, 194, DIM)     # 60 px wide, 24 px tall
        self.assertIsNone(newest_bubble_box(w, h, bytes(data)))

    def test_bubble_above_time_label_is_still_found(self):
        w, h = 300, 260
        data = canvas(w, h)
        bubble(data, w, 20, 60, 160, 110)
        rect(data, w, 120, 210, 180, 234, DIM)
        _l, top, _r, bottom = newest_bubble_box(w, h, bytes(data))
        self.assertAlmostEqual(top, 60, delta=3)
        self.assertAlmostEqual(bottom, 110, delta=4)

    def test_uniform_strip_reports_nothing(self):
        w, h = 300, 200
        self.assertIsNone(newest_bubble_box(w, h, bytes(canvas(w, h))))

    def test_bad_dimensions_are_refused(self):
        self.assertIsNone(newest_bubble_box(0, 10, b""))
        self.assertIsNone(newest_bubble_box(10, 10, b"\x00" * 10))
        with self.assertRaises(ValueError):
            newest_bubble_box(10, 10, bytes(canvas(10, 10)), step=0)

    def test_short_noise_band_is_ignored(self):
        w, h = 300, 200
        data = canvas(w, h)
        rect(data, w, 20, 190, 160, 194, BUBBLE)
        self.assertIsNone(newest_bubble_box(w, h, bytes(data)))

    def test_one_character_bubble_is_found(self):
        # Live 15:06: a one-character "嗯" bubble was 54 px wide and was dropped
        # by the old 60 px minimum, so the reader saw the bubble above it.
        w, h = 300, 260
        data = canvas(w, h)
        bubble(data, w, 20, 40, 160, 90)
        bubble(data, w, 20, 150, 74, 200, ink=6)    # 54 px wide
        left, top, right, _bottom = newest_bubble_box(w, h, bytes(data))
        self.assertAlmostEqual(top, 150, delta=3)
        self.assertLessEqual(right - left, 60)

    def test_every_block_is_returned_top_to_bottom(self):
        w, h = 300, 320
        data = canvas(w, h)
        bubble(data, w, 20, 40, 160, 90)
        bubble(data, w, 20, 130, 120, 180)
        boxes = text_blocks(w, h, bytes(data))
        self.assertEqual(len(boxes), 2)
        self.assertLess(boxes[0][1], boxes[1][1])
        self.assertEqual(boxes[-1], newest_bubble_box(w, h, bytes(data)))


class DirectionTests(unittest.TestCase):
    def test_block_hugging_left_margin_is_incoming(self):
        # Measured shape: x=145..209 inside a 988 px strip.
        self.assertTrue(is_incoming((145, 134, 209, 188), 988))

    def test_block_hugging_right_margin_is_outgoing(self):
        self.assertFalse(is_incoming((700, 134, 900, 188), 988))

    def test_centred_block_is_unknown(self):
        self.assertIsNone(is_incoming((420, 134, 560, 188), 988))

    def test_invalid_box_is_unknown(self):
        self.assertIsNone(is_incoming(None, 988))
        self.assertIsNone(is_incoming((200, 0, 100, 50), 988))
        self.assertIsNone(is_incoming((10, 0, 900, 50), 0))


if __name__ == "__main__":
    unittest.main()

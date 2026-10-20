"""The title crop follows WeChat's list/chat divider. PixelImage frames, offline.

Live 2026-10-20 20:2x: the divider sat at x=401, the fixed crop from x=434 cut
the left half of the first character off and OCR read the title wrong, so
hosting refused to start. The crop must start at the chat pane and must never
reach into the contact list - its search box could otherwise supply the name.
"""
import unittest

from PIL import Image, ImageDraw

from hosting_run import TITLE_FRAME, title_box
from wechat_capture import PixelImage

LIST, BAR, INK, SEARCH = (59, 59, 61), (30, 30, 31), (230, 230, 230), (75, 75, 78)


def window(divider, *, name_at=None, name_width=42, search_text=False, width=2000, height=300):
    image = Image.new("RGB", (width, height), BAR)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, divider - 1, height), fill=LIST)
    if search_text:
        draw.rectangle((20, 70, divider - 60, 110), fill=SEARCH)
        draw.rectangle((40, 80, 100, 100), fill=INK)          # text typed into the search box
    if name_at is not None:
        draw.rectangle((name_at, 82, name_at + name_width, 108), fill=INK)
    rgba = image.convert("RGBA")
    return PixelImage(rgba.width, rgba.height, rgba.tobytes("raw", "BGRA"))


class TitleBoxTests(unittest.TestCase):
    def test_divider_moved_left_the_crop_starts_at_it(self):
        frame = window(401, name_at=425, search_text=True)
        left, top, right, bottom = title_box(frame)
        self.assertEqual(left, 401)
        self.assertEqual((top, bottom), TITLE_FRAME[1::2])
        self.assertLess(left, 425)                            # the whole name is inside

    def test_the_list_and_its_search_box_never_enter_the_crop(self):
        for divider in (300, 401, 470, 560):
            with self.subTest(divider=divider):
                frame = window(divider, name_at=divider + 24, search_text=True)
                self.assertGreaterEqual(title_box(frame)[0], divider)

    def test_the_end_of_the_name_is_not_taken_for_the_divider(self):
        frame = window(401, name_at=425, name_width=42)
        self.assertEqual(title_box(frame)[0], 401)

    def test_long_names_keep_at_least_the_usual_width(self):
        left, _, right, _ = title_box(window(401, name_at=425))
        self.assertGreaterEqual(right, TITLE_FRAME[2])
        self.assertGreaterEqual(right - left, 200)

    def test_no_divider_falls_back_to_the_fixed_frame(self):
        uniform = Image.new("RGBA", (2000, 300), BAR + (255,))
        frame = PixelImage(2000, 300, uniform.tobytes("raw", "BGRA"))
        self.assertEqual(title_box(frame), TITLE_FRAME)

    def test_a_busy_title_bar_falls_back_too(self):
        image = Image.new("RGB", (2000, 300), BAR)
        draw = ImageDraw.Draw(image)
        for x in range(900, 1500, 7):                         # stripes where the bar is sampled
            draw.line((x, 0, x, 300), fill=INK)
        rgba = image.convert("RGBA")
        self.assertEqual(title_box(PixelImage(2000, 300, rgba.tobytes("raw", "BGRA"))), TITLE_FRAME)


if __name__ == "__main__":
    unittest.main()

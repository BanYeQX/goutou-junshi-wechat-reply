"""从微信读取 for the 军师 tab: pure read_from_frame over PixelImage frames.

No window, no capture: frames and the colour-keyed fake OCR come from the
scroll tests. The live wrapper (read_visible_chat) is only a thin capture.
"""
import unittest

from advisor import Turn
from chat_reader import ReadError, read_from_frame
from test_hosting_scroll import CALIBRATION, HISTORY, FakeOcr, chat


def read(frame, title="小雨", window_rect=None):
    return read_from_frame(frame, expected_title=title, calibration=CALIBRATION, ocr=FakeOcr(),
                           window_rect=window_rect or CALIBRATION["window_rect"])


class ReadFromFrameTests(unittest.TestCase):
    def test_visible_messages_become_me_and_other_lines(self):
        self.assertEqual(read(chat(HISTORY)),
                         [Turn("other", "X"), Turn("me", "Y"), Turn("other", "A"), Turn("me", "B")])

    def test_another_contact_open_in_wechat_is_refused(self):
        with self.assertRaises(ReadError) as caught:
            read(chat(HISTORY), title="阿南")
        self.assertIn("不是「阿南」", str(caught.exception))
        self.assertIn("小雨", str(caught.exception))       # says what it saw instead

    def test_a_moved_or_restored_window_is_refused(self):
        with self.assertRaises(ReadError) as caught:
            read(chat(HISTORY), window_rect=(0, 0, 640, 480))
        self.assertIn("最大化", str(caught.exception))

    def test_no_contact_selected(self):
        with self.assertRaises(ReadError):
            read(chat(HISTORY), title="")

    def test_nothing_readable_on_screen(self):
        with self.assertRaises(ReadError) as caught:
            read(chat([]))
        self.assertIn("没有识别到文字", str(caught.exception))

    def test_only_the_last_messages_are_read(self):
        many = [(name, i % 2 == 0) for i, name in enumerate(["X", "Y", "A", "B", "C", "D"])]
        turns = read_from_frame(chat(many), expected_title="小雨", calibration=CALIBRATION,
                                ocr=FakeOcr(), window_rect=CALIBRATION["window_rect"], limit=2)
        self.assertEqual([t.text for t in turns], ["B", "C", "D"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the delivery-echo comparison.

Delivery is verified by OCR-ing our own bubble back. That read can differ from
what we pasted by a punctuation mark; requiring byte equality used to turn a
successful send into an UNKNOWN freeze.
"""
import unittest

from hosting_run import _same_reply


class DeliveryComparisonTests(unittest.TestCase):
    def test_identical_text_matches(self):
        self.assertTrue(_same_reply("收到", "收到"))

    def test_extra_punctuation_still_counts_as_delivered(self):
        self.assertTrue(_same_reply("可以啊，记得早点休息。", "可以啊，记得早点休息"))

    def test_dropped_space_still_counts_as_delivered(self):
        self.assertTrue(_same_reply("那 你自己吃吧", "那你自己吃吧"))

    def test_different_text_is_not_delivered(self):
        self.assertFalse(_same_reply("在的", "收到"))

    def test_empty_read_is_not_delivered(self):
        self.assertFalse(_same_reply("", "收到"))
        self.assertFalse(_same_reply(None, "收到"))

    def test_empty_reply_is_never_confirmed(self):
        self.assertFalse(_same_reply("收到", ""))


if __name__ == "__main__":
    unittest.main()

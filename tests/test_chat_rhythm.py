"""K-line from a chat CSV: the jev-chat format and algorithm, offline."""
import tempfile
import unittest
from pathlib import Path

import chat_rhythm
from chat_rhythm import Candle, RhythmError, parse_csv_text

HEAD = "timestamp,sender,message\n"


class AlgorithmTests(unittest.TestCase):
    def test_single_message_from_the_contact(self):
        (c,) = parse_csv_text(HEAD + "2026-10-20 14:00,other,嗯\n")
        self.assertEqual((c.open, c.high, c.low, c.close), (0, 1, 0, 1))

    def test_single_message_from_me(self):
        (c,) = parse_csv_text(HEAD + "2026-10-20 14:00,me,在吗\n")
        self.assertEqual((c.open, c.high, c.low, c.close), (0, 0, -1, -1))

    def test_balance_carries_across_days_and_counts_are_kept(self):
        series = parse_csv_text(HEAD + "2026-10-19T09:00,other,a\n2026-10-19T09:01,other,b\n"
                                       "2026-10-19T09:02,me,c\n2026-10-20T10:00,me,d\n2026-10-20T10:01,me,e\n")
        self.assertEqual(series, (Candle("2026-10-19", 0, 2, 0, 1, mine=1, theirs=2),
                                  Candle("2026-10-20", 1, 1, -1, -1, mine=2, theirs=0)))

    def test_days_without_messages_have_no_candle(self):
        series = parse_csv_text(HEAD + "2026-10-01 09:00,me,a\n2026-10-05 09:00,other,b\n")
        self.assertEqual([c.day for c in series], ["2026-10-01", "2026-10-05"])

    def test_equal_timestamps_extra_columns_and_bom_are_fine(self):
        text = "﻿id,timestamp,sender,message\n1,2026-10-20 14:00,me,a\n2,2026-10-20 14:00,other,\"b,c\"\n"
        self.assertEqual(len(parse_csv_text(text.lstrip("﻿"))), 1)

    def test_timezones_only_the_day_matters(self):
        series = parse_csv_text(HEAD + "2026-10-20T09:00+09:00,me,a\n2026-10-20T10:00,other,b\n")
        self.assertEqual(len(series), 1)


class ErrorTests(unittest.TestCase):
    def check(self, text, fragment):
        with self.assertRaises(RhythmError) as caught:
            parse_csv_text(text)
        self.assertIn(fragment, str(caught.exception))

    def test_missing_header(self):
        self.check("time,who,text\n2026-10-20,me,a\n", "三列表头")

    def test_header_only(self):
        self.check(HEAD, "没有聊天记录")

    def test_bad_sender_or_empty_message(self):
        self.check(HEAD + "2026-10-20 14:00,her,a\n", "第 2 行")
        self.check(HEAD + "2026-10-20 14:00,me,  \n", "第 2 行")

    def test_bad_timestamp_gets_a_friendly_message(self):
        self.check(HEAD + "昨天,me,a\n", "时间无法识别")

    def test_out_of_order(self):
        self.check(HEAD + "2026-10-20 14:00,me,a\n2026-10-19 14:00,other,b\n", "第 3 行时间比上一行早")


class FileTests(unittest.TestCase):
    def write(self, folder, name, data):
        path = Path(folder) / name
        path.write_bytes(data)
        return path

    def test_utf8_with_bom_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, "chat.csv", ("﻿" + HEAD + "2026-10-20 14:00,other,嗯\n").encode("utf-8"))
            self.assertEqual(len(chat_rhythm.load_csv(path)), 1)

    def test_wrong_extension_size_and_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RhythmError):
                chat_rhythm.load_csv(self.write(folder, "chat.txt", HEAD.encode()))
            with self.assertRaises(RhythmError):
                chat_rhythm.load_csv(self.write(folder, "gbk.csv", (HEAD + "2026-10-20,me,嗯\n").encode("gbk")))
            big = self.write(folder, "big.csv", b"x" * (chat_rhythm.MAX_BYTES + 1))
            with self.assertRaises(RhythmError):
                chat_rhythm.load_csv(big)


class DemoTests(unittest.TestCase):
    def test_every_demo_is_computed_and_skips_empty_days(self):
        for name, days in chat_rhythm.DEMOS.items():
            with self.subTest(name=name):
                series = chat_rhythm.demo(name)
                self.assertEqual(len(series), sum(1 for d in days if d))
                self.assertEqual(series[0].open, 0)
                for previous, current in zip(series, series[1:]):
                    self.assertEqual(current.open, previous.close)

    def test_unknown_demo(self):
        with self.assertRaises(RhythmError):
            chat_rhythm.demo("不存在")


if __name__ == "__main__":
    unittest.main()

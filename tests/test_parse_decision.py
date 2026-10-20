"""Tolerant JSON parsing for model replies.

A model answer that is valid JSON but wrapped in a bare code fence, or preceded by
one sentence, used to be reported as "format error" and stopped a live session.
"""
import unittest

from chat_core import parse_decision

BODY = '{"action":"reply","text":"晚上吃面","reason":"给个具体建议"}'


class ParseDecisionTests(unittest.TestCase):
    def test_plain_json(self):
        decision = parse_decision(BODY)
        self.assertEqual(decision.action, "reply")
        self.assertEqual(decision.text, "晚上吃面")

    def test_json_fenced_block(self):
        self.assertEqual(parse_decision("```json\n" + BODY + "\n```").text, "晚上吃面")

    def test_bare_fenced_block(self):
        self.assertEqual(parse_decision("```\n" + BODY + "\n```").text, "晚上吃面")

    def test_leading_prose(self):
        self.assertEqual(parse_decision("好的，这是回复：" + BODY).text, "晚上吃面")

    def test_trailing_prose(self):
        self.assertEqual(parse_decision(BODY + " 希望有帮助").text, "晚上吃面")

    def test_braces_inside_the_text_field(self):
        raw = '{"action":"reply","text":"用 {} 表示空集合","reason":"解释"}'
        self.assertEqual(parse_decision(raw).text, "用 {} 表示空集合")

    def test_wait_action_has_empty_text(self):
        decision = parse_decision('{"action":"wait","text":"随便","reason":"不该接"}')
        self.assertEqual(decision.action, "wait")
        self.assertEqual(decision.text, "")

    def test_truly_broken_output_still_fails(self):
        with self.assertRaises(ValueError):
            parse_decision("模型今天不想说话")

    def test_invalid_action_still_fails(self):
        with self.assertRaises(ValueError):
            parse_decision('{"action":"sing","text":"啊","reason":"x"}')


if __name__ == "__main__":
    unittest.main()

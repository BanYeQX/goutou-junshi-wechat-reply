"""Offline tests for folding the on-screen conversation into the model request.

DeepSeek's JSON mode returns whitespace once the request contains assistant-role
turns. Folding everything into one user message is what makes it answer again.
"""
import unittest

from hosting_run import fold_context


class FoldContextTests(unittest.TestCase):
    def test_empty_context_is_empty(self):
        self.assertEqual(fold_context(None), [])
        self.assertEqual(fold_context([]), [])

    def test_everything_becomes_a_single_user_message(self):
        context = [
            {"text": "在干嘛", "incoming": False},
            {"text": "刚下班", "incoming": True},
        ]
        folded = fold_context(context)
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["role"], "user")

    def test_speakers_are_labelled(self):
        folded = fold_context([
            {"text": "在干嘛", "incoming": False},
            {"text": "刚下班", "incoming": True},
        ])[0]["content"]
        self.assertIn("我：在干嘛", folded)
        self.assertIn("对方：刚下班", folded)

    def test_order_is_preserved(self):
        folded = fold_context([
            {"text": "一", "incoming": True},
            {"text": "二", "incoming": False},
            {"text": "三", "incoming": True},
        ])[0]["content"]
        self.assertLess(folded.index("对方：一"), folded.index("我：二"))
        self.assertLess(folded.index("我：二"), folded.index("对方：三"))

    def test_blank_texts_are_skipped(self):
        folded = fold_context([
            {"text": "", "incoming": True},
            {"text": "在吗", "incoming": True},
        ])[0]["content"]
        self.assertIn("对方：在吗", folded)
        self.assertEqual(folded.count("对方："), 1)

    def test_context_is_capped(self):
        context = [{"text": f"第{i}条", "incoming": True} for i in range(60)]
        folded = fold_context(context, limit=10)[0]["content"]
        self.assertIn("第59条", folded)
        self.assertNotIn("第0条", folded)

    def test_long_lines_are_truncated(self):
        folded = fold_context([{"text": "长" * 5000, "incoming": True}])[0]["content"]
        self.assertLess(len(folded), 3000)


if __name__ == "__main__":
    unittest.main()

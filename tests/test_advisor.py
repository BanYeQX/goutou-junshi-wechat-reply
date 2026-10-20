"""Offline tests for the 军师 logic. The model is a fake; nothing is sent."""
import json
import unittest

import advisor
from advisor import Turn
from hosting_window import ChatEntry

PROFILE = {"id": "p1", "name": "小雨", "stage": "刚认识", "goal": "自然聊天，互相了解", "context": "同学介绍"}
CFG = {"my_facts": "在上班", "my_style": "短句，少用句号", "profiles": [PROFILE], "selected": "p1"}


def answer(**override):
    value = {"strategy": "承接", "intent": "可能想听你夸她", "confidence": 0.55,
             "basis": "对方：我搞了", "facts": ["她说做完了"], "unknowns": ["做的是什么"],
             "advice": "顺着认可接一句", "stop_when": "她转开话题就别追问",
             "candidates": [{"text": "可以啊，这么快", "why": "接住", "cost": "略平淡"},
                            {"text": "牛", "why": "很短", "cost": "可能显得敷衍"},
                            {"text": "做的什么？给我看看", "why": "好奇", "cost": "可能让她有压力"}]}
    value.update(override)
    return json.dumps(value, ensure_ascii=False)


class FakeAsk:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, instructions, content, *, schema, name):
        self.calls.append((instructions, content, schema, name))
        return self.answers.pop(0)


class TranscriptTests(unittest.TestCase):
    def test_unlabelled_lines_are_the_contact(self):
        turns = advisor.parse_transcript("我搞了\n\n吊不吊")
        self.assertEqual(turns, [Turn("other", "我搞了"), Turn("other", "吊不吊")])

    def test_labels_mark_who_spoke(self):
        turns = advisor.parse_transcript("我：在吗\n对方：在\n她: 干嘛\nme: nothing")
        self.assertEqual([t.speaker for t in turns], ["me", "other", "other", "me"])
        self.assertEqual(turns[2].text, "干嘛")

    def test_only_the_last_thirty_turns_are_kept(self):
        turns = advisor.parse_transcript("\n".join(f"第{i}条" for i in range(50)))
        self.assertEqual(len(turns), advisor.MAX_TURNS)
        self.assertEqual(turns[-1].text, "第49条")

    def test_hosting_entries_skip_system_lines_and_non_text(self):
        entries = [ChatEntry("incoming", "你好棒哦"), ChatEntry("system", "托管中"),
                   ChatEntry("outgoing", "那就这么说定了"), ChatEntry("incoming", "[非文字] 未处理")]
        self.assertEqual(advisor.turns_from_entries(entries),
                         [Turn("other", "你好棒哦"), Turn("me", "那就这么说定了")])


class GuardTests(unittest.TestCase):
    def test_a_request_to_stop_contact_needs_no_model(self):
        ask = FakeAsk()
        result = advisor.analyze([Turn("other", "别再给我发消息了")], PROFILE, CFG, ask)
        self.assertEqual((result.strategy, result.guarded, result.candidates), ("停止", "stop", ()))
        self.assertEqual(ask.calls, [])

    def test_money_or_ai_questions_go_back_to_the_user(self):
        for text in ("能借我500块吗", "你是不是AI"):
            with self.subTest(text=text):
                result = advisor.analyze([Turn("other", text)], PROFILE, CFG, FakeAsk())
                self.assertEqual(result.guarded, "handoff")
                self.assertEqual(result.candidates, ())

    def test_only_my_own_lines_are_refused(self):
        with self.assertRaises(ValueError):
            advisor.analyze([Turn("me", "在吗")], PROFILE, CFG, FakeAsk())


class RequestTests(unittest.TestCase):
    def test_background_and_transcript_go_in_one_user_message(self):
        ask = FakeAsk(answer())
        advisor.analyze([Turn("me", "我搞了"), Turn("other", "吊不吊")], PROFILE, CFG, ask)
        instructions, content, schema, name = ask.calls[0]
        self.assertIn("刚认识", content)
        self.assertIn("短句，少用句号", content)
        self.assertIn("我：我搞了\n对方：吊不吊", content)
        self.assertIn("candidates", instructions)
        self.assertEqual(name, "goutou_advice")
        self.assertEqual(schema, advisor.ANALYSIS_SCHEMA)

    def test_a_malformed_answer_is_asked_once_more(self):
        ask = FakeAsk("not json", answer())
        result = advisor.analyze([Turn("other", "吊不吊")], PROFILE, CFG, ask)
        self.assertEqual(len(ask.calls), 2)
        self.assertEqual(len(result.candidates), 3)

    def test_two_malformed_answers_are_reported(self):
        with self.assertRaises(ValueError):
            advisor.analyze([Turn("other", "吊不吊")], PROFILE, CFG, FakeAsk("x", "y"))


class ParseTests(unittest.TestCase):
    def test_fields_are_read(self):
        result = advisor.parse_analysis(answer())
        self.assertEqual(result.strategy, "承接")
        self.assertAlmostEqual(result.confidence, 0.55)
        self.assertEqual(result.facts, ("她说做完了",))
        self.assertEqual([c.text for c in result.candidates], ["可以啊，这么快", "牛", "做的什么？给我看看"])

    def test_unknown_strategy_and_bad_confidence_fall_back(self):
        result = advisor.parse_analysis(answer(strategy="冲", confidence=1.7, intent=""))
        self.assertEqual(result.strategy, "先核对")
        self.assertIsNone(result.confidence)
        self.assertIn("证据不足", result.intent)

    def test_unsafe_long_duplicate_and_parrot_candidates_are_dropped(self):
        turns = [Turn("other", "吊不吊")]
        result = advisor.parse_analysis(answer(candidates=[
            {"text": "看这个 https://x.cn", "why": "", "cost": ""},
            {"text": "长" * 121, "why": "", "cost": ""},
            {"text": "吊！", "why": "", "cost": ""},
            {"text": "吊不吊", "why": "", "cost": ""},
            {"text": "挺吊的", "why": "a", "cost": "b"},
            {"text": "挺吊的。", "why": "", "cost": ""},
        ]), turns)
        self.assertEqual([c.text for c in result.candidates], ["吊！", "挺吊的"])

    def test_other_key_names_and_plain_strings_are_accepted(self):
        # Live 16:3x: DeepSeek's JSON mode ignores the schema; an analysis came
        # back without usable candidates.
        result = advisor.parse_analysis(answer(candidates=[
            {"reply": "可以呀", "reason": "接住", "risk": "平淡"},
            {"回复": "那就这么定", "理由": "收住"},
            "好嘞"]))
        self.assertEqual([(c.text, c.why, c.cost) for c in result.candidates],
                         [("可以呀", "接住", "平淡"), ("那就这么定", "收住", ""), ("好嘞", "", "")])
        replies = json.loads(answer())
        replies["replies"] = replies.pop("candidates")
        self.assertEqual(len(advisor.parse_analysis(json.dumps(replies, ensure_ascii=False)).candidates), 3)

    def test_offered_counts_what_the_model_gave_before_filtering(self):
        result = advisor.parse_analysis(answer(candidates=[{"text": "看 https://x", "why": "", "cost": ""}]))
        self.assertEqual((result.offered, result.candidates), (1, ()))
        self.assertEqual(advisor.parse_analysis(answer(candidates=[])).offered, 0)

    def test_the_prompt_pins_the_candidate_shape_with_an_example(self):
        self.assertIn('"candidates": [{"text":', advisor.ADVISOR)
        self.assertIn("不要当成已知事实", advisor.ADVISOR)

    def test_at_most_three_candidates(self):
        many = [{"text": f"回复{i}", "why": "", "cost": ""} for i in range(6)]
        self.assertEqual(len(advisor.parse_analysis(answer(candidates=many)).candidates), 3)


class RewriteTests(unittest.TestCase):
    def test_rewrite_learns_from_my_own_short_lines_only(self):
        ask = FakeAsk(json.dumps({"text": "可以 挺快"}))
        turns = [Turn("me", "哈哈行"), Turn("me", "长" * 80), Turn("me", "看 www.x.com"),
                 Turn("other", "我搞了")]
        text = advisor.rewrite("可以啊，这么快", turns, PROFILE, CFG, ask)
        self.assertEqual(text, "可以 挺快")
        content = ask.calls[0][1]
        # Only the style samples are filtered; the recent dialogue is context.
        samples = content.split("我自己说过的话（只学语气）：\n", 1)[1].split("\n\n对象：", 1)[0]
        self.assertEqual(samples, "哈哈行")
        self.assertIn("短句，少用句号", content)

    def test_an_unusable_rewrite_keeps_the_original(self):
        for bad in ({"text": ""}, {"text": "点这里 https://x"}, {"text": "长" * 130}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    advisor.rewrite("可以啊", [Turn("other", "我搞了")], PROFILE, CFG,
                                    FakeAsk(json.dumps(bad, ensure_ascii=False)))


if __name__ == "__main__":
    unittest.main()

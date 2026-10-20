"""对象画像: observable traits with quoted evidence, kept only when ticked. Offline."""
import json
import tempfile
import unittest
from pathlib import Path

import advisor
from advisor import Trait, Turn
from chat_core import Store, new_profile, prompt_for, traits_text, valid_traits

TURNS = [Turn("other", "哈哈哈哈"), Turn("me", "周末去吃火锅？"), Turn("other", "好呀好呀 我超爱吃火锅"),
         Turn("other", "不过别老问我在干嘛哈"), Turn("me", "行")]


def answer(*traits):
    return json.dumps({"traits": list(traits)}, ensure_ascii=False)


class ParsePortraitTests(unittest.TestCase):
    def test_a_trait_backed_by_her_own_words_is_kept(self):
        result = advisor.parse_portrait(answer(
            {"text": "喜欢吃火锅", "evidence": "我超爱吃火锅", "kind": "兴趣话题"},
            {"text": "不喜欢被问在干嘛", "evidence": "不过别老问我在干嘛哈", "kind": "边界偏好"}), TURNS)
        self.assertEqual(result, [Trait("喜欢吃火锅", "我超爱吃火锅", "兴趣话题"),
                                  Trait("不喜欢被问在干嘛", "不过别老问我在干嘛哈", "边界偏好")])

    def test_evidence_that_she_never_said_is_dropped(self):
        result = advisor.parse_portrait(answer(
            {"text": "喜欢猫", "evidence": "我家有两只猫", "kind": "兴趣话题"},          # invented
            {"text": "爱吃火锅", "evidence": "周末去吃火锅", "kind": "兴趣话题"}), TURNS)  # that was ME
        self.assertEqual(result, [])

    def test_labels_feelings_and_sensitive_facts_are_dropped(self):
        result = advisor.parse_portrait(answer(
            {"text": "性格外向", "evidence": "哈哈哈哈", "kind": "说话方式"},
            {"text": "对你有好感", "evidence": "好呀好呀", "kind": "提到的事"},
            {"text": "最近身体不好", "evidence": "好呀好呀", "kind": "提到的事"},
            {"text": "爱用哈哈", "evidence": "哈哈哈哈", "kind": "说话方式"}), TURNS)
        self.assertEqual([t.text for t in result], ["爱用哈哈"])

    def test_known_traits_are_not_suggested_again_and_bad_kinds_are_blanked(self):
        profile = {"traits": [{"text": "喜欢吃火锅", "evidence": "我超爱吃火锅", "kind": "兴趣话题"}]}
        result = advisor.parse_portrait(answer(
            {"text": "喜欢吃火锅。", "evidence": "我超爱吃火锅", "kind": "兴趣话题"},
            {"text": "回复爱带哈哈", "evidence": "哈哈哈哈", "kind": "外貌"}), TURNS, profile)
        self.assertEqual(result, [Trait("回复爱带哈哈", "哈哈哈哈", "")])

    def test_at_most_six(self):
        many = [{"text": f"特点{i}", "evidence": "哈哈哈哈", "kind": "说话方式"} for i in range(9)]
        self.assertEqual(len(advisor.parse_portrait(answer(*many), TURNS)), advisor.MAX_SUGGESTED_TRAITS)


class SuggestTests(unittest.TestCase):
    def test_request_carries_known_traits_and_the_transcript(self):
        seen = []

        def ask(instructions, content, *, schema, name):
            seen.append((instructions, content, schema, name))
            return answer()
        profile = {"name": "小雨", "traits": [{"text": "爱用哈哈", "evidence": "哈哈", "kind": "说话方式"}]}
        self.assertEqual(advisor.suggest_traits(TURNS, profile, ask), [])
        instructions, content, schema, name = seen[0]
        self.assertIn("[说话方式] 爱用哈哈", content)
        self.assertIn("对方：好呀好呀 我超爱吃火锅", content)
        self.assertIn("不要：性格或人格标签", instructions)
        self.assertEqual((schema, name), (advisor.PORTRAIT_SCHEMA, "goutou_portrait"))

    def test_no_portrait_after_a_request_to_stop_contact(self):
        with self.assertRaises(ValueError):
            advisor.suggest_traits([Turn("other", "别再联系我了")], {"name": "x"}, lambda *a, **k: answer())


class StorageAndPromptTests(unittest.TestCase):
    def test_valid_traits(self):
        self.assertTrue(valid_traits([]))
        self.assertTrue(valid_traits([{"text": "爱吃火锅", "evidence": "我超爱吃火锅", "kind": "兴趣话题",
                                       "added": "2026-10-20"}]))
        for bad in ([{"text": ""}], [{"text": "x", "kind": "外貌"}], [{"text": "x", "secret": 1}],
                    [{"text": "长" * 81}], "爱吃火锅", [{"text": "x"}] * 41):
            with self.subTest(bad=str(bad)[:30]):
                self.assertFalse(valid_traits(bad))

    def test_store_saves_and_rejects_traits(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            try:
                cfg = store.config()
                profile = new_profile("小雨")
                profile["traits"] = [{"text": "爱吃火锅", "evidence": "我超爱吃火锅", "kind": "兴趣话题", "added": ""}]
                cfg["profiles"] = [profile]
                store.save(cfg)
                self.assertEqual(store.config()["profiles"][0]["traits"][0]["text"], "爱吃火锅")
                profile["traits"] = [{"text": "x", "kind": "外貌"}]
                with self.assertRaises(ValueError):
                    store.save(cfg)
            finally:
                store.db.close()

    def test_hosting_prompt_gets_the_portrait_only_when_there_is_one(self):
        cfg = {"my_facts": "", "my_style": "", "style_card": ""}
        plain = new_profile("小雨")
        with_traits = dict(plain, traits=[{"text": "不喜欢被问在干嘛", "evidence": "别老问我在干嘛", "kind": "边界偏好"}])
        self.assertNotIn("对方画像", prompt_for(cfg, plain))
        prompt = prompt_for(cfg, with_traits)
        self.assertTrue(prompt.startswith(prompt_for(cfg, plain)))
        self.assertIn("- [边界偏好] 不喜欢被问在干嘛", prompt)
        self.assertIn("不用来操控", prompt)
        self.assertEqual(traits_text(plain), "")

    def test_advisor_background_includes_the_portrait(self):
        profile = {"name": "小雨", "traits": [{"text": "爱吃火锅", "evidence": "", "kind": "兴趣话题"}]}
        _, content = advisor.build_analysis_request([Turn("other", "在吗")], profile, {})
        self.assertIn("[兴趣话题] 爱吃火锅", content)


if __name__ == "__main__":
    unittest.main()

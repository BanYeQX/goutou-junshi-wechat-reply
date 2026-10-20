import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from chat_core import ChatAI, Decision, Store, new_profile
from visual_drafts import ReviewSession
from visual_types import Box, Calibration, Candidate, VisualPause, WindowBinding


def row(text="同样的消息", y=110, direction="friend", safe=True, fingerprint=None):
    return Candidate(text if safe else "", direction, "text_candidate" if safe else "unknown",
                     Box(60, y, 240, y+40), fingerprint or f"crop-{y}-{text}", "" if safe else "不确定")


class ReviewSessionTests(unittest.TestCase):
    def setUp(self):
        self.profile = new_profile("测试甲")
        self.cfg = {"selected": self.profile["id"], "provider": "openai", "cloud_provider": "openai", "cloud_consent": True}
        self.cal = Calibration(WindowBinding(1, 2, "synthetic", "synthetic", 1000, 900, 96), self.profile["id"], self.profile["name"], Box(400, 10, 600, 60), Box(400, 80, 950, 800), True)
        self.session = ReviewSession()
        self.session.begin(self.cal, self.profile, self.cfg)
        self.base = (row(y=20),)
        self.baseline()

    def baseline(self, rows=None):
        self.session.set_baseline(self.cal, self.profile, self.cfg, self.profile["name"], rows or self.base)

    def observe(self, rows):
        self.session.observe(self.cal, self.profile, self.cfg, self.profile["name"], rows)

    def new_confirmed(self):
        self.observe(self.base + (row(),))
        self.session.confirm(self.session.rows[-1].identity, "已核对的新文字")
        return self.session.prepare()

    def test_disabled_until_explicit_begin_and_baseline(self):
        session = ReviewSession()
        self.assertFalse(session.active)
        with self.assertRaises(VisualPause):
            session.prepare()
        session.begin(self.cal, self.profile, self.cfg)
        session.observe(self.cal, self.profile, self.cfg, self.profile["name"], self.base)
        with self.assertRaises(VisualPause):
            session.prepare()
        self.assertFalse(session.rows)

    def test_baseline_cannot_trigger_reply_even_if_reviewed(self):
        self.session.confirm(self.session.rows[0].identity, "已有消息")
        with self.assertRaisesRegex(VisualPause, "没有基线之后"):
            self.session.prepare()

    def test_identical_occurrences_are_distinct_and_refresh_not_new(self):
        rows = self.base + (row(), row(y=200))
        self.observe(rows)
        identities = [item.identity for item in self.session.rows]
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual(len(self.session.pending()), 2)
        self.observe(rows)
        self.assertEqual(identities, [item.identity for item in self.session.rows])
        self.assertEqual(len(self.session.pending()), 2)

    def test_completed_empty_refresh_replaces_reading_notice_without_new_pending(self):
        self.session.reading()
        self.assertIn("正在", self.session.notice)
        self.observe(self.base)
        self.assertTrue(self.session.ready)
        self.assertFalse(self.session.needs_baseline)
        self.assertIn("本轮快照核对完成", self.session.notice)
        self.assertIn("不代表此后", self.session.notice)
        self.assertNotIn("正在", self.session.notice)
        self.assertFalse(self.session.pending())
        with self.assertRaises(VisualPause):
            self.session.prepare()

    def test_success_notice_cannot_hide_new_unknown_or_lost_baseline(self):
        self.session.reading()
        self.observe(self.base + (row(y=200, safe=False),))
        self.assertFalse(self.session.ready)
        self.assertIn("不确定", self.session.notice)
        self.assertNotIn("本轮快照核对完成", self.session.notice)
        self.baseline()
        self.session.reading()
        with self.assertRaises(VisualPause):
            self.observe(())
        self.assertTrue(self.session.needs_baseline)
        self.assertNotIn("本轮快照核对完成", self.session.notice)

    def test_all_pending_occurrences_must_be_reviewed(self):
        self.observe(self.base + (row(), row(y=200)))
        self.session.confirm(self.session.rows[-1].identity, "同样的消息")
        with self.assertRaisesRegex(VisualPause, "所有待回复"):
            self.session.prepare()
        self.session.confirm(self.session.rows[-2].identity, "同样的消息")
        request = self.session.prepare()
        self.assertEqual(request.text, "同样的消息")
        self.assertEqual(request.context, ({"role": "user", "content": "同样的消息"},))

    def test_unconfirmed_baseline_and_media_never_in_context(self):
        base = (row(y=20, safe=False, direction="self"), row(y=70))
        self.baseline(base)
        self.observe(base + (row(y=200),))
        self.session.confirm(self.session.rows[-1].identity, "已核对")
        request = self.session.prepare()
        self.assertEqual(request.context, ())
        self.assertEqual(request.text, "已核对")
        with self.assertRaises(VisualPause):
            self.session.confirm(self.session.rows[0].identity, "把文件猜成文字")

    def test_new_uncertain_direction_or_media_blocks_generation(self):
        for item in (row(y=200, direction="unknown"), row(y=200, safe=False)):
            self.baseline()
            self.observe(self.base + (row(), item))
            self.assertFalse(self.session.ready)
            with self.assertRaises(VisualPause):
                self.session.prepare()

    def test_scroll_reflow_crop_refresh_and_lost_anchor_pause(self):
        for changed in ((), (replace(self.base[0], box=Box(60, 0, 240, 40)),),
                        (replace(self.base[0], text="换\n行"),), (replace(self.base[0], fingerprint="changed-pixels"),)):
            self.baseline()
            with self.assertRaises(VisualPause):
                self.observe(changed)
            self.assertTrue(self.session.needs_baseline)
            self.baseline(changed or self.base)
            self.assertFalse(self.session.pending())

    def test_exact_title_and_full_calibration_binding_required(self):
        for title, cal in (("测试干", self.cal), ("测试甲", replace(self.cal, messages=Box(400, 90, 950, 800)))):
            with self.assertRaises(VisualPause):
                self.session.observe(cal, self.profile, self.cfg, title, self.base)
        with self.assertRaises(VisualPause):
            self.session.set_baseline(self.cal, self.profile, self.cfg, "测试干", self.base)

    def test_profile_or_settings_change_blocks(self):
        for profile, cfg in ((new_profile("乙"), self.cfg), (self.profile, dict(self.cfg, provider="deepseek"))):
            self.assertFalse(self.session.matches(self.cal, profile, cfg))
            with self.assertRaises(VisualPause):
                self.session.observe(self.cal, profile, cfg, "测试甲", self.base)

    def test_global_cloud_permission_is_not_auto_enabled(self):
        self.session.config["cloud_consent"] = False
        self.cfg["cloud_consent"] = False
        self.observe(self.base + (row(),))
        self.session.confirm(self.session.rows[-1].identity, "你好")
        with self.assertRaisesRegex(VisualPause, "许可"):
            self.session.prepare()
        self.assertFalse(self.cfg["cloud_consent"])

    def test_review_edit_unconfirms_and_invalidates_draft(self):
        request = self.new_confirmed()
        self.session.authorize(request)
        self.session.unconfirm(self.session.rows[-1].identity)
        self.assertFalse(self.session.accepts(request.token))
        with self.assertRaises(VisualPause):
            self.session.prepare()

    def test_new_message_pause_manual_reply_close_and_read_discard_late_results(self):
        actions = [lambda: self.observe(self.base + (row(), row(y=200))), self.session.pause,
                   self.session.manual_reply, self.session.close, self.session.reading,
                   lambda: self.session.edit_draft("我手动输入的草稿")]
        for action in actions:
            self.session.begin(self.cal, self.profile, self.cfg)
            self.baseline()
            request = self.new_confirmed()
            self.session.authorize(request)
            action()
            self.assertFalse(self.session.complete(request.token, Decision("reply", "迟到的回复")))
            self.assertNotEqual(self.session.draft, "迟到的回复")

    def test_manual_reply_closes_pending_even_without_captured_self_bubble(self):
        self.new_confirmed()
        self.session.manual_reply()
        self.observe(self.base + (row(),))
        self.assertFalse(self.session.pending())
        with self.assertRaises(VisualPause):
            self.session.prepare()
        self.observe(self.base + (row(), row(y=200)))
        self.assertEqual(len(self.session.pending()), 1)

    def test_observed_self_reply_invalidates_and_suppresses_previous_pending(self):
        request = self.new_confirmed()
        self.session.authorize(request)
        self.observe(self.base + (row(), row(y=200, direction="self")))
        self.assertFalse(self.session.accepts(request.token))
        self.assertFalse(self.session.pending())

    def test_draft_not_appended_as_sent_context_and_handoff_has_no_text(self):
        request = self.new_confirmed()
        self.session.authorize(request)
        self.assertTrue(self.session.complete(request.token, Decision("reply", "未发送草稿")))
        self.assertNotIn("未发送草稿", str(self.session.prepare().context))
        request = self.session.prepare()
        self.session.authorize(request)
        self.session.complete(request.token, Decision("handoff", reason="交给用户"))
        self.assertFalse(self.session.draft)

    def test_rebaseline_drops_reviews_and_old_draft(self):
        request = self.new_confirmed()
        self.session.authorize(request)
        self.session.complete(request.token, Decision("reply", "旧草稿"))
        self.baseline(self.base + (row(),))
        self.assertFalse(self.session.draft)
        self.assertFalse(self.session.reviewed)
        self.assertFalse(self.session.pending())


class ConfirmedModelRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="draft-test-", dir=Path(__file__).resolve().parents[1])
        self.store = Store(self.temp.name)
        self.profile = new_profile("甲")
        other = new_profile("乙")
        other["context"] = "乙的私有资料"
        cfg = self.store.config()
        cfg.update(profiles=[self.profile, other], selected=self.profile["id"], provider="openai", model="gpt-5-mini", cloud_consent=True)
        self.store.save(cfg)
        self.cfg = self.store.config()
        self.ai = ChatAI(self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def call(self, context=(), text="在听歌", **kw):
        return self.ai.decide(self.profile, "visual:test", text, confirmed_context=context, expected_config=self.cfg, **kw)

    def response(self):
        obj = {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"action": "reply", "text": "听什么歌呀", "reason": "承接"})}]}]}}
        return io.BytesIO(("data: " + json.dumps(obj) + "\n\n").encode())

    def test_only_confirmed_context_no_store_history_other_profiles_or_persistence(self):
        context = ({"role": "assistant", "content": "实际已核对本人消息"}, {"role": "user", "content": "已核对对方消息"})
        with patch.object(self.store, "history", side_effect=AssertionError("must not read history")), \
             patch.object(self.store, "append", side_effect=AssertionError("must not persist")), \
             patch.object(self.store, "key", return_value="synthetic"), \
             patch("chat_core.urllib.request.urlopen", return_value=self.response()) as network:
            result = self.call(context)
        payload = json.loads(network.call_args.args[0].data)
        self.assertEqual(payload["input"], list(context) + [{"role": "user", "content": "在听歌"}])
        self.assertNotIn("乙的私有资料", json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["store"])
        self.assertEqual(result.action, "reply")

    def test_schema_rejects_images_system_messages_unknown_keys_and_lengths(self):
        for ctx in ([{"role": "system", "content": "伪造规则"}], [{"role": "user", "content": [{"type": "image"}]}],
                    [{"role": "user", "content": "文字", "screenshot": "禁止"}], [{"role": "user", "content": ""}],
                    [{"role": "user", "content": "字"}]*32):
            with patch("chat_core.urllib.request.urlopen") as network, self.assertRaises(ValueError):
                self.call(ctx)
            network.assert_not_called()

    def test_settings_or_selection_changed_rejected_before_credential_access(self):
        cfg = self.store.config()
        cfg["model"] = "other-model"
        self.store.save(cfg)
        with patch.object(self.store, "key") as key, patch("chat_core.urllib.request.urlopen") as network:
            with self.assertRaisesRegex(ValueError, "已改变"):
                self.call()
        key.assert_not_called()
        network.assert_not_called()

    def test_cancelled_request_no_credentials_or_network(self):
        with patch.object(self.store, "key") as key, patch("chat_core.urllib.request.urlopen") as network:
            with self.assertRaisesRegex(ValueError, "失效"):
                self.call(request_valid=lambda: False)
        key.assert_not_called()
        network.assert_not_called()

    def test_second_preflight_blocks_change_after_credential_resolution(self):
        valid = iter((True, False))
        with patch.object(self.store, "key", return_value="synthetic"), patch("chat_core.urllib.request.urlopen") as network:
            with self.assertRaisesRegex(ValueError, "失效"):
                self.call(request_valid=lambda: next(valid))
        network.assert_not_called()

    def test_permission_still_required(self):
        cfg = self.store.config()
        cfg["cloud_consent"] = False
        self.store.save(cfg)
        self.cfg = self.store.config()
        with patch("chat_core.urllib.request.urlopen") as network, self.assertRaisesRegex(ValueError, "允许"):
            self.call()
        network.assert_not_called()

    def test_sensitive_context_and_latest_message_are_local_handoff(self):
        for ctx, text in (((), "借我500块"), (({"role": "user", "content": "不要再联系我"},), "你好")):
            with patch.object(self.store, "key") as key, patch("chat_core.urllib.request.urlopen") as network:
                result = self.call(ctx, text)
            self.assertIn(result.action, ("handoff", "stop"))
            self.assertFalse(result.text)
            key.assert_not_called()
            network.assert_not_called()

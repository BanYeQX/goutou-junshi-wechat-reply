"""Synthetic-only explicit snapshot selection; no capture/model/input APIs."""
import unittest
from dataclasses import replace

from PIL import Image, ImageDraw

from chat_core import Decision, new_profile
from visual_drafts import ReviewSession
from visual_sync import pixel_evidence
from visual_types import Box, Calibration, Candidate, VisualPause, WindowBinding


class ManualSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.profile = new_profile("合成目标")
        self.cfg = dict(selected=self.profile["id"], provider="openai", cloud_provider="openai", cloud_consent=True)
        self.cal = Calibration(WindowBinding(1, 2, "synthetic", "synthetic", 1000, 900, 96),
            self.profile["id"], self.profile["name"], Box(100, 10, 400, 60), Box(100, 80, 600, 680), True)
        self.rows = (
            Candidate("重复文字", "friend", "text_candidate", Box(70, 30, 250, 70), "a"),
            Candidate("", "unknown", "unknown", Box(70, 110, 250, 160), "media", "不能猜测"),
            Candidate("本人文字", "self", "text_candidate", Box(300, 210, 460, 250), "b"),
            Candidate("未核对文字", "friend", "text_candidate", Box(70, 320, 250, 360), "c"),
            Candidate("重复文字", "friend", "text_candidate", Box(70, 430, 250, 470), "d"))
        with Image.new("RGB", (500, 600), "white") as image:
            ImageDraw.Draw(image).rectangle((70, 30, 250, 470), fill=(70, 80, 90))
            self.evidence = pixel_evidence(image)
        self.session = ReviewSession()
        self.session.begin(self.cal, self.profile, self.cfg)

    def load(self, **kwargs):
        options = dict(evidence=self.evidence)
        options.update(kwargs)
        self.session.set_manual_snapshot(self.cal, self.profile, self.cfg, self.profile["name"], self.rows, **options)

    def selected(self):
        self.load()
        identity = self.session.rows[-1].identity
        self.session.confirm(identity, "已核对本次文字")
        self.session.choose_manual_target(identity)
        return self.session.prepare()

    def test_explicit_snapshot_needs_no_baseline_but_cannot_select_or_submit_by_itself(self):
        self.load()
        self.assertTrue(self.session.manual_mode)
        self.assertTrue(self.session.ready)
        self.assertTrue(self.session.needs_baseline)
        self.assertFalse(self.session.pixel_tracking)
        self.assertFalse(self.session.baseline)
        self.assertFalse(self.session.pending())
        self.assertIn("未判断新旧", self.session.notice)
        with self.assertRaises(VisualPause):
            self.session.prepare()

    def test_confirming_text_is_not_reply_designation_or_cloud_authorization(self):
        self.load()
        identity = self.session.rows[-1].identity
        self.session.confirm(identity, "已核对")
        self.assertFalse(self.session.pending())
        self.assertIsNone(self.session.request_token)
        with self.assertRaises(VisualPause):
            self.session.prepare()

    def test_only_reviewed_safe_friend_can_be_designated(self):
        self.load()
        for index in (0, 1, 2):
            with self.assertRaises(VisualPause):
                self.session.choose_manual_target(self.session.rows[index].identity)
        self.session.confirm(self.session.rows[2].identity, "本人文字")
        with self.assertRaises(VisualPause):
            self.session.choose_manual_target(self.session.rows[2].identity)
        with self.assertRaises(VisualPause):
            self.session.confirm(self.session.rows[1].identity, "把媒体猜成文字")

    def test_context_contains_only_reviewed_preceding_text_not_media_or_unsent_draft(self):
        request = self.selected()
        self.session.confirm(self.session.rows[0].identity, "核对对方")
        self.session.confirm(self.session.rows[2].identity, "核对本人")
        request = self.session.prepare()
        self.assertEqual(request.mode, "manual_snapshot")
        self.assertEqual(request.text, "已核对本次文字")
        self.assertEqual(request.context, ({"role": "user", "content": "核对对方"},
                                           {"role": "assistant", "content": "核对本人"}))
        self.assertNotIn("未核对文字", str(request.context))
        self.assertNotIn("不能猜测", str(request.context))
        self.session.authorize(request)
        self.assertTrue(self.session.complete(request.token, Decision("reply", "未发送草稿")))
        self.assertNotIn("未发送草稿", str(self.session.prepare().context))

    def test_identical_occurrences_are_distinct_and_only_one_is_selected(self):
        self.selected()
        a, b = self.session.rows[0], self.session.rows[-1]
        self.assertEqual(a.text, b.text)
        self.assertNotEqual(a.identity, b.identity)
        self.assertEqual([row.identity for row in self.session.pending()], [b.identity])
        self.session.confirm(a.identity, "第一条")
        old = self.session.prepare()
        self.session.authorize(old)
        self.session.choose_manual_target(a.identity)
        self.assertEqual([row.identity for row in self.session.pending()], [a.identity])
        self.assertFalse(self.session.accepts(old.token))

    def test_refresh_even_same_pixels_drops_reviews_target_and_late_result(self):
        request = self.selected()
        old_ids = {row.identity for row in self.session.rows}
        self.session.authorize(request)
        self.session.observe(self.cal, self.profile, self.cfg, self.profile["name"], self.rows, evidence=self.evidence)
        self.assertTrue(self.session.manual_mode)
        self.assertTrue(self.session.needs_baseline)
        self.assertFalse(self.session.reviewed)
        self.assertFalse(self.session.pending())
        self.assertFalse(old_ids.intersection(row.identity for row in self.session.rows))
        self.assertFalse(self.session.complete(request.token, Decision("reply", "迟到结果")))

    def test_pause_read_reply_close_and_mode_exit_invalidate_selection_and_inflight_request(self):
        for action in ("pause", "reading", "manual_reply", "close", "leave_manual_mode"):
            self.session.begin(self.cal, self.profile, self.cfg)
            request = self.selected()
            self.session.authorize(request)
            getattr(self.session, action)()
            self.assertFalse(self.session.rows)
            self.assertFalse(self.session.reviewed)
            self.assertFalse(self.session.manual_target)
            self.assertFalse(self.session.complete(request.token, Decision("reply", "迟到")))

    def test_editing_selected_text_requires_a_new_designation(self):
        request = self.selected()
        self.session.authorize(request)
        target = self.session.manual_target
        self.session.unconfirm(target)
        self.assertFalse(self.session.pending())
        self.assertFalse(self.session.accepts(request.token))
        self.session.confirm(target, "修正后")
        with self.assertRaises(VisualPause):
            self.session.prepare()
        self.session.choose_manual_target(target)
        self.assertEqual(self.session.prepare().text, "修正后")
        self.session.confirm(target, "再次修正")
        self.assertFalse(self.session.pending())

    def test_missing_malformed_mismatched_evidence_or_outside_candidate_fails_closed(self):
        for evidence in (None, replace(self.evidence, rows=()), replace(self.evidence, width=501)):
            with self.assertRaises(VisualPause):
                self.load(evidence=evidence)
            self.assertFalse(self.session.ready)
            self.assertFalse(self.session.rows)
        self.rows = (replace(self.rows[0], box=Box(-1, 30, 250, 70)),)
        with self.assertRaises(VisualPause):
            self.load()

    def test_wrong_title_profile_config_and_calibration_still_reject(self):
        for cal, profile, cfg, title in (
            (self.cal, self.profile, self.cfg, "其他目标"),
            (self.cal, new_profile("其他目标"), self.cfg, self.profile["name"]),
            (self.cal, self.profile, dict(self.cfg, model="changed"), self.profile["name"]),
            (replace(self.cal, title=Box(100, 10, 450, 60)), self.profile, self.cfg, self.profile["name"])):
            with self.assertRaises(VisualPause):
                self.session.set_manual_snapshot(cal, profile, cfg, title, self.rows, evidence=self.evidence)
            self.assertFalse(self.session.ready)

    def test_existing_alignment_failure_not_relabelled_as_restored_baseline(self):
        self.session.require_rebaseline("旧画面没有唯一的像素重叠")
        self.session.sync_diagnostic = {"result": "no_exact_match", "exact_matches": 0}
        self.load()
        self.assertTrue(self.session.needs_baseline)
        self.assertFalse(self.session.pixel_tracking)
        self.assertEqual(self.session.sync_diagnostic["result"], "no_exact_match")
        self.assertIn("未恢复像素同步", self.session.notice)
        self.assertTrue(self.session.rebaseline_reason)

    def test_cloud_consent_is_not_enabled_or_bypassed(self):
        self.selected()
        for cfg in (dict(self.cfg, cloud_consent=False), dict(self.cfg, cloud_provider="other")):
            self.session.config = cfg
            with self.assertRaisesRegex(VisualPause, "许可"):
                self.session.prepare()
        self.assertTrue(self.cfg["cloud_consent"])

    def test_request_mode_must_match_current_mode_and_revision(self):
        request = self.selected()
        with self.assertRaises(VisualPause):
            self.session.authorize(replace(request, mode="new_messages"))
        self.session.authorize(request)
        self.session.leave_manual_mode()
        with self.assertRaises(VisualPause):
            self.session.authorize(request)

    def test_return_to_baseline_never_carries_manual_selection_or_calls_it_new(self):
        self.selected()
        self.session.set_baseline(self.cal, self.profile, self.cfg, self.profile["name"], self.rows,
                                  evidence=self.evidence, bottom_confirmed=True)
        self.assertFalse(self.session.manual_mode)
        self.assertFalse(self.session.manual_target)
        self.assertFalse(self.session.reviewed)
        self.assertFalse(self.session.pending())
        self.assertTrue(self.session.pixel_tracking)
        with self.assertRaises(VisualPause):
            self.session.prepare()


if __name__ == "__main__":
    unittest.main()

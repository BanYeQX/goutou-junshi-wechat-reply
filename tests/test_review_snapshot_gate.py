"""Regression for the reported ready-label / rejected-baseline mismatch."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from chat_core import new_profile
from visual_drafts import ReviewSession
from visual_draft_ui import DraftReviewWindow
from visual_types import Box, Calibration, VisualPause, WindowBinding


class SnapshotGateTests(unittest.TestCase):
    def setUp(self):
        self.profile = new_profile("合成目标")
        self.cfg = {"selected": self.profile["id"]}
        self.cal = Calibration(WindowBinding(1, 2, "synthetic", "synthetic", 1000, 900, 96),
            self.profile["id"], self.profile["name"], Box(400, 10, 600, 60), Box(400, 80, 950, 800), True)
        self.session = ReviewSession()
        self.session.begin(self.cal, self.profile, self.cfg)
        self.result = {"epoch": 4, "title": self.profile["name"], "rows": (), "evidence": object()}
        self.app = SimpleNamespace(visual_last=self.result, visual_calibration=self.cal,
            visual=SimpleNamespace(epoch=4, running=False), visual_job_busy=False,
            visual_status=SimpleNamespace(get=lambda: "微信不在前台，已暂停"),
            store=SimpleNamespace(selected=lambda: self.profile, config=lambda: self.cfg))
        self.window = object.__new__(DraftReviewWindow)
        self.window.app, self.window.session = self.app, self.session
        self.window.baseline_button, self.window.generate_button = Mock(), Mock()
        self.window.bottom_check, self.window.manual_snapshot_button = Mock(), Mock()
        self.window.status = Mock()
        self.window.bottom_confirmed = SimpleNamespace(get=lambda: True)

    def baseline_disabled(self):
        self.window.update_availability()
        self.window.baseline_button.config.assert_called_with(state="disabled")
        with self.assertRaises(VisualPause):
            self.window.current_snapshot()

    def test_initial_empty_snapshot_reports_actual_pause_reason(self):
        self.app.visual_last = None
        self.baseline_disabled()
        self.assertIn("微信不在前台", self.window.status.set.call_args.args[0])
        self.assertNotIn("快照可设为基线", self.window.status.set.call_args.args[0])

    def test_read_in_progress_is_not_a_ready_baseline(self):
        self.app.visual.running = True
        self.baseline_disabled()
        self.assertIn("5秒只是开始前", self.window.status.set.call_args.args[0])

    def test_stop_invalidates_epoch_even_with_old_ready_notice(self):
        self.session.notice = "本轮快照可设为基线"
        self.app.visual.epoch += 1
        self.baseline_disabled()
        self.assertIn("已作废", self.window.status.set.call_args.args[0])

    def test_title_mismatch_and_calibration_change_disable_baseline(self):
        self.result["title"] = "其他目标"
        self.baseline_disabled()
        self.result["title"] = self.profile["name"]
        self.app.visual_calibration = replace(self.cal, title=Box(400, 10, 650, 60))
        self.baseline_disabled()

    def test_settings_change_and_closed_session_disable_baseline(self):
        self.cfg["changed"] = True
        self.baseline_disabled()
        del self.cfg["changed"]
        self.session.close()
        self.baseline_disabled()

    def test_worker_finish_reenables_baseline_without_new_capture(self):
        self.app.visual.running = True
        self.baseline_disabled()
        self.app.visual.running = False
        self.window.update_availability()
        self.window.baseline_button.config.assert_called_with(state="normal")
        self.assertIs(self.window.current_snapshot()[1], self.result)
        self.window.generate_button.config.assert_called_with(state="disabled")

    def test_poll_updates_only_controls_without_resetting_edits(self):
        self.window.closed = False
        self.window.window = Mock()
        self.window.editor, self.window.draft = Mock(), Mock()
        self.window.poll_availability()
        self.window.window.after.assert_called_once()
        self.window.editor.delete.assert_not_called()
        self.window.draft.delete.assert_not_called()
        self.window.closed = True
        self.window.window.reset_mock()
        self.window.poll_availability()
        self.window.window.after.assert_not_called()

    def test_latest_bottom_must_be_confirmed_and_evidence_present(self):
        self.window.bottom_confirmed = SimpleNamespace(get=lambda: False)
        self.baseline_disabled()
        self.assertIn("最新底部", self.window.status.set.call_args.args[0])
        self.window.bottom_confirmed = SimpleNamespace(get=lambda: True)
        del self.result["evidence"]
        self.baseline_disabled()
        self.assertIn("像素衔接摘要", self.window.status.set.call_args.args[0])

    def test_repoll_after_failed_alignment_shows_persistent_loss_and_disables_generation(self):
        self.session.require_rebaseline("旧画面没有唯一的像素重叠")
        self.session.observe(self.cal,self.profile,self.cfg,self.profile["name"],())
        self.window.update_availability()
        self.window.baseline_button.config.assert_called_with(state="normal")
        self.window.generate_button.config.assert_called_with(state="disabled")
        self.assertIn("旧画面没有唯一的像素重叠",self.window.status.set.call_args.args[0])
        self.assertIn("将当前来信全部记为已有消息",self.window.status.set.call_args.args[0])

    def test_manual_snapshot_needs_no_bottom_tick_but_all_fresh_snapshot_guards_remain(self):
        self.window.bottom_confirmed = SimpleNamespace(get=lambda: False)
        self.assertIn("最新底部", self.window.snapshot_problem())
        self.assertEqual(self.window.snapshot_problem(require_bottom=False), "")
        self.window.update_availability()
        self.assertEqual(self.window.manual_snapshot_button.config.call_args.kwargs["state"], "normal")
        for mutation in (lambda: setattr(self.app.visual, "running", True),
                         lambda: self.result.update(epoch=3),
                         lambda: self.result.update(title="其他目标"),
                         lambda: self.result.update(evidence=None)):
            self.app.visual.running = False
            self.result.update(epoch=4, title=self.profile["name"], evidence=object())
            mutation()
            self.assertTrue(self.window.snapshot_problem(require_bottom=False))

    def test_manual_mode_never_enables_baseline_and_generation_still_checks_freshness(self):
        self.session.manual_mode, self.session.ready = True, True
        self.session.notice = "人工快照；未判断新旧"
        self.window.bottom_confirmed = SimpleNamespace(get=lambda: False)
        self.window.update_availability()
        self.window.baseline_button.config.assert_called_with(state="disabled")
        self.window.bottom_check.config.assert_called_with(state="disabled")
        self.window.generate_button.config.assert_called_with(state="disabled")
        self.session.pending = Mock(return_value=(object(),))
        self.window.update_availability()
        self.window.generate_button.config.assert_called_with(state="normal")
        self.app.visual.epoch += 1
        self.window.update_availability()
        self.window.generate_button.config.assert_called_with(state="disabled")

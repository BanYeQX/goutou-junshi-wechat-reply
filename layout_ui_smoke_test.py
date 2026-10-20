"""Build the real Tk App with a temporary synthetic Store, not user data."""
import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


def run_synthetic_ui(app_class, project):
    import tkinter as tk
    from chat_core import Decision, Store, new_profile
    from visual_types import Box, Calibration, Candidate, WindowBinding
    from visual_sync import pixel_evidence
    from PIL import Image, ImageDraw

    def synthetic_evidence(rows):
        image = Image.new("RGB", (850, 720), "white")
        draw = ImageDraw.Draw(image)
        for number, row in enumerate(rows):
            for y in range(row.box.top, row.box.bottom):
                draw.line((row.box.left, y, row.box.right-1, y), fill=((y-row.box.top+number*29)%200, 70, 100))
        try:
            return pixel_evidence(image)
        finally:
            image.close()

    root = tk.Tk()
    root.withdraw()
    application = None
    with tempfile.TemporaryDirectory(prefix="ui-selftest-", dir=project) as folder:
        store = Store(folder)
        cfg = store.config()
        profile = new_profile("合成测试联系人")
        cfg.update(profiles=[profile], selected=profile["id"], autostart=False, cloud_consent=True)
        store.save(cfg)
        module = app_class.__module__
        try:
            with patch(module + ".Store", return_value=store), \
                 patch(module + ".compatibility", return_value={"ready": False, "message": "合成界面自检"}):
                application = app_class(root)
                assert str(application.visual_map_button["state"]) == "disabled"
                calibration = Calibration(WindowBinding(1, 1, "synthetic", "synthetic", 1000, 900, 96),
                    profile["id"], profile["name"], Box(100, 10, 500, 60), Box(100, 80, 950, 800), True)
                application.visual_calibration = calibration
                rows = (Candidate("", "self", "unknown", Box(180, 20, 800, 430), "synthetic",
                                  "合成不确定块", evidence_boxes=(Box(200, 70, 600, 95),)),
                        Candidate("合成文字", "friend", "text_candidate", Box(70, 520, 220, 570), "synthetic-2"),
                        Candidate("合成文字", "friend", "text_candidate", Box(70, 610, 220, 660), "synthetic-3"))
                application.visual_last = dict(epoch=application.visual.epoch, title=profile["name"], rows=rows, message="合成快照")
                application.view_mode.set("视觉只读")
                application.render_history()
                root.update_idletasks()
                assert str(application.preview_button["state"]) == "disabled"
                assert str(application.approve_button["state"]) == "disabled"
                assert str(application.visual_confirm_button["state"]) == "disabled"
                assert str(application.visual_map_button["state"]) == "normal"
                button = application.visual_map_button
                assert button.winfo_x() + button.winfo_width() <= button.master.winfo_width()
                application.visual_show_locations()
                preview = application.visual_location
                root.update_idletasks()
                assert preview and preview.window.winfo_exists()
                application.visual_stop()
                assert preview.closed
                assert str(application.visual_map_button["state"]) == "disabled"
                assert not application.draft_token
                # Synthetic full phase-two path. No consent dialog clicked,
                # no user data read, no model HTTP: the fake decision is local.
                application.visual_last = dict(epoch=application.visual.epoch, title=profile["name"], rows=rows, evidence=synthetic_evidence(rows), message="合成基线快照")
                application.visual_open_review()
                review = application.visual_draft_window
                root.update_idletasks()
                assert review and review.window.winfo_exists()
                assert application.visual_review.active
                assert not application.visual_review.draft
                assert not review.bottom_confirmed.get()
                assert str(review.baseline_button["state"]) == "disabled"
                # Synthetic state only. Never tick a real user's confirmation.
                review.bottom_confirmed.set(True)
                review.update_availability()
                assert str(review.baseline_button["state"]) == "normal"
                # A result can reach Tk before the worker's finally block.
                # The UI must wait and then become usable without re-capture.
                application.visual.running = True
                review.update_availability()
                assert str(review.baseline_button["state"]) == "disabled"
                application.visual.running = False
                review.update_availability()
                assert str(review.baseline_button["state"]) == "normal"
                review.baseline()
                assert len(application.visual_review.baseline) == 3
                added = Candidate("合成新增文字", "friend", "text_candidate", Box(70, 680, 220, 710), "synthetic-4")
                application.visual_review.reading()
                application.visual.events.put(dict(kind="snapshot", epoch=application.visual.epoch, title=profile["name"], rows=rows+(added,), evidence=synthetic_evidence(rows+(added,)), added=1, paused=True, message="合成新增快照"))
                application.drain()
                assert len(application.visual_review.pending()) == 1
                review.list.selection_clear(0, "end")
                review.list.selection_set(3)
                review.select()
                review.confirm()
                request = application.visual_review.prepare()
                assert request.text == "合成新增文字" and not request.context
                application.visual_review.authorize(request)
                with patch.object(application.ai, "decide", return_value=Decision("reply", "合成可编辑草稿")) as model:
                    review.run_request(request, None)
                    deadline = time.monotonic() + 3
                    while application.visual_job_busy and time.monotonic() < deadline:
                        application.drain()
                        root.update_idletasks()
                        time.sleep(.01)
                    assert not application.visual_job_busy
                    assert model.call_args.kwargs["confirmed_context"] == ()
                assert review.draft.get("1.0", "end-1c") == "合成可编辑草稿"
                root.update()
                review.draft.insert("end", "，手动编辑")
                root.update()
                assert application.visual_review.draft.endswith("手动编辑")
                assert not store.volatile
                assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
                assert str(application.approve_button["state"]) == "disabled"
                # A second incoming item scrolls the same generated pixels.
                # Old OCR boxes can be clipped; only the unique new tail is
                # marked new, and the previously edited draft is invalidated.
                previous = rows + (added,)
                moved = tuple(replace(item, box=Box(item.box.left+1, item.box.top-40, item.box.right+1, item.box.bottom-40)) for item in previous)
                second_added = Candidate("合成新增文字二", "friend", "text_candidate", Box(70,680,230,710), "synthetic-5")
                actual = moved + (second_added,)
                raw = tuple(replace(item, box=Box(item.box.left, 0, item.box.right, item.box.bottom)) if item.box.top < 0 else item for item in actual)
                application.visual_review.reading()
                application.visual.events.put(dict(kind="snapshot", epoch=application.visual.epoch,
                    title=profile["name"], rows=raw, evidence=synthetic_evidence(actual), added=1,
                    paused=True, message="合成上移后新增快照"))
                application.drain()
                assert application.visual_review.tracker.last_shift == -40
                assert application.visual_review.tracker.last_shift_x == 1
                assert "横向+1像素" in review.status.get()
                assert len(application.visual_review.pending()) == 2
                assert application.visual_review.pending()[-1].text == "合成新增文字二"
                assert not application.visual_review.draft
                assert not review.draft.get("1.0", "end-1c")
                # A failed comparison clears the stale list/editor. A later
                # successful capture must not turn that failure into a false
                # ready/synchronized label, nor silently adopt a new baseline.
                bad_evidence = replace(synthetic_evidence(actual), background=b"x"*32)
                application.visual_review.reading()
                application.visual.events.put(dict(kind="snapshot", epoch=application.visual.epoch,
                    title=profile["name"], rows=raw, evidence=bad_evidence, added=0,
                    paused=True, message="合成匹配失败"))
                application.drain()
                assert application.visual_review.needs_baseline
                assert not application.visual_review.rows
                assert review.list.size() == 0
                assert not review.editor.get("1.0", "end-1c")
                assert str(review.generate_button["state"]) == "disabled"
                application.visual_review.reading()
                application.visual.events.put(dict(kind="snapshot", epoch=application.visual.epoch,
                    title=profile["name"], rows=raw, evidence=synthetic_evidence(actual), added=0,
                    paused=True, message="合成重测快照"))
                application.drain()
                assert "旧基线已失效" in review.status.get()
                assert "窗口区域或背景变化" in review.status.get()
                assert not application.visual_review.rows and review.list.size() == 0
                assert str(review.generate_button["state"]) == "disabled"
                application.visual_stop()
                assert not application.visual_review.draft
                assert not review.draft.get("1.0", "end-1c")
                assert str(review.baseline_button["state"]) == "disabled"
                assert str(review.generate_button["state"]) == "disabled"
                # Report the capture guard's actual fixed diagnostic, not an
                # inviting ready label or the old generic baseline error.
                application.visual.events.put(dict(kind="paused", epoch=application.visual.epoch,
                    message="合成前台校验未通过"))
                application.drain()
                assert "合成前台校验未通过" in review.status.get()
                assert not application.visual_last
                # Phase-two session remains a send guard even when the user
                # switches the main view away from the visual tab.
                application.view_mode.set("微信")
                application.render_history()
                with patch(module + ".messagebox.showinfo"), patch.object(application.engine, "approve") as send:
                    application.approve()
                    send.assert_not_called()
                review.close()
                assert not application.visual_review.active and not application.visual_review.rows
                # Exact empty refresh with an existing OCR box below the
                # pixel tail must neither duplicate that box nor pause the UI.
                # Only generated images and the temporary synthetic Store.
                ghost = Candidate("", "unknown", "unknown", Box(70,680,220,710),
                                  "synthetic-ocr-tail", "合成底边外识别框")
                raw_empty = rows + (ghost,)
                same_pixels = synthetic_evidence(rows)  # No ink for the ghost.
                application.visual_last = dict(epoch=application.visual.epoch,
                    title=profile["name"], rows=raw_empty, evidence=same_pixels, message="合成待建基线")
                application.visual_open_review()
                review = application.visual_draft_window
                review.bottom_confirmed.set(True)  # Synthetic test only.
                review.update_availability()
                review.baseline()
                baseline_ids = tuple(row.identity for row in application.visual_review.rows)
                for _ in range(2):
                    application.visual_review.reading()
                    application.visual.events.put(dict(kind="snapshot", epoch=application.visual.epoch,
                        title=profile["name"], rows=raw_empty, evidence=same_pixels,
                        added=0, paused=True, message="合成像素完全相同的空刷新"))
                    application.drain()
                    assert application.visual_review.ready
                    assert not application.visual_review.needs_baseline
                    assert "本轮快照核对完成" in application.visual_review.notice
                    assert "正在重新核对" not in application.visual_review.notice
                    assert tuple(row.identity for row in application.visual_review.rows) == baseline_ids
                    assert review.list.size() == len(raw_empty)
                    assert not application.visual_review.rows[-1].safe
                    assert not application.visual_review.pending()
                    assert not application.visual_review.draft
                    assert str(application.approve_button["state"]) == "disabled"
                with patch(module + ".messagebox.showinfo"), patch.object(application.ai, "decide") as model:
                    review.generate()
                    model.assert_not_called()
                review.close()
                # Alternate explicit manual snapshot route. It must work on
                # existing text, without marking it new or rebuilding a
                # baseline, and still require a separate model consent.
                application.visual_last = dict(epoch=application.visual.epoch,
                    title=profile["name"], rows=rows, evidence=synthetic_evidence(rows), message="合成人工快照")
                application.visual_open_review()
                review = application.visual_draft_window
                root.update_idletasks()
                assert not review.bottom_confirmed.get()
                assert str(review.manual_snapshot_button["state"]) == "normal"
                review.manual_snapshot()
                session = application.visual_review
                assert session.manual_mode and session.needs_baseline and not session.pixel_tracking
                assert review.list.size() == len(rows)
                assert not session.pending() and not session.reviewed
                assert str(review.generate_button["state"]) == "disabled"
                assert str(review.baseline_button["state"]) == "disabled"
                review.list.selection_clear(0, "end")
                review.list.selection_set(0)
                review.select()
                assert str(review.confirm_button["state"]) == "disabled"
                assert str(review.manual_target_button["state"]) == "disabled"
                review.list.selection_clear(0, "end")
                review.list.selection_set(1)
                review.select()
                review.confirm()
                assert not session.pending()
                assert str(review.generate_button["state"]) == "disabled"
                review.choose_manual_target()
                assert len(session.pending()) == 1
                assert "本次指定" in review.list.get(1)
                assert str(review.generate_button["state"]) == "normal"
                with patch.object(application.ai, "decide") as model:
                    review.generate()
                    model.assert_not_called()
                assert review.consent_window is not None
                review.close_consent()  # No model consent clicked.
                request = session.prepare()
                assert request.mode == "manual_snapshot" and request.text == "合成文字"
                assert not request.context
                session.authorize(request)  # Synthetic state only.
                with patch.object(application.ai, "decide", return_value=Decision("reply", "合成人工草稿")):
                    review.run_request(request, None)
                    deadline = time.monotonic() + 3
                    while application.visual_job_busy and time.monotonic() < deadline:
                        application.drain()
                        root.update_idletasks()
                        time.sleep(.01)
                assert session.draft == "合成人工草稿"
                assert str(review.draft["state"]) == "normal"
                root.update_idletasks()
                assert review.manual_target_button.winfo_x() + review.manual_target_button.winfo_width() <= review.manual_target_button.master.winfo_width()
                assert review.safety_notice.winfo_height() >= review.safety_notice.winfo_reqheight()
                assert review.form_canvas.bbox("all")[3] >= review.safety_notice.winfo_y() + review.safety_notice.winfo_height()
                review.form_canvas.yview_moveto(1.0)
                assert not store.volatile
                assert store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
                # A new capture, even identical, must not inherit designation
                # or reviewed text and must discard any in-flight old result.
                old_ids = {row.identity for row in session.rows}
                application.visual_review.reading()
                application.visual.events.put(dict(kind="snapshot", epoch=application.visual.epoch,
                    title=profile["name"], rows=rows, evidence=synthetic_evidence(rows), added=0,
                    paused=True, message="合成人工快照刷新"))
                application.drain()
                assert session.manual_mode and not session.reviewed and not session.pending() and not session.draft
                assert not old_ids.intersection(row.identity for row in session.rows)
                assert str(review.generate_button["state"]) == "disabled"
                assert str(application.approve_button["state"]) == "disabled"
                report = json.loads((store.data / "visual_draft_status.json").read_text(encoding="utf-8"))
                assert report["review_mode"] == "manual_snapshot"
                assert report["pending_new_count"] == 0 and report["manual_pending_count"] == 0
                review.close()
                # New experiment entry is idle by default and is independent
                # of completed manual drafts. Cancel must never start capture.
                assert application.visual_shadow_report is None
                root.update_idletasks()
                shadow_button = application.visual_shadow_button
                assert shadow_button.winfo_x() + shadow_button.winfo_width() <= shadow_button.master.winfo_width()
                with patch.object(application.visual, "start_shadow") as start_shadow:
                    application.visual_open_shadow()
                    consent_window = application.visual_shadow_dialog
                    assert consent_window is not None
                    assert not consent_window.bottom.get() and not consent_window.operations.get()
                    assert str(consent_window.confirm["state"]) == "disabled"
                    start_shadow.assert_not_called()
                    consent_window.close()
                    start_shadow.assert_not_called()
                return {"success": True, "synthetic_app_ui": True, "map_visible_in_layout": True,
                        "shadow_entry_visible_and_default_off": True,
                        "shadow_cancel_does_not_start_capture": True,
                        "manual_snapshot_without_new_message": True,
                        "manual_target_needs_review_and_separate_consent": True,
                        "manual_refresh_drops_selection_and_review": True,
                        "uncertain_snapshot_cannot_confirm": True, "stop_closes_map": True,
                        "synthetic_review_baseline_new_confirm_edit_pause": True,
                        "baseline_button_matches_snapshot_gate": True,
                        "pause_reason_visible_in_review": True,
                        "pixel_scroll_new_tail_and_draft_invalidation": True,
                        "exact_two_dimensional_scroll_and_invalidation": True,
                        "latest_bottom_confirmation_default_off": True,
                        "failed_alignment_clears_stale_list_editor": True,
                        "retest_keeps_baseline_failure_visible": True,
                        "unchanged_pixel_ocr_tail_no_duplicate_or_reply": True,
                        "empty_refresh_completed_notice": True,
                        "unsent_draft_not_in_history": True, "visual_send_guard": True,
                        "real_user_data_access": False, "wechat_captures": 0,
                        "model_calls": 0, "wechat_inputs": 0, "wechat_sends": 0,
                        "frozen": bool(getattr(sys, "frozen", False))}
        finally:
            if application:
                application.close()
            else:
                root.destroy()
            store.db.close()


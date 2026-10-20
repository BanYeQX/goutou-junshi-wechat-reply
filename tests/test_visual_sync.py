import unittest
from dataclasses import replace
from types import SimpleNamespace

from PIL import Image, ImageDraw

from chat_core import new_profile
from visual_drafts import ReviewSession
from visual_sync import PixelEvidence, PixelSequenceTracker, pixel_evidence, unique_shift, safe_alignment_diagnostic
from visual_types import Box, Candidate, VisualPause, WindowBinding, Calibration


WIDTH, HEIGHT = 500, 600


def candidate(text, top, key=None, safe=True, direction="friend"):
    return Candidate(text if safe else "", direction, "text_candidate" if safe else "unknown",
        Box(70, top, 250, top+50), key or text, "" if safe else "合成不确定")


def image_for(rows, patterns=None):
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    for index, row in enumerate(rows):
        seed = (patterns or {}).get(row.fingerprint, sum(row.fingerprint.encode()) % 200)
        for y in range(max(0, row.box.top), min(HEIGHT, row.box.bottom)):
            offset = y-row.box.top
            draw.line((row.box.left, y, row.box.right-1, y), fill=((seed+offset*7)%230, (offset*11)%210, 80))
    return image


def evidence_for(rows, **kw):
    image = image_for(rows, **kw)
    try:
        return pixel_evidence(image)
    finally:
        image.close()


def move(rows, dy):
    return tuple(replace(row, box=Box(row.box.left, row.box.top+dy, row.box.right, row.box.bottom+dy)) for row in rows)


class PixelTrackerTests(unittest.TestCase):
    def setUp(self):
        self.old = (candidate("独特旧锚点", 100), candidate("重复文字", 260, "same"), candidate("重复文字", 400, "same"))
        self.old_evidence = evidence_for(self.old)
        self.tracker = PixelSequenceTracker()
        self.baseline, _ = self.tracker.update(self.old, self.old_evidence)

    def test_fixed_append_and_refresh_preserve_occurrence_ids(self):
        rows = self.old + (candidate("新增文字", 500),)
        tracked, added = self.tracker.update(rows, evidence_for(rows))
        self.assertEqual(len(added), 1)
        self.assertEqual([r.identity for r in tracked[:3]], [r.identity for r in self.baseline])
        again, repeated = self.tracker.update(rows, evidence_for(rows))
        self.assertFalse(repeated)
        self.assertEqual([r.identity for r in again], [r.identity for r in tracked])

    def test_exact_refresh_does_not_duplicate_old_candidate_below_pixel_tail(self):
        # OCR boxes need not end at the last non-background pixel. Reproduce
        # the real aggregate symptom: identical pixels, one extra old unknown.
        raw = self.old + (candidate("", 500, "old-tail-unknown", safe=False),)
        tracker = PixelSequenceTracker()
        base, _ = tracker.update(raw, self.old_evidence)
        for _ in range(3):
            tracked, added = tracker.update(raw, self.old_evidence)
            self.assertFalse(added)
            self.assertEqual([r.identity for r in tracked], [r.identity for r in base])
            self.assertEqual(len(tracked), len(raw))
            self.assertFalse(tracked[-1].safe)
            report = safe_alignment_diagnostic(tracker.diagnostic)
            self.assertEqual(report["old_candidate_count"], len(raw))
            self.assertEqual(report["current_candidate_count"], len(raw))
            self.assertEqual(report["retained_candidate_count"], len(raw))
            self.assertEqual(report["added_candidate_count"], 0)
            self.assertEqual(report["unchanged_tail_candidate_count"], 1)

    def test_exact_refresh_candidate_padding_across_pixel_tail_is_not_new(self):
        padding = replace(candidate("", 430, "padding", safe=False), box=Box(70,430,250,480))
        raw = self.old + (padding,)
        tracker = PixelSequenceTracker()
        base, _ = tracker.update(raw, self.old_evidence)
        tracked, added = tracker.update(raw, self.old_evidence)
        self.assertFalse(added)
        self.assertEqual([r.identity for r in tracked], [r.identity for r in base])

    def test_exact_refresh_cannot_create_events_from_new_ocr_tail_boxes(self):
        for safe, direction in ((False, "unknown"), (True, "friend"), (True, "self")):
            with self.subTest(safe=safe, direction=direction):
                tracker = PixelSequenceTracker()
                base, _ = tracker.update(self.old, self.old_evidence)
                raw = self.old + (candidate("识字幻觉", 500, "new-ocr-box", safe=safe, direction=direction),)
                tracked, added = tracker.update(raw, self.old_evidence)
                self.assertFalse(added)
                self.assertEqual([r.identity for r in tracked], [r.identity for r in base])

    def test_exact_refresh_never_promotes_unknown_tail_to_text(self):
        unknown = candidate("",500,"unknown",safe=False)
        tracker = PixelSequenceTracker()
        base, _ = tracker.update(self.old + (unknown,), self.old_evidence)
        raw = self.old + (replace(unknown, text="后来识别为文字", kind="text_candidate", uncertainty=""),)
        tracked, added = tracker.update(raw, self.old_evidence)
        self.assertFalse(added)
        self.assertEqual([r.identity for r in tracked], [r.identity for r in base])
        self.assertFalse(tracked[-1].safe)
        self.assertEqual(tracked[-1].text, "")

    def test_upward_translation_retains_two_identical_messages_and_new_tail(self):
        rows = move(self.old, -60) + (candidate("新的内容", 480),)
        self.assertEqual(unique_shift(self.old_evidence, evidence_for(rows)), -60)
        tracked, added = self.tracker.update(rows, evidence_for(rows))
        self.assertEqual(self.tracker.last_shift, -60)
        self.assertEqual(len(added), 1)
        self.assertEqual([r.identity for r in tracked[:3]], [r.identity for r in self.baseline])
        self.assertNotEqual(tracked[1].identity, tracked[2].identity)

    def test_odd_pixel_translation_ignores_crop_fingerprint_recut_not_text_only(self):
        rows = move(self.old, -37) + (candidate("新来信", 500),)
        pixels = evidence_for(rows)
        # Bubble extraction uses a downsampled grid, so old crop hashes can
        # differ despite a proven full interior-pixel translation.
        rows = tuple(replace(r, fingerprint="different-crop-"+str(i)) if i < 3 else r for i, r in enumerate(rows))
        tracked, added = self.tracker.update(rows, pixels)
        self.assertEqual(self.tracker.last_shift, -37)
        self.assertEqual(len(added), 1)
        self.assertEqual(tracked[2].identity, self.baseline[2].identity)

    def test_changed_old_ocr_segmentation_is_not_new_and_unknown_not_promoted(self):
        old = (candidate("", 70, "complex", safe=False), candidate("锚点", 400))
        tracker = PixelSequenceTracker()
        base, _ = tracker.update(old, evidence_for(old))
        actual = move(old, -30) + (candidate("新文字", 500),)
        # The same old pixels are now split into additional OCR boxes.
        raw = (candidate("假想旧片段", 40, "fragment"), replace(candidate("", 65, "fragment2", safe=False), box=Box(70,65,250,90)), actual[1], actual[2])
        tracked, added = tracker.update(raw, evidence_for(actual))
        self.assertEqual(len(added), 1)
        self.assertEqual(tracked[0].identity, base[0].identity)
        self.assertFalse(tracked[0].safe)

    def test_changed_ocr_text_downgrades_old_candidate_instead_of_reusing_it(self):
        rows = (replace(self.old[0], text="识字不一致"),) + self.old[1:]
        tracked, added = self.tracker.update(rows, self.old_evidence)
        self.assertFalse(added)
        self.assertFalse(tracked[0].safe)
        self.assertEqual(tracked[0].text, "")

    def test_clipped_old_prefix_removed_without_replaying_it(self):
        actual = move(self.old, -120) + (candidate("新尾部", 480),)
        visible = tuple(r for r in actual if r.box.top >= 0)
        # Old head is partly clipped; pixel evidence includes that clipped
        # content, but only complete old items may be displayed for review.
        tracked, added = self.tracker.update(visible, evidence_for(actual))
        self.assertEqual(len(added), 1)
        self.assertNotIn(self.baseline[0].identity, [r.identity for r in tracked])
        self.assertIn(self.baseline[-1].identity, [r.identity for r in tracked])

    def test_pure_periodic_repeated_bubbles_without_unique_anchor_pause(self):
        old = tuple(candidate("相同", y, "same") for y in (100, 240, 380))
        actual = move(old, -140) + (candidate("相同", 380, "same"), candidate("相同", 520, "same"))
        new = tuple(row for row in actual if row.box.top >= 0)
        tracker = PixelSequenceTracker()
        tracker.update(old, evidence_for(old))
        with self.assertRaisesRegex(VisualPause, "唯一"):
            tracker.update(new, evidence_for(actual))

    def test_two_identical_new_messages_have_distinct_occurrences_after_scroll(self):
        actual = move(self.old, -100) + (candidate("一样", 440, "new-same"), candidate("一样", 530, "new-same"))
        tracked, added = self.tracker.update(actual, evidence_for(actual))
        self.assertEqual(len(added), 2)
        self.assertNotEqual(added[0].identity, added[1].identity)

    def test_same_text_but_pixel_mutation_in_overlap_pauses(self):
        image = image_for(self.old)
        image.putpixel((100, 420), (1, 2, 3))
        changed = pixel_evidence(image)
        image.close()
        with self.assertRaises(VisualPause):
            self.tracker.update(self.old, changed)

    def test_reflow_horizontal_motion_background_resize_missing_anchor_pause(self):
        for ev in (replace(self.old_evidence, width=WIDTH+1),
                   evidence_for((candidate("完全不同", 120),)),
                   replace(self.old_evidence, background=b"x"*32)):
            with self.assertRaises(VisualPause):
                self.tracker.update(self.old, ev)

    def test_history_direction_and_cross_boundary_pause(self):
        rows = move(self.old, 30)
        with self.assertRaisesRegex(VisualPause, "历史"):
            self.tracker.update(rows, evidence_for(rows))
        rows = self.old + (candidate("跨边界", 440),)
        # A candidate straddling the proven old/new boundary cannot be new.
        with self.assertRaises(VisualPause):
            self.tracker.update(rows, evidence_for(rows))

    def test_unknown_new_tail_is_never_promoted(self):
        rows = self.old + (candidate("", 500, "media", safe=False),)
        tracked, added = self.tracker.update(rows, evidence_for(rows))
        self.assertEqual(len(added), 1)
        self.assertFalse(added[0].safe)

    def test_unclassified_new_tail_pixels_block_even_without_ocr_candidate(self):
        image = image_for(self.old + (candidate("漏识别", 500),))
        ev = pixel_evidence(image)
        image.close()
        with self.assertRaisesRegex(VisualPause, "未归类像素"):
            self.tracker.update(self.old, ev)

    def test_new_verified_timestamp_can_be_excluded_as_system_not_message(self):
        stamp = candidate("时间", 500)
        ev = evidence_for(self.old + (stamp,))
        ev = replace(ev, systems=(stamp.box,))
        tracked, added = self.tracker.update(self.old, ev)
        self.assertFalse(added)

    def test_no_evidence_or_malformed_evidence_never_falls_back_to_text(self):
        for ev in (None, replace(self.old_evidence, rows=(b"bad",)), replace(self.old_evidence, systems=(Box(-1,0,50,20),))):
            with self.assertRaises(VisualPause):
                self.tracker.update(self.old, ev)

    def test_blank_view_is_not_a_temporal_anchor(self):
        empty = evidence_for(())
        tracker = PixelSequenceTracker()
        tracker.update((), empty)
        with self.assertRaises(VisualPause):
            tracker.update((candidate("来信", 400),), evidence_for((candidate("来信", 400),)))

    def test_unchanged_blank_pixels_cannot_create_ocr_message(self):
        empty = evidence_for(())
        tracker = PixelSequenceTracker()
        tracker.update((), empty)
        tracked, added = tracker.update((candidate("背景误识字", 400),), empty)
        self.assertFalse(tracked)
        self.assertFalse(added)

    def test_diagnostics_count_exact_zero_matches_without_relaxing_one_changed_row(self):
        image = image_for(self.old)
        image.putpixel((100,420), (1,2,3))
        ev = pixel_evidence(image)
        image.close()
        with self.assertRaises(VisualPause):
            self.tracker.update(self.old, ev)
        report = safe_alignment_diagnostic(self.tracker.diagnostic)
        self.assertEqual(report["result"], "no_exact_match")
        self.assertEqual(report["exact_matches"], 0)
        self.assertEqual(report["closest_changed_rows"], 1)
        self.assertEqual(report["closest_shift_y"], 0)
        self.assertIs(self.tracker.evidence, self.old_evidence)

    def test_diagnostics_distinguish_periodic_ambiguity_from_no_match(self):
        old = tuple(candidate("相同", y, "same") for y in (100,240,380))
        actual = move(old,-140) + (candidate("相同",380,"same"), candidate("相同",520,"same"))
        tracker = PixelSequenceTracker()
        tracker.update(old, evidence_for(old))
        with self.assertRaises(VisualPause):
            tracker.update(tuple(r for r in actual if r.box.top >= 0), evidence_for(actual))
        self.assertEqual(tracker.diagnostic["result"], "ambiguous")
        self.assertGreater(tracker.diagnostic["exact_matches"], 1)

    def test_diagnostics_do_not_change_success_or_history_direction_guards(self):
        rows = move(self.old,-60) + (candidate("新来信",480),)
        tracked, added = self.tracker.update(rows,evidence_for(rows))
        report = safe_alignment_diagnostic(self.tracker.diagnostic)
        self.assertEqual(report["result"], "unique")
        self.assertEqual(report["matched_shift_y"], -60)
        self.assertEqual(report["pipeline_result"], "complete")
        self.assertEqual(len(added), 1)
        with self.assertRaises(VisualPause):
            unique_shift(self.old_evidence, evidence_for(move(self.old,30)), diagnostic=report)
        self.assertEqual(report["result"], "history")

    def test_diagnostic_allowlist_cannot_serialize_hashes_text_identities_or_bad_types(self):
        unsafe = dict(result="no_exact_match", version=1, exact_matches=0,
            same_background=True, rows=self.old_evidence.rows, background=self.old_evidence.background,
            chat="private text", profile="private identity", closest_shift_y="private text",
            eligible_shifts=True, old_width=10**9, pipeline_result="private text")
        clean = safe_alignment_diagnostic(unsafe)
        self.assertEqual(clean, dict(result="no_exact_match",version=1,exact_matches=0,same_background=True))
        self.assertEqual(safe_alignment_diagnostic({"result":[], "pipeline_result":{}}), {})
        self.assertEqual(safe_alignment_diagnostic(None), {})


class PixelReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = new_profile("合成目标")
        self.cfg = {"selected": self.profile["id"], "provider":"openai", "cloud_provider":"openai", "cloud_consent":True}
        self.cal = Calibration(WindowBinding(1,2,"synthetic","synthetic",1000,900,96), self.profile["id"], self.profile["name"], Box(100,10,400,60), Box(100,80,600,680), True)
        self.session = ReviewSession()
        self.session.begin(self.cal, self.profile, self.cfg)
        self.rows = (candidate("旧基线", 100), candidate("旧基线2", 400))
        self.session.set_baseline(self.cal, self.profile, self.cfg, self.profile["name"], self.rows,
            evidence=evidence_for(self.rows), bottom_confirmed=True)

    def observe(self, rows):
        return self.session.observe(self.cal, self.profile, self.cfg, self.profile["name"], rows, evidence=evidence_for(rows))

    def test_empty_refresh_with_old_tail_unknown_stays_ready_without_pending_or_draft(self):
        raw = self.rows + (candidate("",500,"old-bottom-unknown",safe=False),)
        evidence = evidence_for(self.rows)
        self.session.set_baseline(self.cal,self.profile,self.cfg,self.profile["name"],raw,
                                  evidence=evidence,bottom_confirmed=True)
        base_ids = tuple(row.identity for row in self.session.rows)
        self.session.reading()
        self.session.observe(self.cal,self.profile,self.cfg,self.profile["name"],raw,evidence=evidence)
        self.assertTrue(self.session.ready)
        self.assertFalse(self.session.needs_baseline)
        self.assertFalse(self.session.pending())
        self.assertFalse(self.session.draft)
        self.assertFalse(self.session.rows[-1].safe)
        self.assertEqual(tuple(row.identity for row in self.session.rows),base_ids)
        with self.assertRaises(VisualPause):
            self.session.prepare()

    def test_new_safe_ocr_box_without_pixel_change_cannot_prepare_reply(self):
        raw = self.rows + (candidate("没有新像素的识字候选",500),)
        self.session.reading()
        self.session.observe(self.cal,self.profile,self.cfg,self.profile["name"],raw,
                             evidence=evidence_for(self.rows))
        self.assertTrue(self.session.ready)
        self.assertEqual(len(self.session.rows),len(self.rows))
        self.assertFalse(self.session.pending())
        with self.assertRaises(VisualPause):
            self.session.prepare()

    def test_scroll_new_message_only_after_human_review_can_prepare(self):
        rows = move(self.rows, -50) + (candidate("新增来信", 480),)
        self.observe(rows)
        self.assertEqual(len(self.session.pending()), 1)
        with self.assertRaises(VisualPause):
            self.session.prepare()
        self.session.confirm(self.session.pending()[0].identity, "人工核对的新来信")
        request = self.session.prepare()
        self.assertEqual(request.text, "人工核对的新来信")
        self.assertFalse(request.context)
        self.session.authorize(request)
        self.session.pause()
        self.assertFalse(self.session.accepts(request.token))

    def test_new_media_or_missing_proof_blocks_draft_and_preserves_pause(self):
        rows = self.rows + (candidate("",500,"media",safe=False),)
        self.observe(rows)
        self.assertFalse(self.session.ready)
        with self.assertRaises(VisualPause):
            self.session.prepare()
        with self.assertRaises(VisualPause):
            self.session.observe(self.cal, self.profile, self.cfg, self.profile["name"], rows)
        self.assertTrue(self.session.needs_baseline)

    def test_pixel_mode_cannot_be_enabled_without_local_proof(self):
        with self.assertRaises(VisualPause):
            self.session.set_baseline(self.cal, self.profile, self.cfg, self.profile["name"], self.rows, bottom_confirmed=True)

    def test_profile_title_and_config_guards_still_apply_with_evidence(self):
        with self.assertRaises(VisualPause):
            self.session.observe(self.cal,self.profile,self.cfg,"另一位",self.rows,evidence=evidence_for(self.rows))
        self.assertFalse(self.session.ready)

    def test_new_direction_unknown_and_manual_reply_still_pause_or_invalidate(self):
        rows = move(self.rows, -50) + (candidate("方向不明", 480, direction="unknown"),)
        self.observe(rows)
        self.assertFalse(self.session.ready)
        self.session.set_baseline(self.cal,self.profile,self.cfg,self.profile["name"],self.rows,
            evidence=evidence_for(self.rows),bottom_confirmed=True)
        rows = move(self.rows,-50) + (candidate("新文字",480),)
        self.observe(rows)
        self.session.confirm(self.session.pending()[0].identity,"新文字")
        request=self.session.prepare()
        self.session.authorize(request)
        self.session.manual_reply()
        self.assertFalse(self.session.accepts(request.token))
        self.assertFalse(self.session.pending())

    def test_failed_alignment_then_repoll_never_hides_failure_or_shows_stale_rows(self):
        self.session.confirm(self.session.rows[0].identity, "已核对旧文字")
        self.session.edit_draft("旧草稿")
        changed = (candidate("不同旧像素",100), self.rows[-1])
        with self.assertRaises(VisualPause):
            self.observe(changed)
        reason = self.session.rebaseline_reason
        report = self.session.sync_diagnostic.copy()
        self.assertTrue(reason)
        self.assertFalse(self.session.rows)
        self.assertFalse(self.session.reviewed)
        self.assertFalse(self.session.draft)
        self.assertTrue(self.session.needs_baseline)
        self.session.reading()
        self.observe(self.rows + (candidate("下一次快照文字",500),))
        self.assertTrue(self.session.ready)  # Explicit rebaseline may be offered.
        self.assertTrue(self.session.needs_baseline)
        self.assertIn(reason, self.session.notice)
        self.assertNotEqual(self.session.notice, "本轮快照可设为基线；尚未同步任何新消息。")
        self.assertIn("旧列表已清空", self.session.notice)
        self.assertFalse(self.session.rows)
        self.assertFalse(self.session.pending())
        self.assertEqual(self.session.sync_diagnostic, report)
        with self.assertRaises(VisualPause):
            self.session.prepare()
        self.session.set_baseline(self.cal,self.profile,self.cfg,self.profile["name"],self.rows,
            evidence=evidence_for(self.rows),bottom_confirmed=True)
        self.assertFalse(self.session.rebaseline_reason)
        self.assertEqual(len(self.session.rows), 2)
        self.assertFalse(self.session.pending())

    def test_explicit_bottom_reset_clears_old_rows_and_reviews(self):
        self.session.confirm(self.session.rows[0].identity,"已核对")
        self.session.require_rebaseline("合成底部确认改变")
        self.assertTrue(self.session.needs_baseline)
        self.assertFalse(self.session.ready)
        self.assertFalse(self.session.rows)
        self.assertFalse(self.session.reviewed)
        self.observe(self.rows)
        self.assertIn("合成底部确认改变",self.session.notice)

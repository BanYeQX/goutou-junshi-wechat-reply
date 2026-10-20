"""Offline tests for the hosting state machine. No I/O, no WeChat."""
import unittest

from hosting_loop import (
    MERGE_MAX_MESSAGES,
    MERGE_QUIET_S,
    MERGE_TOTAL_S,
    Decision,
    Loop,
    Observation,
    State,
)

INCOMING = True
OUTGOING = False


def frame(now, text=None, incoming=None, change=0.0, **kw):
    return Observation(now=now, change=change, text=text, incoming=incoming, **kw)


class BaselineTests(unittest.TestCase):
    def test_first_frame_only_baselines(self):
        loop = Loop()
        decision, detail = loop.observe(frame(0.0, "很久以前的旧消息", INCOMING, 0.9))
        self.assertIs(decision, Decision.BASELINE)
        self.assertEqual(detail, "first_frame")
        self.assertIsNone(loop.pending)

    def test_history_after_baseline_is_not_answered(self):
        loop = Loop()
        loop.observe(frame(0.0))
        decision, _ = loop.observe(frame(1.0, "旧的", INCOMING, 0.0))
        self.assertIs(decision, Decision.WAIT)
        self.assertIsNone(loop.pending)


class MergeWindowTests(unittest.TestCase):
    def test_quiet_opens_the_window(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        decision, detail = loop.observe(frame(1.0 + MERGE_QUIET_S, text=None))
        self.assertIs(decision, Decision.GENERATE)
        self.assertEqual(detail, "在吗")

    def test_quiet_does_not_open_early(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        decision, _ = loop.observe(frame(1.0 + MERGE_QUIET_S / 2))
        self.assertIs(decision, Decision.WAIT)

    def test_three_messages_close_it(self):
        loop = Loop()
        loop.observe(frame(0.0))
        for i in range(MERGE_MAX_MESSAGES):
            loop.observe(frame(1.0 + i * 0.1, f"消息{i}", INCOMING, 0.5))
        decision, detail = loop.observe(frame(1.0 + MERGE_MAX_MESSAGES * 0.1))
        self.assertIs(decision, Decision.GENERATE)
        self.assertEqual(detail, f"消息{MERGE_MAX_MESSAGES - 1}")

    def test_total_wait_caps_a_fast_typer(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "第一条", INCOMING, 0.5))
        loop.observe(frame(1.0 + MERGE_TOTAL_S, "仍在打字", INCOMING, 0.5))
        decision, _ = loop.observe(frame(1.0 + MERGE_TOTAL_S, text=None))
        self.assertIs(decision, Decision.GENERATE)


class SupersedeTests(unittest.TestCase):
    def test_new_message_during_generation_discards_the_result(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "出来玩吗", INCOMING, 0.5))
        decision, _ = loop.observe(frame(2.5))
        self.assertIs(decision, Decision.GENERATE)

        loop.observe(frame(2.6, "算了", INCOMING, 0.5))   # arrives mid-generation
        decision, detail = loop.generation_finished("好，马上出门")
        self.assertIs(decision, Decision.DISCARD)
        self.assertEqual(detail, "superseded_during_generation")

    def test_new_message_before_commit_kills_the_staged_reply(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished("在的")

        loop.observe(frame(2.8, "别发了", INCOMING, 0.5))  # before the commit
        decision, detail = loop.readback_finished(True, "exact_clipboard_text_match")
        self.assertIs(decision, Decision.DISCARD)
        self.assertEqual(detail, "nothing_staged")

    def test_staged_reply_survives_unrelated_frames(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished("在的")
        loop.observe(frame(3.0))
        decision, _ = loop.readback_finished(True, "exact_clipboard_text_match")
        self.assertIs(decision, Decision.COMMIT)


class CommitGateTests(unittest.TestCase):
    def test_readback_mismatch_pauses_and_never_commits(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished("在的")
        decision, detail = loop.readback_finished(False, "exact_text_mismatch")
        self.assertIs(decision, Decision.PAUSE)
        self.assertTrue(detail.startswith("readback_failed"))

    def test_unknown_after_commit_freezes(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished("在的")
        loop.readback_finished(True, "match")
        decision, detail = loop.commit_finished(delivered=False)
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "unknown_after_commit")
        self.assertIs(loop.state, State.PAUSED)

    def test_paused_loop_only_waits(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.pause("manual")
        decision, _ = loop.observe(frame(1.0, "新消息", INCOMING, 0.9))
        self.assertIs(decision, Decision.WAIT)

    def test_resume_requires_a_new_baseline(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.pause("manual")
        loop.resume()
        self.assertIs(loop.state, State.RUNNING)
        decision, _ = loop.observe(frame(5.0, "积压的旧消息", INCOMING, 0.9))
        self.assertIsNot(decision, Decision.GENERATE)


class TakeoverTests(unittest.TestCase):
    def test_outgoing_bubble_means_the_human_took_over(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        decision, detail = loop.observe(frame(1.2, outgoing=True))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "user_took_over")
        self.assertIsNone(loop.pending)

    def test_outgoing_discards_a_staged_reply(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished("在的")
        loop.observe(frame(2.7, outgoing=True))
        decision, detail = loop.readback_finished(True, "match")
        self.assertIs(decision, Decision.DISCARD)


class OwnReplyEchoTests(unittest.TestCase):
    def _committed(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished("收到")
        loop.readback_finished(True, "match")
        loop.commit_finished(True, now=3.0)
        return loop

    def test_our_own_bubble_does_not_pause(self):
        loop = self._committed()
        decision, detail = loop.observe(frame(3.5, "收到", OUTGOING, 0.4, outgoing=True))
        self.assertIs(decision, Decision.WAIT)
        self.assertEqual(detail, "own_reply_echo")
        self.assertIs(loop.state, State.RUNNING)

    def test_a_different_outgoing_bubble_is_a_takeover(self):
        loop = self._committed()
        decision, detail = loop.observe(frame(3.5, "我自己回的", OUTGOING, 0.4, outgoing=True))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "user_took_over")

    def test_the_same_text_later_is_a_takeover(self):
        loop = self._committed()
        decision, detail = loop.observe(frame(3.0 + 60, "收到", OUTGOING, 0.4, outgoing=True))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "user_took_over")


class NewestUnchangedTests(unittest.TestCase):
    """Live 14:2x: a repaint re-read an old bubble as a new event."""

    def _committed(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "你好棒哦", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished("那就这么说定了，周末见。")
        loop.readback_finished(True, "match")
        loop.commit_finished(True, now=3.0)
        return loop

    def test_repaint_of_our_old_reply_after_the_echo_window_is_not_a_takeover(self):
        loop = self._committed()
        loop.observe(frame(3.5, "那就这么说定了，周末见。", OUTGOING, 0.4, outgoing=True))
        decision, detail = loop.observe(
            frame(3.0 + 45, "那就这么说定了，周末见", OUTGOING, 0.01, outgoing=True))
        self.assertIs(decision, Decision.WAIT)
        self.assertEqual(detail, "newest_unchanged")
        self.assertIs(loop.state, State.RUNNING)

    def test_restart_does_not_read_our_old_reply_as_a_takeover(self):
        loop = Loop()
        loop.note_baseline("那就这么说定了，周末见。", OUTGOING)
        loop.observe(frame(0.0))
        decision, detail = loop.observe(
            frame(1.0, "那就这么说定了，周末见。", OUTGOING, 0.01, outgoing=True))
        self.assertIs(decision, Decision.WAIT)
        self.assertEqual(detail, "newest_unchanged")

    def test_restart_does_not_answer_the_old_incoming_message(self):
        loop = Loop()
        loop.note_baseline("你好棒哦", INCOMING)
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "你好棒哦", INCOMING, 0.01))
        decision, _ = loop.observe(frame(1.0 + MERGE_TOTAL_S + 1))
        self.assertIsNot(decision, Decision.GENERATE)
        self.assertIsNone(loop.pending)

    def test_a_new_incoming_message_after_restart_is_answered(self):
        loop = Loop()
        loop.note_baseline("你好棒哦", INCOMING)
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "晚安", INCOMING, 0.2))
        decision, detail = loop.observe(frame(1.0 + MERGE_QUIET_S))
        self.assertIs(decision, Decision.GENERATE)
        self.assertEqual(detail, "晚安")

    def test_a_different_outgoing_bubble_after_restart_is_still_a_takeover(self):
        loop = Loop()
        loop.note_baseline("那就这么说定了，周末见。", OUTGOING)
        loop.observe(frame(0.0))
        decision, detail = loop.observe(frame(1.0, "我自己来回吧", OUTGOING, 0.2, outgoing=True))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "user_took_over")

    def test_an_unreadable_outgoing_bubble_is_still_a_takeover(self):
        loop = Loop()
        loop.note_baseline("那就这么说定了", OUTGOING)
        loop.observe(frame(0.0))
        decision, detail = loop.observe(frame(1.0, outgoing=True))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "user_took_over")


class ModelWaitTests(unittest.TestCase):
    """Live 15:3x: a "wait" answer left the message pending; it was regenerated
    every frame (eight model calls for one "okok")."""

    def generating(self):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "okok", INCOMING, 0.3))
        decision, _ = loop.observe(frame(1.0 + MERGE_QUIET_S))
        self.assertIs(decision, Decision.GENERATE)
        return loop

    def test_wait_handles_the_message_and_does_not_generate_again(self):
        loop = self.generating()
        decision, detail = loop.generation_skipped()
        self.assertEqual((decision, detail), (Decision.WAIT, "model_wait"))
        for i in range(10):
            decision, _ = loop.observe(frame(3.0 + i))
            self.assertIsNot(decision, Decision.GENERATE)

    def test_a_message_that_arrived_during_generation_is_still_answered(self):
        loop = self.generating()
        loop.observe(frame(2.1, "在吗", INCOMING, 0.3, above=["okok"]))
        decision, detail = loop.generation_skipped()
        self.assertEqual(detail, "superseded_during_generation")
        decision, detail = loop.observe(frame(2.1 + MERGE_QUIET_S))
        self.assertEqual((decision, detail), (Decision.GENERATE, "在吗"))


class RepeatedMessageTests(unittest.TestCase):
    """Live 15:2x: "嗯" right under an unanswered "嗯" was taken for a repaint."""

    def started(self):
        loop = Loop()
        loop.note_baseline("嗯", INCOMING, ["好吧"])          # run of 1
        loop.observe(frame(0.0))
        return loop

    def test_the_same_words_sent_again_are_a_new_message(self):
        loop = self.started()
        loop.observe(frame(1.0, "嗯", INCOMING, 0.2, above=["嗯", "好吧"]))
        decision, detail = loop.observe(frame(1.0 + MERGE_QUIET_S))
        self.assertEqual((decision, detail), (Decision.GENERATE, "嗯"))

    def test_a_repaint_of_the_same_bubble_is_still_not_a_message(self):
        loop = self.started()
        decision, detail = loop.observe(frame(1.0, "嗯", INCOMING, 0.01, above=["好吧"]))
        self.assertEqual((decision, detail), (Decision.WAIT, "newest_unchanged"))
        self.assertIsNone(loop.pending)

    def test_a_repaint_after_the_repeat_is_not_a_third_message(self):
        loop = self.started()
        loop.observe(frame(1.0, "嗯", INCOMING, 0.2, above=["嗯", "好吧"]))
        decision, detail = loop.observe(frame(1.3, "嗯", INCOMING, 0.01, above=["嗯", "好吧"]))
        self.assertEqual(detail, "newest_unchanged")
        self.assertEqual(loop.pending.count, 1)

    def test_one_misread_copy_does_not_make_the_next_read_a_new_message(self):
        loop = self.started()
        loop.observe(frame(1.0, "嗯", INCOMING, 0.2, above=["嗯", "好吧"]))   # run 2
        loop.observe(frame(1.2, "嗯", INCOMING, 0.01, above=["恩呢", "好吧"]))  # misread: run 1
        loop.observe(frame(1.4, "嗯", INCOMING, 0.01, above=["嗯", "好吧"]))    # back to 2
        self.assertEqual(loop.pending.count, 1)

    def test_without_a_look_above_the_old_text_only_rule_still_applies(self):
        loop = Loop()
        loop.note_baseline("嗯", INCOMING)
        loop.observe(frame(0.0))
        decision, detail = loop.observe(frame(1.0, "嗯", INCOMING, 0.2))
        self.assertEqual(detail, "newest_unchanged")


class OwnReplyEchoNormalisationTests(unittest.TestCase):
    """OCR of our own bubble is never byte-identical; that must not look like a takeover."""

    def _committed(self, text="可以啊，记得早点休息"):
        loop = Loop()
        loop.observe(frame(0.0))
        loop.observe(frame(1.0, "在吗", INCOMING, 0.5))
        loop.observe(frame(2.5))
        loop.generation_finished(text)
        loop.readback_finished(True, "match")
        loop.commit_finished(True, now=3.0)
        return loop

    def test_added_punctuation_is_still_ours(self):
        loop = self._committed()
        decision, detail = loop.observe(
            frame(3.5, "可以啊,记得早点休息。", OUTGOING, 0.4, outgoing=True))
        self.assertIs(decision, Decision.WAIT)
        self.assertEqual(detail, "own_reply_echo")

    def test_dropped_trailing_punctuation_is_still_ours(self):
        loop = self._committed()
        decision, detail = loop.observe(
            frame(3.5, "可以啊 记得早点休息", OUTGOING, 0.4, outgoing=True))
        self.assertIs(decision, Decision.WAIT)
        self.assertEqual(detail, "own_reply_echo")

    def test_a_real_human_reply_is_still_a_takeover(self):
        loop = self._committed()
        decision, detail = loop.observe(
            frame(3.5, "那你自己吃吧", OUTGOING, 0.4, outgoing=True))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "user_took_over")

    def test_a_heavily_truncated_read_is_not_treated_as_ours(self):
        loop = self._committed()
        decision, detail = loop.observe(
            frame(3.5, "可以啊", OUTGOING, 0.4, outgoing=True))
        # Only 3 of 14 characters: too little to claim. Refusing to guess is
        # safer than silently swallowing a human reply.
        self.assertIs(decision, Decision.PAUSE)


class GuardTests(unittest.TestCase):
    def test_geometry_change_pauses(self):
        loop = Loop()
        loop.observe(frame(0.0))
        decision, detail = loop.observe(frame(1.0, geometry_ok=False))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "geometry_changed")

    def test_title_mismatch_pauses(self):
        loop = Loop()
        loop.observe(frame(0.0))
        decision, detail = loop.observe(frame(1.0, title_ok=False))
        self.assertIs(decision, Decision.PAUSE)
        self.assertEqual(detail, "title_mismatch")

    def test_scrolling_holds_instead_of_pausing(self):
        # Changed design (scroll detection): scrolled up, nothing is an event,
        # but the loop is not stopped - it carries on once the bottom is back.
        loop = Loop()
        loop.observe(frame(0.0))
        decision, detail = loop.observe(frame(1.0, scrolling=True))
        self.assertIs(decision, Decision.WAIT)
        self.assertEqual(detail, "scrolled")
        self.assertIs(loop.state, State.RUNNING)


if __name__ == "__main__":
    unittest.main()

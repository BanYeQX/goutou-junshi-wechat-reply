"""Offline tests for the send ledger."""
import os
import tempfile
import unittest

from hosting_ledger import ACK, BEGIN, FINISH, Ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-test-")
        self.path = os.path.join(self.dir, "send-ledger.jsonl")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def test_empty_ledger_does_not_block(self):
        ledger = Ledger(self.path)
        self.assertFalse(ledger.blocks_start())
        self.assertEqual(ledger.unfinished(), [])

    def test_finished_commit_does_not_block(self):
        ledger = Ledger(self.path)
        commit_id = ledger.begin_commit("小雨", "收到")
        ledger.finish_commit(commit_id, delivered=True)
        self.assertFalse(ledger.blocks_start())

    def test_crash_between_begin_and_finish_blocks_start(self):
        ledger = Ledger(self.path)
        ledger.begin_commit("小雨", "收到")
        # no finish: the process died after Enter
        fresh = Ledger(self.path)          # what the next start would see
        self.assertTrue(fresh.blocks_start())
        self.assertIn("收到", fresh.describe_unfinished())

    def test_acknowledgement_unblocks_without_resending(self):
        ledger = Ledger(self.path)
        commit_id = ledger.begin_commit("小雨", "收到")
        ledger.acknowledge(commit_id, note="user confirmed it was sent")
        fresh = Ledger(self.path)
        self.assertFalse(fresh.blocks_start())

    def test_unconfirmed_commit_blocks_until_acknowledged(self):
        ledger = Ledger(self.path)
        commit_id = ledger.begin_commit("小雨", "收到")
        ledger.finish_commit(commit_id, delivered=False, note="unknown")
        # "Pressed Enter, could not confirm" is an UNKNOWN: it must freeze the
        # next start too, not just the current process.
        self.assertTrue(ledger.blocks_start())
        ledger.acknowledge(commit_id, note="user confirmed")
        self.assertFalse(Ledger(self.path).blocks_start())

    def test_recorded_failure_is_still_visible_in_history(self):
        ledger = Ledger(self.path)
        commit_id = ledger.begin_commit("小雨", "收到")
        ledger.finish_commit(commit_id, delivered=False, note="unknown")
        kinds = [entry.kind for entry in ledger.read()]
        self.assertEqual(kinds, [BEGIN, FINISH])

    def test_two_commits_are_tracked_independently(self):
        ledger = Ledger(self.path)
        first = ledger.begin_commit("小雨", "一")
        second = ledger.begin_commit("小雨", "二")
        ledger.finish_commit(first, delivered=True)
        unfinished = Ledger(self.path).unfinished()
        self.assertEqual([entry.commit_id for entry in unfinished], [second])

    def test_delivered_commit_never_blocks(self):
        ledger = Ledger(self.path)
        for _ in range(3):
            commit_id = ledger.begin_commit("小雨", "收到")
            ledger.finish_commit(commit_id, delivered=True)
        self.assertFalse(ledger.blocks_start())

    def test_half_written_line_is_ignored(self):
        ledger = Ledger(self.path)
        commit_id = ledger.begin_commit("小雨", "收到")
        ledger.finish_commit(commit_id, delivered=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write('{"kind": "commit_begin", "commit_id": "trunc')  # torn write
        self.assertFalse(Ledger(self.path).blocks_start())
        self.assertEqual(len(Ledger(self.path).read()), 2)

    def test_ledger_records_target_and_text(self):
        ledger = Ledger(self.path)
        ledger.begin_commit("小雨", "晚上吃啥")
        entry = ledger.read()[0]
        self.assertEqual(entry.target, "小雨")
        self.assertEqual(entry.text, "晚上吃啥")
        self.assertEqual(entry.kind, BEGIN)
        self.assertEqual(Ledger(self.path).read()[0].kind, BEGIN)
        self.assertEqual(ACK, "commit_ack")
        self.assertEqual(FINISH, "commit_finish")


if __name__ == "__main__":
    unittest.main()

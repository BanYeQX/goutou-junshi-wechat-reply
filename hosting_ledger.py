"""Persistent send ledger.

One line per attempt. The rule that matters: the COMMITTING entry is written
*discard-safe* before Enter, and only cleared by a recorded outcome. If the
process dies between those two points, the next start sees an unfinished entry
and must refuse to run until a human acknowledges it - never auto-retry.

Append-only on purpose: rewriting a JSON file would lose the crash window.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import uuid

BEGIN = "commit_begin"
FINISH = "commit_finish"
ACK = "commit_ack"


@dataclass(frozen=True)
class Entry:
    kind: str
    commit_id: str
    at: str
    target: str = ""
    text: str = ""
    delivered: bool | None = None
    note: str = ""


class Ledger:
    def __init__(self, path):
        self.path = path

    # ------------------------------------------------------------------ write

    def _append(self, entry: Entry) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def begin_commit(self, target, text, now=None) -> str:
        commit_id = uuid.uuid4().hex[:12]
        self._append(Entry(BEGIN, commit_id, _stamp(now), target=target, text=text))
        return commit_id

    def finish_commit(self, commit_id, delivered, note="", now=None) -> None:
        self._append(Entry(FINISH, commit_id, _stamp(now), delivered=bool(delivered),
                           note=note))

    def acknowledge(self, commit_id, note="", now=None) -> None:
        self._append(Entry(ACK, commit_id, _stamp(now), note=note))

    # ------------------------------------------------------------------- read

    def read(self) -> list:
        if not os.path.exists(self.path):
            return []
        entries = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(Entry(**json.loads(line)))
                except (ValueError, TypeError):
                    # A half-written last line is exactly the crash case we care
                    # about; keep going, the unfinished scan will catch the rest.
                    continue
        return entries

    def unfinished(self) -> list:
        """Commits that still need a human decision.

        Two cases block a start: an entry with no outcome at all (the process
        died between Enter and the check) and an entry whose outcome is
        "not delivered" (we pressed Enter and could not confirm). Neither may
        be retried automatically, and neither is cleared by restarting.
        """
        entries = self.read()
        acknowledged = {e.commit_id for e in entries if e.kind == ACK}
        outcomes = {e.commit_id: e for e in entries if e.kind == FINISH}
        blocking = []
        for entry in entries:
            if entry.kind != BEGIN or entry.commit_id in acknowledged:
                continue
            outcome = outcomes.get(entry.commit_id)
            if outcome is None or outcome.delivered is False:
                blocking.append(entry)
        return blocking

    def blocks_start(self) -> bool:
        return bool(self.unfinished())

    def describe_unfinished(self) -> str:
        rows = []
        for entry in self.unfinished():
            rows.append(f"{entry.at} {entry.commit_id} target={entry.target} "
                        f"text={entry.text[:40]!r}")
        return "; ".join(rows)


def _stamp(now=None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds")

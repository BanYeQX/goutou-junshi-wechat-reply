"""Hosting loop: the state machine that decides what to do with each frame.

This module holds no I/O. It takes an Observation and returns a Decision, so
every rule below is testable offline. The runner is responsible for capturing,
calling the model, pasting, pressing Enter and writing the ledger entry.

Rules encoded here (each one has a test):

  * the first frame only establishes a baseline - history is never answered;
  * one in-flight task per contact; a newer message supersedes an older pending
    text instead of replying to both;
  * a pending text is generated only after a merge window closes
    (1 s quiet, 3 distinct messages, or 5 s total - whichever comes first);
  * an outgoing bubble of our own means the human took over: discard and pause;
  * any new incoming message invalidates a staged reply *until the commit*;
  * a commit is refused unless the readback matched and nothing changed since;
  * geometry/title mismatch or an unfinished ledger entry pauses everything;
  * while the message area is scrolled away from the bottom, or the newest
    block is not a continuation of the last one seen, nothing is read as an
    event: the loop holds (WAIT) until the bottom is back.

PAUSE is sticky: only an explicit resume clears it, and resume rebuilds the
baseline. A hold is not a pause: it ends by itself on the first changed frame
at the bottom whose newest block is the known one or follows it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

MERGE_QUIET_S = 1.0
MERGE_TOTAL_S = 5.0
MERGE_MAX_MESSAGES = 3
OWN_REPLY_WINDOW_S = 30.0

_PUNCTUATION = set(" \t\r\n，。！？、；：,.!?;:\"'“”‘’…—-~（）()[]【】<>《》")


def _skeleton(text: str) -> str:
    """Lower-case and drop whitespace/punctuation, for identity comparison only."""
    return "".join(ch for ch in (text or "").lower() if ch not in _PUNCTUATION)


def _same_text(a: str, b: str) -> bool:
    """Same message as far as OCR can tell: equal skeletons, or one read is a
    near-complete (>= 80%) piece of the other. Identity only, never correctness."""
    seen, sent = _skeleton(a), _skeleton(b)
    if not seen or not sent:
        return False
    if seen == sent:
        return True
    shorter, longer = sorted((seen, sent), key=len)
    return shorter in longer and len(shorter) >= 0.8 * len(longer)


class Decision(str, Enum):
    BASELINE = "BASELINE"
    WAIT = "WAIT"
    GENERATE = "GENERATE"
    DISCARD = "DISCARD"
    COMMIT = "COMMIT"
    PAUSE = "PAUSE"


class State(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class Observation:
    now: float
    change: float = 0.0            # message-area change ratio this frame
    text: str | None = None        # newest incoming text, None if unreadable
    incoming: bool | None = None   # direction of that text block
    outgoing: bool = False         # an outgoing bubble appeared
    geometry_ok: bool = True
    title_ok: bool = True
    scrolling: bool = False
    # Texts right above the newest block, nearest first: a sequence or a
    # zero-argument callable returning an iterable. None = reader did not check.
    above: object = None


class _Above:
    """The texts right above the newest block, nearest first, each read once.

    The reader produces them lazily (every one is an OCR call); this keeps what
    was already read so the continuity check and the run count share it.
    """

    def __init__(self, source):
        self.unknown = source is None      # the reader did not look above
        self._source = source
        self._items = []
        self._iter = None
        self._done = source is None

    def __iter__(self):
        index = 0
        while True:
            if index < len(self._items):
                yield self._items[index]
                index += 1
                continue
            if self._done:
                return
            if self._iter is None:
                self._iter = iter(self._source() if callable(self._source) else self._source)
            try:
                self._items.append(next(self._iter))
            except StopIteration:
                self._done = True

    def run_of(self, text) -> int | None:
        """Blocks in a row, ending at the newest, that read as `text`."""
        if self._source is None:
            return None
        run = 1
        for seen in self:
            if not _same_text(seen, text):
                break
            run += 1
        return run


@dataclass
class _Pending:
    text: str
    first_seen: float
    last_seen: float
    count: int = 1


@dataclass
class Loop:
    """Pure decision logic. Construct, then call observe() for every frame."""
    state: State = State.IDLE
    pause_reason: str = ""
    pending: _Pending | None = None
    staged_text: str | None = None
    version: int = 0               # bumped by every accepted incoming message
    staged_version: int = -1
    last_outgoing_seen: bool = False
    last_commit_text: str = ""
    last_commit_at: float = -1e9
    # The newest block last seen in the message area (baseline included). A
    # changed frame whose newest block is still this one is a repaint, not an
    # event: live 14:2x a hover/redraw re-read our own 30-s-old reply as
    # "the human took over", and after a restart the same old reply paused the
    # loop right after its baseline.
    newest_text: str = ""
    newest_incoming: bool | None = None
    # How many blocks in a row, ending at the newest, carry that same text
    # (None = not counted). A repaint keeps it; the same words sent again push
    # the old copy up and add one. Live 15:2x: the contact sent "嗯" right
    # under an unanswered "嗯", and comparing text alone called it a repaint.
    newest_run: int | None = None
    # Why events are held right now ("scrolled" / "not_continuous"), "" if not.
    held: str = ""
    decisions: list = field(default_factory=list)

    # ------------------------------------------------------------------ public

    def observe(self, obs: Observation) -> tuple[Decision, str]:
        """Return (decision, detail). detail carries the reason or the text."""
        if self.state is State.IDLE:
            self.state = State.RUNNING
            return self._emit(Decision.BASELINE, "first_frame")

        if self.state is State.PAUSED:
            return self._emit(Decision.WAIT, f"paused:{self.pause_reason}")

        if not obs.geometry_ok:
            return self._pause("geometry_changed")
        if not obs.title_ok:
            return self._pause("title_mismatch")

        if obs.scrolling:
            # Scrolled up, the bottom-most visible block is history: an old
            # incoming message would be answered, an old own bubble would read
            # as a takeover. Nothing here is an event, and the known newest
            # block is not overwritten. A pending message stays pending: it
            # arrived at the bottom, and is answered once the bottom is back.
            return self._hold("scrolled")

        if obs.text and obs.incoming is not None:
            above = _Above(obs.above)
            same = (obs.incoming is self.newest_incoming
                    and _same_text(obs.text, self.newest_text))
            if same:
                run = above.run_of(obs.text)
                if run is None or self.newest_run is None or run <= self.newest_run:
                    # Same bubble as before, only the pixels around it moved.
                    # Keep the highest run seen, so one misread copy above does
                    # not make the next correct read look like a new message.
                    if run is not None:
                        self.newest_run = max(run, self.newest_run or 0)
                    self.newest_text = obs.text
                    self.held = ""
                    return self._emit(Decision.WAIT, "newest_unchanged")
                # One more copy of the same words than before: sent again. The
                # previous copy is right above it, so this continues by itself.
            elif not self._continues(above):
                # A different newest block, but the one we knew is not right
                # above it: a scroll that happened to end at the bottom padding,
                # or more new blocks than the reader looks back over. Hold.
                return self._hold("not_continuous")
            self.newest_text, self.newest_incoming = obs.text, obs.incoming
            self.newest_run = above.run_of(obs.text)
            self.held = ""
        elif self.held:
            # Unchanged frames are not re-read; stay held until one that is.
            return self._emit(Decision.WAIT, self.held)

        if obs.outgoing:
            if self._is_own_reply(obs):
                # Our own bubble, right after our own commit. Not a takeover.
                return self._emit(Decision.WAIT, "own_reply_echo")
            # The human answered. Drop whatever we were about to say.
            self.pending = None
            self.staged_text = None
            self.staged_version = -1
            return self._pause("user_took_over")

        # A staged reply dies the moment a new incoming message arrives.
        if (self.staged_text is not None and obs.incoming is True
                and obs.text and obs.text != self.staged_text):
            self.staged_text = None
            self.staged_version = -1

        if obs.change > 0 and obs.incoming is True and obs.text:
            self._accept_incoming(obs)
        elif obs.incoming is False and obs.text is None:
            return self._emit(Decision.WAIT, "outgoing_text_unreadable")

        if self.staged_text is not None:
            return self._emit(Decision.WAIT, "staged_awaiting_commit")

        if self.pending is None:
            return self._emit(Decision.WAIT, "idle")

        if self._merge_closed(obs.now):
            self.generation_version = self.version
            return self._emit(Decision.GENERATE, self.pending.text)

        return self._emit(Decision.WAIT, "merging")

    def generation_finished(self, text: str) -> tuple[Decision, str]:
        """Called when the model returned. The text is staged, not committed."""
        if self.state is State.PAUSED:
            return self._emit(Decision.DISCARD, "paused")
        if self.generation_version != self.version:
            return self._emit(Decision.DISCARD, "superseded_during_generation")
        self.staged_text = text
        self.staged_version = self.version
        return self._emit(Decision.WAIT, "staged")

    def generation_skipped(self) -> tuple[Decision, str]:
        """The model chose not to answer ("wait"). That message is handled.

        Without this the pending message stayed pending and the next frame
        generated again: live 15:3x the model was asked about one "okok" eight
        times in four seconds, and earlier about one "嗯" until it gave in and
        replied. A message that arrived during generation is kept: it is new.
        """
        if self.state is State.PAUSED:
            return self._emit(Decision.DISCARD, "paused")
        if self.generation_version != self.version:
            return self._emit(Decision.DISCARD, "superseded_during_generation")
        self.pending = None
        return self._emit(Decision.WAIT, "model_wait")

    def readback_finished(self, matched: bool, reason: str) -> tuple[Decision, str]:
        """Called with the clipboard readback result, before any Enter."""
        if self.state is State.PAUSED:
            return self._emit(Decision.DISCARD, "paused")
        if self.staged_text is None:
            return self._emit(Decision.DISCARD, "nothing_staged")
        if not matched:
            self.staged_text = None
            return self._pause(f"readback_failed:{reason}")
        return self._emit(Decision.COMMIT, self.staged_text)

    def commit_finished(self, delivered: bool, now: float | None = None) -> tuple[Decision, str]:
        if self.staged_text:
            self.last_commit_text = self.staged_text
            self.last_commit_at = now if now is not None else float("inf")
        self.staged_text = None
        self.staged_version = -1
        self.pending = None
        if not delivered:
            return self._pause("unknown_after_commit")
        return self._emit(Decision.WAIT, "delivered")

    def resume(self) -> None:
        """Explicit resume. The caller must also rebuild the baseline."""
        if self.state is State.PAUSED:
            self.state = State.RUNNING
            self.pause_reason = ""
            self.held = ""
            self.pending = None
            self.staged_text = None

    def pause(self, reason: str) -> tuple[Decision, str]:
        return self._pause(reason)

    def note_baseline(self, text: str | None, incoming: bool | None,
                      above: object = None) -> None:
        """The newest block visible when hosting starts. Without it, the first
        repaint after a restart re-reads history as if it had just arrived."""
        if text and incoming is not None:
            self.newest_text, self.newest_incoming = text, incoming
            self.newest_run = _Above(above).run_of(text)

    # ----------------------------------------------------------------- private

    def _is_own_reply(self, obs: Observation) -> bool:
        """Was this outgoing bubble the one we just sent?

        OCR of our own bubble is not byte-identical to what we sent: it can add
        or drop a comma, a space or a trailing glyph. Comparing exactly failed on
        perfectly ordinary replies, the loop read our own message as "the human
        took over" and stopped - which the user saw as "it answers once and then
        goes quiet".

        This comparison only answers "is this mine"; it never answers "is this
        correct". Normalising punctuation here cannot mask a wrong reply, because
        the clipboard readback still compares verbatim before anything is sent.
        """
        if not self.last_commit_text:
            return False
        if (obs.now - self.last_commit_at) > OWN_REPLY_WINDOW_S:
            return False
        return _same_text(obs.text or "", self.last_commit_text)

    def _continues(self, above: "_Above") -> bool:
        """Is the newest block we knew still visible right above this one?

        A new message pushes the old newest up by one block; a scroll up pushes
        it out of view below. So a changed newest block is believed only when
        the previous one appears above it. Looked up lazily, nearest first, so a
        normal event costs one extra OCR call.
        """
        if above.unknown or not self.newest_text:
            return True
        return any(_same_text(text, self.newest_text) for text in above)

    def _hold(self, reason: str) -> tuple[Decision, str]:
        self.held = reason
        return self._emit(Decision.WAIT, reason)

    def _accept_incoming(self, obs: Observation) -> None:
        self.version += 1
        if self.pending is None:
            self.pending = _Pending(obs.text, obs.now, obs.now)
            return
        self.pending.last_seen = obs.now
        self.pending.count += 1
        self.pending.text = obs.text

    def _merge_closed(self, now: float) -> bool:
        pending = self.pending
        if pending is None:
            return False
        if now - pending.last_seen >= MERGE_QUIET_S:
            return True
        if pending.count >= MERGE_MAX_MESSAGES:
            return True
        return now - pending.first_seen >= MERGE_TOTAL_S

    def _pause(self, reason: str) -> tuple[Decision, str]:
        self.state = State.PAUSED
        self.pause_reason = reason
        self.pending = None
        self.staged_text = None
        return self._emit(Decision.PAUSE, reason)

    def _emit(self, decision: Decision, detail: str) -> tuple[Decision, str]:
        self.decisions.append((decision, detail))
        return decision, detail

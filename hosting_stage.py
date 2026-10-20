"""Staging: put the reply into the input box and read it back. Never presses Enter.

The order matters and each step is a separate outcome:

    1. probe the box with a sentinel  -> a non-empty box is a draft: refuse,
       never overwrite what the user typed;
    2. paste the reply;
    3. select-all + copy and compare  -> the clipboard is the source of truth;
    4. restore whatever text was on the clipboard before we started.

Nothing here commits. Committing is a separate, explicitly authorised step.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from input_readback_clipboard import PortError, make_sentinel


class StageOutcome(str, Enum):
    STAGED = "STAGED"
    FOCUS_LOST = "FOCUS_LOST"
    DRAFT_PRESENT = "DRAFT_PRESENT"
    PROBE_FAILED = "PROBE_FAILED"
    READBACK_MISMATCH = "READBACK_MISMATCH"
    CLIPBOARD_ERROR = "CLIPBOARD_ERROR"


@dataclass(frozen=True)
class StageResult:
    outcome: StageOutcome
    text: str | None = None
    reason: str = ""


def _restore(clipboard, saved):
    try:
        clipboard.write_text(saved if saved is not None else "")
    except Exception:
        pass


def commit_and_verify(*, reply, target, ledger, keyboard, verify):
    """Write the ledger entry BEFORE Enter, then press Enter exactly once.

    If verification cannot confirm delivery, the entry stays in the history as
    an unconfirmed commit and the loop must freeze rather than retry. The ledger
    write happens first on purpose: a crash between the two leaves a visible
    unfinished record, which is the safe failure.
    """
    commit_id = ledger.begin_commit(target, reply)
    keyboard.send_enter()
    delivered = bool(verify())
    ledger.finish_commit(commit_id, delivered,
                         note="" if delivered else "not_confirmed_after_enter")
    return delivered, commit_id


def stage_reply(reply, *, keyboard, clipboard, policy="text_only", guard=None):
    """Paste `reply` into the focused input box and verify it by clipboard readback.

    `guard` is called before every key press: if the target window lost the
    foreground the keys would go somewhere else, so we abort instead of typing
    into a stranger. Without it a focus loss looks like "copy did not run".
    """
    if not reply:
        return StageResult(StageOutcome.PROBE_FAILED, reason="empty_reply")
    if guard is not None and not guard():
        return StageResult(StageOutcome.FOCUS_LOST, reason="foreground_lost")

    try:
        saved = clipboard.read_text()
    except Exception:
        saved = None

    sentinel = make_sentinel()
    try:
        clipboard.write_text(sentinel)
        keyboard.select_all()
        keyboard.copy()
        probe = clipboard.read_text()
    except PortError as error:
        return StageResult(StageOutcome.PROBE_FAILED, reason=error.reason)
    except Exception:
        return StageResult(StageOutcome.PROBE_FAILED, reason="probe_exception")

    if probe is None:
        # Our sentinel text is gone and nothing textual replaced it: Ctrl+C did
        # copy something, just not text. That is a draft (an image, a file, a
        # sticker) - live 14:05 an image sat in the box. Refuse like any draft.
        _restore(clipboard, saved)
        return StageResult(StageOutcome.DRAFT_PRESENT, reason="non_text_draft_in_input")

    if probe != sentinel:
        # Something is already in the box. Never overwrite a draft.
        _restore(clipboard, saved)
        return StageResult(StageOutcome.DRAFT_PRESENT, text=probe, reason="draft_in_input")

    if guard is not None and not guard():
        _restore(clipboard, saved)
        return StageResult(StageOutcome.FOCUS_LOST, reason="foreground_lost_before_paste")

    try:
        clipboard.write_text(reply)
        keyboard.paste()
        # Write the sentinel again: otherwise a dead Ctrl+C would leave the
        # reply we just put on the clipboard and the readback would "succeed"
        # without ever reading the input box.
        if guard is not None and not guard():
            raise PortError("foreground_lost_before_readback")
        clipboard.write_text(sentinel)
        keyboard.select_all()
        keyboard.copy()
        got = clipboard.read_text()
    except PortError as error:
        _restore(clipboard, saved)
        return StageResult(StageOutcome.CLIPBOARD_ERROR, reason=error.reason)
    except Exception:
        _restore(clipboard, saved)
        return StageResult(StageOutcome.CLIPBOARD_ERROR, reason="stage_exception")

    _restore(clipboard, saved)

    if got is None:
        return StageResult(StageOutcome.PROBE_FAILED, reason="clipboard_non_text_after_paste")
    if got == sentinel:
        return StageResult(StageOutcome.PROBE_FAILED, reason="copy_did_not_run")
    if got != reply:
        return StageResult(StageOutcome.READBACK_MISMATCH, text=got, reason="exact_text_mismatch")
    return StageResult(StageOutcome.STAGED, text=got, reason="exact_clipboard_text_match")

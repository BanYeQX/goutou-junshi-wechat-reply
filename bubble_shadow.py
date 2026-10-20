"""Offline-only occurrence hypotheses. Not a capture, review or reply adapter.

Inputs are already segmented bubbles and explicit caller assertions. Those
assertions are NOT verified against WeChat here. Outputs never authorize model
requests or sending. Exact title checks remain exact; body OCR is not repaired.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from bubble_appearance import Appearance, compare_appearance, valid_appearance
from visual_types import Box, Candidate, WindowBinding, exact_title


def _text(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


@dataclass(frozen=True, repr=False)
class TrackingScope:
    profile_id: str
    expected_title: str
    binding: WindowBinding
    message_roi: Box
    calibration_revision: int
    observation_epoch: str


@dataclass(frozen=True, repr=False)
class Bubble:
    candidate: Candidate
    # Optional OCR-independent LOCAL appearance key, supplied by the replay.
    # Not Candidate.fingerprint (which mixes pixels with OCR text): that value
    # can corroborate two UNREADABLE placeholders, not an OCR type change. No
    # legacy semantic fixtures use this only; required mode ignores manual keys.
    visual_key: str | None = None
    complete: bool = True
    appearance: Appearance | None = None

    @property
    def readable(self):
        return self.complete and self.candidate.safe and bool(_text(self.candidate.text))


@dataclass(frozen=True, repr=False)
class Snapshot:
    scope: TrackingScope
    sequence: int
    monotonic_ms: int
    observed_title: str
    bubbles: tuple[Bubble, ...]
    stable: bool
    continuous: bool
    at_latest: bool
    user_activity: str  # none / reply / scroll / unknown; never inferred here


@dataclass(frozen=True, repr=False)
class Instance:
    instance_id: str
    bubble: Bubble
    # Sticky: an unreadable occurrence never becomes a fresh text event just
    # because a later OCR pass reads it. It cannot become a trusted anchor.
    quarantined: bool


@dataclass(frozen=True, repr=False)
class ShadowResult:
    verdict: str  # old / new / uncertain; these are OFFLINE hypotheses
    reason: str
    instances: tuple[Instance, ...] = ()
    new_ids: tuple[str, ...] = ()
    possible_overlaps: tuple[int, ...] = ()
    anchor_count: int = 0
    distinct_anchor_count: int = 0
    baseline: bool = False
    appearance_mode: str = "required"

    @property
    def automatic_reply_allowed(self):
        return False

    @property
    def requires_human_review(self):
        return True

    def numeric_report(self):
        """No body, title, profile, window handle, instance ID or image data."""
        return {
            "diagnostic_only": True,
            "verdict": self.verdict,
            "reason": self.reason,
            "baseline": self.baseline,
            "appearance_mode": self.appearance_mode,
            "visible_instances": len(self.instances),
            "new_hypotheses": len(self.new_ids),
            "possible_overlap_count": len(self.possible_overlaps),
            "anchor_count": self.anchor_count,
            "distinct_anchor_count": self.distinct_anchor_count,
            "quarantined_instances": sum(i.quarantined for i in self.instances),
            "automatic_reply_allowed": False,
            "requires_human_review": True,
        }


class _AppearanceBudgetExceeded(RuntimeError):
    pass


class BubbleShadowTracker:
    """Ordered old-suffix/current-prefix correspondence, not text deduplication.

    Enumerate ALL compatible overlaps before checking anchor strength. Never
    choose the longest match when a shorter explanation is also possible.
    Require the old last occurrence plus >=3 readable, distinct text/direction
    anchors. Independent vertical movements are allowed, not just one rigid
    translation. Default mode requires image-derived local appearance as an
    additional filter, never as a substitute for text/direction or sequence.
    Unknown/readable correspondence keeps quarantine. Manual keys and exact
    unknown signatures are available ONLY in explicit semantic fixture mode.

    Known scroll/reply/gap, downward old content, clipping, ambiguity, weak
    anchors, or new unknown/self content pause and latch until an EXPLICIT new
    baseline. No failed update consumes IDs or commits a partial observation.

    These constraints do not solve visually identical history replay. Geometry
    and caller assertions alone cannot prove freshness. This class is not wired
    into the application and must not be used as sending authorization.
    Comparisons have a bounded budget; exceeding it pauses rather than selecting
    the best match from a partially searched set.
    """

    def __init__(self, *, max_gap_ms=10_000, horizontal_slack_px=24,
                 appearance_mode="required", max_appearance_comparisons=512):
        if type(max_gap_ms) is not int or max_gap_ms <= 0:
            raise ValueError("invalid_gap_limit")
        if type(horizontal_slack_px) is not int or not 0 <= horizontal_slack_px <= 100:
            raise ValueError("invalid_horizontal_limit")
        if appearance_mode not in ("required", "semantic_fixture"):
            raise ValueError("invalid_appearance_mode")
        if (type(max_appearance_comparisons) is not int
                or not 1 <= max_appearance_comparisons <= 16384):
            raise ValueError("invalid_appearance_budget")
        self.max_gap_ms = max_gap_ms
        self.horizontal_slack_px = horizontal_slack_px
        self.appearance_mode = appearance_mode
        self.max_appearance_comparisons = max_appearance_comparisons
        self._previous: tuple[Instance, ...] = ()
        self._snapshot: Snapshot | None = None
        self._serial = 0
        self._paused = False

    @property
    def previous(self):
        return self._previous

    @property
    def serial(self):
        return self._serial

    @property
    def paused(self):
        return self._paused

    def _reject(self, reason, overlaps=(), anchors=0, distinct=0):
        self._paused = True
        # Never present stale rows as a successful CURRENT observation.
        return ShadowResult("uncertain", reason, possible_overlaps=overlaps,
                            anchor_count=anchors, distinct_anchor_count=distinct,
                            appearance_mode=self.appearance_mode)

    @staticmethod
    def _invalid(snapshot):
        if not isinstance(snapshot, Snapshot) or not isinstance(snapshot.scope, TrackingScope):
            return "invalid_snapshot"
        scope = snapshot.scope
        if (not isinstance(scope.binding, WindowBinding) or not isinstance(scope.message_roi, Box)
                or not isinstance(scope.profile_id, str) or not scope.profile_id
                or not isinstance(scope.expected_title, str) or not scope.expected_title.strip()
                or not isinstance(scope.observation_epoch, str) or not scope.observation_epoch
                or type(scope.calibration_revision) is not int or scope.calibration_revision < 1):
            return "invalid_scope"
        binding, roi = scope.binding, scope.message_roi
        if (any(type(v) is not int for v in (binding.hwnd, binding.pid, binding.width,
                    binding.height, binding.dpi, *roi.tuple()))
                or min(binding.hwnd, binding.pid, binding.dpi) <= 0
                or not 1 <= binding.width <= 5000 or not 1 <= binding.height <= 4000
                or not roi.inside(binding.width, binding.height)):
            return "invalid_scope"
        if not isinstance(snapshot.observed_title, str) or not exact_title(snapshot.observed_title, scope.expected_title):
            return "title_mismatch"
        if (type(snapshot.sequence) is not int or snapshot.sequence < 1
                or type(snapshot.monotonic_ms) is not int or snapshot.monotonic_ms < 0):
            return "invalid_sequence"
        if snapshot.stable is not True:
            return "unstable_frame"
        if snapshot.continuous is not True:
            return "observation_gap"
        if snapshot.at_latest is not True:
            return "latest_bottom_unconfirmed"
        if snapshot.user_activity != "none":
            return "user_activity"
        if not isinstance(snapshot.bubbles, tuple) or len(snapshot.bubbles) > 256:
            return "invalid_bubbles"
        last_bottom = 0
        for bubble in snapshot.bubbles:
            if not isinstance(bubble, Bubble) or not isinstance(bubble.candidate, Candidate):
                return "invalid_bubble"
            row = bubble.candidate
            if (not isinstance(row.box, Box) or any(type(v) is not int for v in row.box.tuple())
                    or not row.box.inside(roi.width, roi.height)
                    or row.box.top < last_bottom or type(bubble.complete) is not bool
                    or row.direction not in ("friend", "self", "unknown")
                    or not isinstance(row.text, str) or len(row.text) > 8192
                    or not isinstance(row.kind, str) or not 1 <= len(row.kind) <= 80
                    or not isinstance(row.uncertainty, str)
                    or not isinstance(row.fingerprint, str) or len(row.fingerprint) > 256
                    or (bubble.visual_key is not None and (not isinstance(bubble.visual_key, str)
                        or not 1 <= len(bubble.visual_key) <= 256))):
                return "invalid_bubble"
            if row.safe and not _text(row.text):
                return "empty_text_candidate"
            if bubble.appearance is not None and not valid_appearance(bubble.appearance):
                return "invalid_appearance"
            if bubble.appearance is not None and (bubble.appearance.width != row.box.width
                                                  or bubble.appearance.height != row.box.height):
                return "appearance_geometry_mismatch"
            last_bottom = row.box.bottom
        return None

    def _input_problem(self, snapshot):
        problem = self._invalid(snapshot)
        if problem:
            return problem
        if self.appearance_mode == "required" and any(
                bubble.complete and bubble.appearance is None for bubble in snapshot.bubbles):
            return "appearance_required"
        return None

    def establish_baseline(self, snapshot):
        """Explicit rebaseline ONLY; all visible instances are old, never new."""
        problem = self._input_problem(snapshot)
        if problem:
            return self._reject(problem)
        tracked = tuple(Instance(f"shadow:{self._serial + i + 1}", bubble,
                                 not bubble.readable)
                        for i, bubble in enumerate(snapshot.bubbles))
        self._serial += len(tracked)
        self._previous, self._snapshot, self._paused = tracked, snapshot, False
        return ShadowResult("old", "explicit_baseline", tracked, baseline=True,
                            appearance_mode=self.appearance_mode)

    def _pair(self, old, current, comparison_budget):
        before, after = old.bubble, current
        a, b = before.candidate, after.candidate
        if not before.complete or not after.complete:
            return None
        if a.direction != "unknown" and b.direction != "unknown" and a.direction != b.direction:
            return None
        # Candidate proposal limits only, NOT a learned confidence threshold.
        if abs(a.box.left - b.box.left) > self.horizontal_slack_px or b.box.top > a.box.top:
            return None
        if before.readable and after.readable and (
                a.direction != b.direction or _text(a.text) != _text(b.text)):
            return None
        has_features = before.appearance is not None or after.appearance is not None
        if has_features:
            if before.appearance is None or after.appearance is None:
                return None  # never downgrade a missing/conflicting descriptor to text-only
            if comparison_budget[0] >= self.max_appearance_comparisons:
                raise _AppearanceBudgetExceeded()
            comparison_budget[0] += 1
            if compare_appearance(before.appearance, after.appearance).verdict != "same_candidate":
                return None
        elif self.appearance_mode == "required":
            return None
        if before.readable and after.readable:
            return (a.direction, _text(a.text)) if not old.quarantined else ()
        if has_features:
            return ()  # local appearance bridges the placeholder; quarantine stays
        if before.visual_key is not None and before.visual_key == after.visual_key:
            return ()  # placeholder correspondence, never a readable anchor
        if (not before.readable and not after.readable and a.fingerprint
                and a.signature == b.signature):
            return ()  # unchanged unknown crop/text/type, not a message ID
        return None

    def update(self, snapshot):
        if self._snapshot is None:
            return self._reject("baseline_required")
        if self._paused:
            return self._reject("explicit_rebaseline_required")
        problem = self._input_problem(snapshot)
        if problem:
            return self._reject(problem)
        previous = self._snapshot
        if snapshot.scope != previous.scope:
            return self._reject("scope_changed")
        if snapshot.sequence != previous.sequence + 1:
            return self._reject("sequence_gap")
        elapsed = snapshot.monotonic_ms - previous.monotonic_ms
        if not 0 < elapsed <= self.max_gap_ms:
            return self._reject("time_gap")
        # Exact observation refresh is a no-event fast path, including repeated
        # strings and placeholders. This does NOT establish real-world identity.
        if snapshot.bubbles == previous.bubbles:
            self._snapshot = snapshot
            return ShadowResult("old", "identical_observation", self._previous,
                                appearance_mode=self.appearance_mode)
        old, current = self._previous, snapshot.bubbles
        if not old or not current:
            return self._reject("lost_anchors")
        if not old[-1].bubble.complete:
            return self._reject("old_tail_clipped")
        possibilities = []
        comparison_budget = [0]
        try:
            for size in range(1, min(len(old), len(current)) + 1):
                anchors = []
                for a, b in zip(old[-size:], current[:size]):
                    pair = self._pair(a, b, comparison_budget)
                    if pair is None:
                        break
                    if pair:
                        anchors.append(pair)
                else:
                    possibilities.append((size, len(anchors), len(set(anchors))))
        except _AppearanceBudgetExceeded:
            return self._reject("appearance_comparison_budget_exceeded")
        overlaps = tuple(item[0] for item in possibilities)
        if not possibilities:
            return self._reject("no_ordered_overlap")
        if len(possibilities) != 1:
            return self._reject("ambiguous_overlap", overlaps)
        size, anchor_count, distinct_count = possibilities[0]
        if anchor_count < 3 or distinct_count < 3:
            return self._reject("insufficient_distinct_anchors", overlaps, anchor_count, distinct_count)
        tail = current[size:]
        if any(not bubble.readable for bubble in tail):
            return self._reject("unknown_or_clipped_new_tail", overlaps, anchor_count, distinct_count)
        if any(bubble.candidate.direction != "friend" for bubble in tail):
            return self._reject("manual_reply_in_new_tail", overlaps, anchor_count, distinct_count)
        retained = tuple(Instance(a.instance_id, b, a.quarantined or not b.readable)
                         for a, b in zip(old[-size:], current[:size]))
        added = tuple(Instance(f"shadow:{self._serial + i + 1}", bubble, False)
                      for i, bubble in enumerate(tail))
        # Commit only after every check. Failed updates never allocate IDs.
        self._serial += len(added)
        self._previous, self._snapshot = retained + added, snapshot
        return ShadowResult("new" if added else "old", "unique_ordered_overlap",
                            self._previous, tuple(i.instance_id for i in added),
                            overlaps, anchor_count, distinct_count,
                            appearance_mode=self.appearance_mode)

"""Local, exact-pixel continuity for an explicitly bottom-anchored session.

Hashes only, no screenshot retention/export and no model or input APIs. This
cannot infer a user's scrolling intent or distinguish pixel-identical replay.
It is for manually reviewed drafts, not an authorization for automatic sends.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, replace

from visual_types import Box, ObservationTracker, VisualPause


@dataclass(frozen=True, repr=False)
class PixelEvidence:
    width: int
    height: int
    rows: tuple[bytes, ...]
    background: bytes
    systems: tuple[Box, ...] = ()
    # Equal-width strips translated within the existing side margins. These
    # are exact hashes, not reduced-resolution/color-tolerant signatures.
    x_rows: tuple[tuple[int, tuple[bytes, ...]], ...] = ()

    @property
    def bottom(self):
        return max((y + 1 for y, key in enumerate(self.rows) if key != self.background), default=0)

    def validate(self):
        if not 40 <= self.width <= 5000 or not 100 <= self.height <= 4000 or len(self.rows) != self.height:
            raise VisualPause("本地像素衔接摘要尺寸无效，未区分新旧消息。")
        if len(self.background) != 32 or any(not isinstance(key, bytes) or len(key) != 32 for key in self.rows):
            raise VisualPause("本地像素衔接摘要无效，未区分新旧消息。")
        if any(not box.inside(self.width, self.height) for box in self.systems):
            raise VisualPause("时间标记位置越界，像素衔接已暂停。")
        if not isinstance(self.x_rows, tuple) or len(self.x_rows) not in (0, 2):
            raise VisualPause("二维像素摘要不完整，未区分新旧消息。")
        offsets = set()
        for entry in self.x_rows:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise VisualPause("二维像素摘要格式无效，未区分新旧消息。")
            dx, keys = entry
            if type(dx) is not int or dx not in (-1, 1) or dx in offsets:
                raise VisualPause("二维像素摘要偏移无效，未区分新旧消息。")
            if (not isinstance(keys, tuple) or len(keys) != self.height
                    or any(not isinstance(key, bytes) or len(key) != 32 for key in keys)):
                raise VisualPause("二维像素摘要行无效，未区分新旧消息。")
            offsets.add(dx)
        return self


def pixel_evidence(image):
    """Only the already-authorized message ROI is supplied by VisualReader.

    Exclude the existing narrow side margin (scrollbar), never crop away
    message text vertically. Also hash +/-1px translated strips of the SAME
    width inside those margins. Every byte of the old interior must match
    one unique translation; no fuzzy image score or color tolerance.
    """
    rgb = image.convert("RGB")
    try:
        width, height = rgb.size
        if width < 40 or height < 100 or width > 5000 or height > 4000:
            raise VisualPause("消息区域尺寸不适合本地像素衔接，保持只读暂停。")
        background = Counter(rgb.getpixel((0, y)) for y in range(height)).most_common(1)[0][0]
        raw = rgb.tobytes()
        left, right = 6, width - 12
        def rows_at(dx):
            return tuple(hashlib.sha256(raw[(y * width + left + dx) * 3:
                                            (y * width + right + dx) * 3]).digest() for y in range(height))
        keys = rows_at(0)
        blank = hashlib.sha256(bytes(background) * (right - left)).digest()
        return PixelEvidence(width, height, keys, blank,
                             x_rows=tuple((dx, rows_at(dx)) for dx in (-1, 1))).validate()
    finally:
        rgb.close()


DIAGNOSTIC_RESULTS = {
    "invalid", "missing_evidence", "out_of_bounds", "baseline", "unchanged",
    "geometry_changed", "no_exact_match", "ambiguous", "history", "unique",
}
DIAGNOSTIC_PIPELINES = {"cross_boundary", "unclassified_tail", "missing_anchor", "complete"}
DIAGNOSTIC_INTS = {
    "version", "old_width", "old_height", "new_width", "new_height",
    "old_content_bottom", "new_content_bottom", "old_foreground_rows", "new_foreground_rows",
    "same_position_equal_rows", "eligible_shifts", "exact_matches", "closest_shift_y",
    "closest_changed_rows", "closest_overlap_rows", "closest_ties", "matched_shift_y",
    "old_candidate_count", "current_candidate_count", "retained_candidate_count",
    "added_candidate_count", "unchanged_tail_candidate_count",
    "eligible_translations", "closest_shift_x", "matched_shift_x",
    "hypothesis_shift_x", "hypothesis_shift_y", "hypothesis_foreground_rows",
    "hypothesis_matched_foreground_rows", "hypothesis_overlap_rows",
    "hypothesis_changed_rows", "hypothesis_equal_rows", "hypothesis_ties",
    "hypothesis_longest_equal_run", "hypothesis_longest_changed_run",
    "hypothesis_changed_runs",
    *(f"hypothesis_band_{index}_changed_rows" for index in range(8)),
}


def safe_alignment_diagnostic(value):
    """Allow only fixed enums, booleans and bounded counts; never hashes/text."""
    if not isinstance(value, dict):
        return {}
    result = {key: number for key, number in value.items()
              if key in DIAGNOSTIC_INTS and type(number) is int and -10000 <= number <= 10000}
    for key in ("same_size", "same_background"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if type(value.get("result")) is str and value["result"] in DIAGNOSTIC_RESULTS:
        result["result"] = value["result"]
    if type(value.get("pipeline_result")) is str and value["pipeline_result"] in DIAGNOSTIC_PIPELINES:
        result["pipeline_result"] = value["pipeline_result"]
    return result


def _failure_sections(old, new, variants):
    """Diagnostic hypothesis only, built from existing hashes, never pixels.

    Absolute changed-row counts alone favor a tiny overlap and can conceal
    much longer matching content. Rank a DIAGNOSTIC hypothesis by exact
    foreground-row matches first, then equal-row fraction and overlap size.
    This does not choose a translation for the tracker or change rejection.
    No hashes, text, row-by-row bitmap or screenshot leave this function.
    """
    best, best_rank, ties = None, None, 0
    bottom = old.bottom
    for dy in range(-old.height + 120, old.height - 119):
        start = max(0, -dy)
        if bottom - start < 120 or not 0 < bottom + dy <= new.height:
            continue
        sample = old.rows[start:bottom]
        foreground = tuple(key for key in sample if key != old.background)
        if len(foreground) < 24 or len(set(foreground)) < 8:
            continue
        for dx, keys in variants:
            other = keys[start + dy:bottom + dy]
            equal = tuple(a == b for a, b in zip(sample, other))
            foreground_hits = sum(a != old.background and same for a, same in zip(sample, equal))
            changed = len(sample) - sum(equal)
            rank = (foreground_hits, sum(equal) / len(sample), len(sample))
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best = dx, dy, len(foreground), foreground_hits, equal, changed
                ties = 1
            elif rank == best_rank:
                ties += 1
    if best is None:
        return {}
    dx, dy, foreground_count, hits, equal, changed = best
    longest_equal = longest_changed = equal_run = changed_run = changed_runs = 0
    for same in equal:
        if same:
            equal_run += 1
            changed_run = 0
            longest_equal = max(longest_equal, equal_run)
        else:
            if not changed_run:
                changed_runs += 1
            changed_run += 1
            equal_run = 0
            longest_changed = max(longest_changed, changed_run)
    result = dict(hypothesis_shift_x=dx, hypothesis_shift_y=dy,
                  hypothesis_foreground_rows=foreground_count,
                  hypothesis_matched_foreground_rows=hits,
                  hypothesis_overlap_rows=len(equal), hypothesis_changed_rows=changed,
                  hypothesis_equal_rows=sum(equal), hypothesis_ties=ties,
                  hypothesis_longest_equal_run=longest_equal,
                  hypothesis_longest_changed_run=longest_changed,
                  hypothesis_changed_runs=changed_runs)
    for index in range(8):
        section = equal[index * len(equal) // 8:(index + 1) * len(equal) // 8]
        result[f"hypothesis_band_{index}_changed_rows"] = len(section) - sum(section)
    return result


def _unique_translation(old, new, *, horizontal, diagnostic=None):
    stats = diagnostic if diagnostic is not None else {}
    stats.clear()
    stats.update(version=2 if horizontal else 1, result="invalid")
    old.validate()
    new.validate()
    stats.update(old_width=old.width, old_height=old.height, new_width=new.width, new_height=new.height,
                 same_size=(old.width, old.height) == (new.width, new.height),
                 same_background=old.background == new.background,
                 old_content_bottom=old.bottom, new_content_bottom=new.bottom,
                 old_foreground_rows=sum(key != old.background for key in old.rows),
                 new_foreground_rows=sum(key != new.background for key in new.rows),
                 same_position_equal_rows=sum(a == b for a, b in zip(old.rows, new.rows)))
    if (old.width, old.height, old.background) != (new.width, new.height, new.background):
        stats["result"] = "geometry_changed"
        raise VisualPause("窗口区域或背景变化，像素衔接不成立；未将旧消息当新消息。")
    if horizontal and bool(old.x_rows) != bool(new.x_rows):
        raise VisualPause("前后像素摘要版本不同，不能混用；请重新建立明确基线。")
    if old.rows == new.rows:
        stats.update(result="unchanged", exact_matches=1, matched_shift_y=0)
        if horizontal:
            stats["matched_shift_x"] = 0
        return 0, 0
    bottom = old.bottom
    matches = []
    stats.update(eligible_shifts=0, exact_matches=0, closest_ties=0)
    variants = ((0, new.rows),) + (new.x_rows if horizontal else ())
    if horizontal:
        stats["eligible_translations"] = 0
    for dy in range(-old.height + 120, old.height - 119):
        start = max(0, -dy)
        if bottom - start < 120 or not 0 < bottom + dy <= new.height:
            continue
        sample = old.rows[start:bottom]
        informative = tuple(key for key in sample if key != old.background)
        # Solid rectangles/blank backgrounds are not a reliable identity.
        if len(informative) < 24 or len(set(informative)) < 8:
            continue
        stats["eligible_shifts"] += 1
        for dx, keys in variants:
            other = keys[start + dy:bottom + dy]
            # Counts rank diagnostics ONLY. Acceptance below still requires
            # equality of every full-width interior row, including old tail.
            changed = sum(a != b for a, b in zip(sample, other))
            if horizontal:
                stats["eligible_translations"] += 1
            if changed < stats.get("closest_changed_rows", old.height + 1):
                stats.update(closest_shift_y=dy, closest_changed_rows=changed,
                             closest_overlap_rows=len(sample), closest_ties=1)
                if horizontal:
                    stats["closest_shift_x"] = dx
            elif changed == stats.get("closest_changed_rows"):
                stats["closest_ties"] += 1
            if sample == other:
                matches.append((dx, dy))
    stats["exact_matches"] = len(matches)
    if len(matches) != 1:
        stats["result"] = "no_exact_match" if not matches else "ambiguous"
        if diagnostic is not None:
            try:
                stats.update(_failure_sections(old, new, variants))
            except Exception:
                pass  # A diagnostic failure must not replace the rejection.
        raise VisualPause("旧画面没有唯一的像素重叠，可能为重复消息、丢失锚点或重排；已暂停，不猜测新旧。")
    dx, dy = matches[0]
    stats["matched_shift_y"] = dy
    if horizontal:
        stats["matched_shift_x"] = dx
    if dy > 0:
        stats["result"] = "history"
        raise VisualPause("检测到向历史方向移动，不能把后来露出的历史当新消息；请重新确认会话最新底部。")
    stats["result"] = "unique"
    return dx, dy


def unique_shift(old, new, *, diagnostic=None):
    """Legacy vertical-only comparator, retained for numeric probe parity."""
    return _unique_translation(old, new, horizontal=False, diagnostic=diagnostic)[1]


def unique_translation(old, new, *, diagnostic=None):
    """One exact integer translation: x in {-1,0,1}, y upward or unchanged.

    All eligible x/y pairs compete, even when x=0 has an exact match. An
    ambiguous secondary translation must never be hidden by a fallback order.
    No matching pixel is discarded or recolored to obtain a translation.
    """
    return _unique_translation(old, new, horizontal=True, diagnostic=diagnostic)


def shifted(box, dy, dx=0):
    return Box(box.left + dx, box.top + dy, box.right + dx, box.bottom + dy)


class PixelSequenceTracker(ObservationTracker):
    """Carry occurrence IDs across one unique upward translation.

    Requires a user-confirmed latest-bottom baseline and no manual history
    scrolling thereafter. New candidates must lie wholly after the previous
    content tail. Changes to old OCR segmentation never create new events.
    Unknown candidates in the new tail remain unknown and block generation.
    """
    def __init__(self):
        super().__init__()
        self.evidence = None
        self.last_shift = 0
        self.last_shift_x = 0
        self.diagnostic = {}

    def update(self, rows, evidence):
        if not isinstance(evidence, PixelEvidence):
            self.diagnostic = {"version": 1, "result": "missing_evidence"}
            raise VisualPause("缺少本轮本地像素摘要，不能跨画面衔接；请重新单次只读检测。")
        self.diagnostic = {"version": 1, "result": "invalid"}
        evidence.validate()
        if any(not row.box.inside(evidence.width, evidence.height) for row in rows):
            self.diagnostic["result"] = "out_of_bounds"
            raise VisualPause("识别候选越过像素区域，已暂停。")
        if self.previous is None:
            result, added = super().update(rows)
            self.evidence = evidence
            self.diagnostic.update(result="baseline", old_candidate_count=0,
                current_candidate_count=len(rows), retained_candidate_count=len(result), added_candidate_count=0)
            return result, added
        dx, dy = unique_translation(self.evidence, evidence, diagnostic=self.diagnostic)
        # With EXACTLY unchanged interior row hashes there is no newly visible
        # pixel region. OCR geometry may include padding/phantom boxes beyond
        # the last non-background row. The old bottom is not a new-message
        # boundary in this case: every current OCR box is re-observation only.
        # Do not create occurrence IDs or promote old unknowns from such boxes.
        # Changed frames still use the original strict unique-shift/tail rules.
        unchanged = self.evidence.rows == evidence.rows
        boundary = evidence.height if unchanged else self.evidence.bottom + dy
        old = self.previous
        self.diagnostic.update(old_candidate_count=len(old), current_candidate_count=len(rows),
            unchanged_tail_candidate_count=sum(row.box.bottom > self.evidence.bottom for row in rows) if unchanged else 0)
        retained = []
        for item in old:
            box = shifted(item.box, dy, dx)
            if not box.inside(evidence.width, evidence.height):
                continue  # A clipped old item cannot be presented as complete.
            evidence_boxes = tuple(shifted(b, dy, dx) for b in item.evidence_boxes
                                   if shifted(b, dy, dx).inside(evidence.width, evidence.height))
            retained.append(replace(item, box=box, evidence_boxes=evidence_boxes, origin="已见候选（像素衔接）"))
        new = []
        for row in rows:
            if row.box.bottom <= boundary:
                # Pixel-proven old region: do not promote an unknown old row
                # to text just because a later OCR pass segmented it anew.
                # Conversely, new OCR uncertainty can only downgrade old rows.
                for i, item in enumerate(retained):
                    if item.safe and item.box.overlaps(row.box):
                        if not row.safe or (item.text, item.direction) != (row.text, row.direction):
                            retained[i] = replace(item, text="", kind="unknown",
                                                  uncertainty="旧画面像素一致，但本轮复查不确定；不再作为文字上下文")
                continue
            if row.box.top < boundary:
                self.diagnostic["pipeline_result"] = "cross_boundary"
                raise VisualPause("候选跨越新旧画面边界，不能确认归属；保持暂停，不生成草稿。")
            new.append(row)
        cover = tuple(row.box for row in rows) + evidence.systems
        if any(evidence.rows[y] != evidence.background
               and not any(box.top - 6 <= y < box.bottom + 6 for box in cover)
               for y in range(max(0, boundary), evidence.height)):
            self.diagnostic["pipeline_result"] = "unclassified_tail"
            raise VisualPause("新增尾部存在未归类像素，不能仅凭缺少文字判断没有新内容；已暂停。")
        # With no prior visible content, there is no temporal anchor.
        if new and self.evidence.bottom == 0:
            self.diagnostic["pipeline_result"] = "missing_anchor"
            raise VisualPause("旧画面没有可定位内容，不能确认新增；请重建明确基线。")
        added = tuple(self._new(item, "新增候选（像素衔接）") for item in new)
        result = tuple(retained) + added
        self.previous, self.evidence, self.last_shift = result, evidence, dy
        self.last_shift_x = dx
        self.diagnostic.update(pipeline_result="complete", retained_candidate_count=len(retained),
                               added_candidate_count=len(added))
        return result, added

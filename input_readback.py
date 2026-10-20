"""Experimental full-input local OCR. 尚未接入任何发送入口。

MATCH concerns visible text, not invisible line-end whitespace or sending
permission. All raw OCR strings are preserved: no strip, substitutions, or
separator guesses. Only provided memory images are used; no window access.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import math

import numpy as np
from PIL import Image

INSET_PX = 6
WHITE_PAD_PX = 60


class Verdict(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class Fragment:
    text: str
    score: float
    box: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class Readback:
    verdict: Verdict
    reason: str
    ocr_text: str | None = None
    raw_texts: tuple[str, ...] = ()
    fragments: tuple[Fragment, ...] = ()
    words: tuple[Fragment, ...] = ()
    ink_bbox: tuple[int, int, int, int] | None = None
    crop_rect: tuple[int, int, int, int] | None = None
    prepared_size: tuple[int, int] | None = None
    excluded_border_pixels: int = 0
    original_size: tuple[int, int] | None = None
    inset_size: tuple[int, int] | None = None
    padded_size: tuple[int, int] | None = None
    ocr_input_size: tuple[int, int] | None = None
    scale_factor: float | None = None
    failure_stage: str | None = None


def _bounds(mask):
    ys, xs = np.nonzero(mask)
    return None if not len(xs) else (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def _components(mask):
    import cv2
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    return labels, stats[1:count]


def _remove_closed_border(mask):
    """Prior strict closed-outline proof; never erase edge text."""
    height, width = mask.shape
    if min(height, width) < 8:
        return mask, 0
    labels, stats = _components(mask)
    for label, (x, y, w, h, area) in enumerate(stats, 1):
        if (x, y, w, h) != (0, 0, width, height):
            continue
        component = labels == label
        if component[2:-2, 2:-2].any():
            continue
        if not (component[:2].any(axis=0).all() and component[-2:].any(axis=0).all()
                and component[:, :2].any(axis=1).all() and component[:, -2:].any(axis=1).all()):
            continue
        cleaned = mask.copy()
        cleaned[component] = False
        return cleaned, int(area)
    return mask, 0


class InputReadback:
    """Lazy reuse of the existing pinned local RapidOCR, not a Transport."""
    def __init__(self, engine=None):
        self._engine = engine

    def read(self, image, expected_text, single_line_height_px):
        return read_input(image, expected_text, single_line_height_px, reader=self)

    def recognize(self, prepared):
        if self._engine is None:
            from ocr_runtime import ensure_ocr_runtime
            ensure_ocr_runtime()
            from title_ocr import build_offline_engine
            self._engine = build_offline_engine()
        # Optional geometry uses the SAME recognizer/weights; no text filtering.
        return self._engine(np.asarray(prepared)[:, :, ::-1].copy(),
                            return_word_box=True, return_single_char_box=True)


def _fragment(text, score, box, size):
    points = tuple(tuple(float(v) for v in point) for point in box)
    value = float(score)
    if (not isinstance(text, str) or len(points) != 4
            or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points)
            or not math.isfinite(value) or not 0 <= value <= 1
            or any(not (0 <= x <= size[0] and 0 <= y <= size[1]) for x, y in points)):
        raise ValueError("invalid_ocr_metadata")
    if max(x for x, _ in points) <= min(x for x, _ in points) or max(y for _, y in points) <= min(y for _, y in points):
        raise ValueError("degenerate_ocr_box")
    return Fragment(text, value, points)


def _interior_separator_unresolved(mask, words):
    """Only reject visible blank runs INSIDE OCR character centers.

    Never infer separator existence/count or join fragments from a short gap.
    The final character-to-caret gap is outside these centers. No cursor is
    segmented/deleted, and a second ink component is not a refusal condition.
    This conservative ambiguity check is not whitespace reconstruction.
    """
    if len(words) < 2:
        return False
    centers = sorted(sum(x for x, _ in word.box) / 4 for word in words)
    left = max(0, math.ceil(centers[0]) + INSET_PX)
    right = min(mask.shape[1], math.floor(centers[-1]) + INSET_PX + 1)
    run = 0
    for occupied in mask[:, left:right].any(axis=0):
        run = 0 if occupied else run + 1
        if run >= 3:
            return True
    return False


def read_input(image, expected_text, single_line_height_px, *, reader=None):
    """Original-pixel preflight -> inset6 -> white top/bottom60 -> full OCR.

    Scale is exactly 1 (never shrinks). Single-line ink height is supplied by
    the caller's known fixture/font, not inferred from editor dimensions.
    """
    meta = {}

    def uncertain(reason, stage):
        return Readback(Verdict.UNCERTAIN, reason, failure_stage=stage, **meta)

    if not isinstance(image, Image.Image) or image.mode != "RGB" or not isinstance(expected_text, str):
        return uncertain("invalid_input", "validation")
    width, height = image.size
    meta["original_size"] = image.size
    if min(width, height) <= 2 * INSET_PX or width * height > 4_000_000:
        return uncertain("invalid_image_size", "validation")
    if (isinstance(single_line_height_px, bool) or not isinstance(single_line_height_px, (int, float))
            or not math.isfinite(single_line_height_px) or single_line_height_px <= 0):
        return uncertain("single_line_height_unknown", "validation")
    pixels = np.asarray(image)
    colors, counts = np.unique(pixels.reshape(-1, 3), axis=0, return_counts=True)
    dominant = int(counts.argmax())
    if counts[dominant] < width * height * 0.55:
        return uncertain("background_ambiguous", "preflight")
    difference = np.max(np.abs(pixels.astype(np.int16) - colors[dominant].astype(np.int16)), axis=2)
    mask, excluded = _remove_closed_border(difference > 0)
    bbox = _bounds(mask)
    meta.update(ink_bbox=bbox, excluded_border_pixels=excluded)
    if bbox is None:
        return uncertain("no_text_ink_after_border" if excluded else "ink_not_observed", "preflight")
    left, top, right, bottom = bbox
    # Height refusal precedes trimming and any OCR, including the two-line case.
    if bottom - top > 2 * single_line_height_px:
        return uncertain("ink_height_exceeds_two_line_baselines", "geometry")
    # Inspect ORIGINAL pixels including the inset boundary before cropping.
    if (left <= INSET_PX or top <= INSET_PX
            or right >= width - INSET_PX or bottom >= height - INSET_PX):
        return uncertain("ink_in_removed_band_or_inset_boundary", "preflight")
    if int(difference[mask].max()) <= 32:
        return uncertain("low_contrast_ink_unresolved", "preflight")
    crop = (INSET_PX, INSET_PX, width - INSET_PX, height - INSET_PX)
    inset_size = (width - 2 * INSET_PX, height - 2 * INSET_PX)
    size = (inset_size[0], inset_size[1] + 2 * WHITE_PAD_PX)
    meta.update(crop_rect=crop, inset_size=inset_size, padded_size=size,
                prepared_size=size, ocr_input_size=size, scale_factor=1.0)
    if size[0] * size[1] > 8_000_000:
        return uncertain("prepared_image_too_large", "preparation")
    with image.crop(crop) as inset, Image.new("RGB", size, (255, 255, 255)) as prepared:
        prepared.paste(inset, (0, WHITE_PAD_PX))
        try:
            result = (reader or InputReadback()).recognize(prepared)
        except Exception:
            return uncertain("local_ocr_failed", "ocr")
    try:
        raw = () if result.txts is None else tuple(result.txts)
    except (AttributeError, TypeError):
        return uncertain("ocr_result_invalid", "ocr")
    if not all(isinstance(text, str) for text in raw):
        return uncertain("ocr_result_invalid", "ocr")
    meta["raw_texts"] = raw
    if not raw or not any(text != "" for text in raw):
        return uncertain("ocr_empty", "ocr")
    try:
        scores = () if result.scores is None else tuple(result.scores)
        boxes = () if result.boxes is None else tuple(result.boxes)
        if len(scores) != len(raw) or len(boxes) != len(raw):
            return uncertain("ocr_metadata_incomplete", "ocr")
        fragments = tuple(_fragment(t, s, b, size) for t, s, b in zip(raw, scores, boxes))
    except (AttributeError, ValueError, TypeError, OverflowError):
        return uncertain("ocr_metadata_invalid", "ocr")
    meta["fragments"] = fragments
    if any(fragment.score < 0.95 for fragment in fragments):
        return uncertain("ocr_score_below_guard", "ocr")
    # Only an unresolved bar is not confirmed body text or an empty editor.
    # Whole-input OCR has already run; no per-component precondition is added.
    if right - left <= 3:
        return uncertain("only_unresolved_vertical_mark", "comparison")
    ordered = sorted(fragments, key=lambda f: (sum(y for _, y in f.box) / 4,
                                               min(x for x, _ in f.box)))
    if len(ordered) > 1:
        # Multiplicity can prove extra visible text WITHOUT guessing separators.
        # It is never used to fabricate a matching complete string.
        actual = Counter(char for f in ordered for char in f.text if not char.isspace())
        expected = Counter(char for char in expected_text if not char.isspace())
        if any(count > expected[char] for char, count in actual.items()):
            return Readback(Verdict.MISMATCH, "extra_visible_text_confirmed", failure_stage="comparison", **meta)
        return uncertain("fragment_separator_unresolved", "comparison")
    text = raw[0]
    meta["ocr_text"] = text
    if not text or any(char in text for char in ("\x00", "\ufffd")) or all(char.isspace() for char in text):
        return uncertain("ocr_text_unresolved", "comparison")
    if text != expected_text:
        return Readback(Verdict.MISMATCH, "exact_text_mismatch", failure_stage="comparison", **meta)
    words = []
    try:
        for line in getattr(result, "word_results", ()) or ():
            for word_text, score, box in line:
                if box is not None:
                    words.append(_fragment(word_text, score, box, size))
    except (ValueError, TypeError, OverflowError):
        return uncertain("ocr_word_geometry_invalid", "comparison")
    meta["words"] = tuple(words)
    if len(text) > 1 and not words:
        return uncertain("ocr_word_geometry_unavailable", "comparison")
    if _interior_separator_unresolved(mask, words):
        return uncertain("visible_separator_unresolved", "comparison")
    return Readback(Verdict.MATCH, "exact_visible_text_match", **meta)

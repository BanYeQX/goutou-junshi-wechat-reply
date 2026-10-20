"""Bounded, in-memory LOCAL bubble features. No capture/OCR/network/file I/O.

Similarity proposes correspondence only. It cannot verify a Chinese character,
message freshness, or sender identity. Never serialize the private feature bytes.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

from visual_types import Box


WIDTH, HEIGHT = 192, 96
COLOR_WIDTH, COLOR_HEIGHT = 16, 8
SCHEMA = 1


@dataclass(frozen=True, repr=False)
class Appearance:
    width: int
    height: int
    gray: bytes
    edges: bytes
    color: bytes
    contrast: int
    edge_pixels: int
    version: int = SCHEMA

    @property
    def informative(self):
        return self.contrast >= 24 and self.edge_pixels >= 20


@dataclass(frozen=True)
class AppearanceComparison:
    verdict: str  # same_candidate / different / uncertain; never identity proof
    reason: str
    gray_error_milli: int = 0
    active_error_milli: int = 0
    edge_overlap_milli: int = 0
    color_error_milli: int = 0

    def numeric_report(self):
        return {
            "diagnostic_only": True,
            "verdict": self.verdict,
            "reason": self.reason,
            "gray_error_milli": self.gray_error_milli,
            "active_error_milli": self.active_error_milli,
            "edge_overlap_milli": self.edge_overlap_milli,
            "color_error_milli": self.color_error_milli,
            "automatic_reply_allowed": False,
        }


def valid_appearance(value):
    shape_valid = (isinstance(value, Appearance) and type(value.version) is int and value.version == SCHEMA
            and type(value.width) is int and 8 <= value.width <= 5000
            and type(value.height) is int and 8 <= value.height <= 4000
            and type(value.contrast) is int and 0 <= value.contrast <= 255
            and type(value.edge_pixels) is int and 0 <= value.edge_pixels <= WIDTH * HEIGHT
            and isinstance(value.gray, bytes) and len(value.gray) == WIDTH * HEIGHT
            and isinstance(value.edges, bytes) and len(value.edges) == WIDTH * HEIGHT
            and isinstance(value.color, bytes) and len(value.color) == COLOR_WIDTH * COLOR_HEIGHT * 3)
    if not shape_valid:
        return False
    gray = np.frombuffer(value.gray, np.uint8).reshape(HEIGHT, WIDTH)
    contrast = int(gray.max()) - int(gray.min())
    edges = _edges(gray)
    active = int(np.count_nonzero(edges >= max(12, contrast // 8)))
    return value.contrast == contrast and value.edge_pixels == active and value.edges == edges.tobytes()


def _edges(values):
    signed = values.astype(np.int16)
    edges = np.zeros((HEIGHT, WIDTH), dtype=np.int16)
    edges[:, 1:] += np.abs(np.diff(signed, axis=1))
    edges[1:, :] += np.abs(np.diff(signed, axis=0))
    return np.minimum(edges, 255).astype(np.uint8)


def extract_appearance(message_roi, box):
    """Read exactly the supplied bubble box; own/close crops, not caller image.

    Input MUST already be the authorized message ROI, not a full window/frame.
    RGB only: do not silently flatten alpha or reinterpret foreign pixel formats.
    Features are bounded to 37248 bytes per bubble, independent of source area.
    """
    if (not isinstance(message_roi, Image.Image) or message_roi.mode != "RGB"
            or not 1 <= message_roi.width <= 5000 or not 1 <= message_roi.height <= 4000
            or not isinstance(box, Box) or any(type(v) is not int for v in box.tuple())
            or not box.inside(message_roi.width, message_roi.height)
            or min(box.width, box.height) < 8):
        raise ValueError("invalid_appearance_roi")
    crop = message_roi.crop(box.tuple())
    gray_image = fine = color = None
    try:
        gray_image = crop.convert("L")
        fine = gray_image.resize((WIDTH, HEIGHT), Image.Resampling.BILINEAR)
        color = crop.resize((COLOR_WIDTH, COLOR_HEIGHT), Image.Resampling.BILINEAR)
        values = np.array(fine, dtype=np.uint8)
        edges = _edges(values)
        contrast = int(values.max()) - int(values.min())
        active = int(np.count_nonzero(edges >= max(12, contrast // 8)))
        return Appearance(box.width, box.height, values.tobytes(), edges.tobytes(),
                          color.tobytes(), contrast, active)
    finally:
        for owned in (crop, gray_image, fine, color):
            if owned is not None:
                owned.close()


def compare_appearance(before, after):
    """Multi-check candidate filter, NOT a calibrated confidence score.

    Fixed experimental limits: shape 12%, coarse RGB MAE 12/255, fine gray
    MAE 8/255, edge-active gray MAE 28/255, edge overlap >=600/1000. Search only
    +/-1 normalized cell locally; no whole-frame translation or row hashes.
    Low-information crops are uncertain, including byte-identical blank crops.
    """
    if not valid_appearance(before) or not valid_appearance(after):
        return AppearanceComparison("uncertain", "invalid_features")
    if not before.informative or not after.informative:
        return AppearanceComparison("uncertain", "uninformative_crop")
    if (abs(before.width - after.width) * 100 > min(before.width, after.width) * 12
            or abs(before.height - after.height) * 100 > min(before.height, after.height) * 12):
        return AppearanceComparison("different", "shape_changed")
    if before == after:
        return AppearanceComparison("same_candidate", "exact_informative_features", edge_overlap_milli=1000)
    rgb_before = np.frombuffer(before.color, np.uint8).astype(np.int16)
    rgb_after = np.frombuffer(after.color, np.uint8).astype(np.int16)
    color_error = int(np.abs(rgb_before - rgb_after).mean() * 1000)
    if color_error > 12_000:
        return AppearanceComparison("different", "coarse_color_changed", color_error_milli=color_error)
    a = np.frombuffer(before.gray, np.uint8).reshape(HEIGHT, WIDTH).astype(np.int16)
    b = np.frombuffer(after.gray, np.uint8).reshape(HEIGHT, WIDTH).astype(np.int16)
    ae = np.frombuffer(before.edges, np.uint8).reshape(HEIGHT, WIDTH)
    be = np.frombuffer(after.edges, np.uint8).reshape(HEIGHT, WIDTH)
    threshold = max(12, min(before.contrast, after.contrast) // 8)
    proposals = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            ay, ax = slice(max(0, -dy), HEIGHT - max(0, dy)), slice(max(0, -dx), WIDTH - max(0, dx))
            by, bx = slice(max(0, dy), HEIGHT - max(0, -dy)), slice(max(0, dx), WIDTH - max(0, -dx))
            mask_a, mask_b = ae[ay, ax] >= threshold, be[by, bx] >= threshold
            active = mask_a | mask_b
            count = int(np.count_nonzero(active))
            if count < 20:
                continue
            delta = np.abs(a[ay, ax] - b[by, bx])
            gray_error = int(delta.mean() * 1000)
            active_error = int(delta[active].mean() * 1000)
            overlap = int(np.count_nonzero(mask_a & mask_b) * 1000 // count)
            proposals.append((gray_error <= 8000 and active_error <= 28_000 and overlap >= 600,
                              active_error, gray_error, overlap))
    if not proposals:
        return AppearanceComparison("uncertain", "insufficient_structural_support", color_error_milli=color_error)
    # Prefer candidates satisfying ALL checks; never trade a failed check for
    # a higher aggregate similarity. Scores remain private scalar diagnostics.
    accepted, active_error, gray_error, overlap = min(proposals,
        key=lambda p: (not p[0], p[1], p[2], -p[3]))
    return AppearanceComparison("same_candidate" if accepted else "different",
        "local_structure_candidate" if accepted else "local_structure_changed",
        gray_error, active_error, overlap, color_error)

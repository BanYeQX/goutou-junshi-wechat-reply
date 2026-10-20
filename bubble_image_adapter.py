"""Pure offline adapter: already-authorized message ROI -> private features.

Does not acquire images, OCR text, open files, write reports or invoke models.
Title/context assertions are validated before touching supplied image pixels.
"""
from dataclasses import replace

from PIL import Image

from bubble_appearance import extract_appearance
from bubble_shadow import BubbleShadowTracker


def enrich_snapshot(snapshot, message_roi):
    problem = BubbleShadowTracker._invalid(snapshot)
    if problem:
        raise ValueError(problem)
    roi = snapshot.scope.message_roi
    if (not isinstance(message_roi, Image.Image) or message_roi.mode != "RGB"
            or message_roi.size != (roi.width, roi.height)):
        raise ValueError("message_roi_size_or_mode_changed")
    # Conservatively mark every ROI-edge-touching box incomplete, even if an
    # upstream classifier forgot to flag it. Do not manufacture visual support
    # for the partial green bubble seen at the top of earlier user screenshots.
    # Build locally; never partially modify the original snapshot.
    rows = []
    for bubble in snapshot.bubbles:
        box = bubble.candidate.box
        complete = (bubble.complete and box.left > 0 and box.top > 0
                    and box.right < roi.width and box.bottom < roi.height)
        rows.append(replace(bubble, complete=complete,
                            appearance=extract_appearance(message_roi, box) if complete else None))
    return replace(snapshot, bubbles=tuple(rows))

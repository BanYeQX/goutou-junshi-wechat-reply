"""Locate the newest text block inside a cropped chat strip.

Pure function over already-captured BGRA pixels: no window API, no OCR, no I/O.

Measured geometry on the real client (dark theme, 988x225 strip, 2026-10-19):
  * the chat background is #1e1e1f and covers ~81% of the strip;
  * bubble fill is almost identical to the background, so a "bubble" is not
    visible as a block - only its glyphs are. The unit to find is a text block;
  * a solid 40 px vertical band (#2f2f30) runs down the left edge - it makes
    every row look "occupied" and must be skipped, otherwise no row can ever
    be classified as background;
  * the right edge carries a dark border, also skipped;
  * consecutive messages are separated by ~25 px of empty background.
"""

DIFF_THRESHOLD = 10      # per-pixel channel-sum distance from background
ROW_RATIO = 0.02         # share of non-background pixels for a row to count
COL_RATIO = 0.02         # same, for a column inside the chosen band
MIN_HEIGHT = 32          # a time label is ~24 px tall, a one-line message block ~40 px
# Narrower than this is not a readable line. Was 60: live 2026-10-20 15:06 a
# one-character incoming "嗯" measured 54 px wide in the maximized 2109 px strip
# and was dropped, so one-character messages were never seen. Time labels are
# kept out by MIN_HEIGHT, not by width.
MIN_WIDTH = 40
COL_GAP = 24             # horizontal background columns that split two blocks
LEFT_SKIP_RATIO = 0.05   # skip the solid vertical band on the left edge
RIGHT_SKIP_RATIO = 0.01  # skip the window border on the right edge
DIRECTION_MARGIN_RATIO = 0.05  # block margins closer than this are ambiguous


def _pixel(data, width, x, y):
    offset = (y * width + x) * 4
    return data[offset], data[offset + 1], data[offset + 2]


def _strip_background(width, height, data, step):
    """Most common colour of the whole strip: background dominates."""
    counts = {}
    for y in range(0, height, step):
        row = y * width * 4
        for x in range(0, width, step):
            o = row + x * 4
            key = (data[o], data[o + 1], data[o + 2])
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return _pixel(data, width, 0, 0)
    return max(counts, key=counts.get)


def is_incoming(box, width, *, margin_ratio=DIRECTION_MARGIN_RATIO):
    """True if the block hugs the left margin (the contact), False if it hugs
    the right one (the user), None when it is too centred to call.

    Measured on the client: an incoming one-line block starts at x=145 in a
    988 px strip and never reaches the right half for short text.
    """
    if not box or width <= 0:
        return None
    left, _top, right, _bottom = box
    if not 0 <= left < right <= width:
        return None
    left_margin = left
    right_margin = width - right
    if abs(left_margin - right_margin) < width * margin_ratio:
        return None
    return left_margin < right_margin


def text_blocks(width, height, data, *, diff=DIFF_THRESHOLD,
                row_ratio=ROW_RATIO, col_ratio=COL_RATIO,
                min_height=MIN_HEIGHT, min_width=MIN_WIDTH,
                col_gap=COL_GAP,
                left_skip_ratio=LEFT_SKIP_RATIO,
                right_skip_ratio=RIGHT_SKIP_RATIO,
                step=2):
    """Every qualifying text block in the strip, ordered top to bottom."""
    if width < 8 or height < 8 or len(data) != width * height * 4:
        return []
    if step < 1:
        raise ValueError("step must be >= 1")

    x0 = int(width * left_skip_ratio)
    x1 = width - int(width * right_skip_ratio)
    if x1 - x0 < 16:
        x0, x1 = 0, width

    br, bg, bb = _strip_background(width, height, data, max(1, step))

    def is_ink(x, y):
        o = (y * width + x) * 4
        return abs(data[o] - br) + abs(data[o + 1] - bg) + abs(data[o + 2] - bb) > diff

    bands = []
    span = len(range(x0, x1, step))
    for y in range(0, height, step):
        hits = sum(1 for x in range(x0, x1, step) if is_ink(x, y))
        if span and hits / span > row_ratio:
            if bands and y - bands[-1][1] <= step:
                bands[-1][1] = y
            else:
                bands.append([y, y])

    boxes = []
    for top, bottom in bands:
        if bottom - top + step < min_height:
            continue
        runs = []
        for x in range(x0, x1, step):
            hits = 0
            total = 0
            for y in range(top, bottom + 1, step):
                total += 1
                if is_ink(x, y):
                    hits += 1
            if total and hits / total > col_ratio:
                if runs and x - runs[-1][1] <= step:
                    runs[-1][1] = x
                else:
                    runs.append([x, x])
        if not runs:
            continue
        left, right = max(runs, key=lambda r: r[1] - r[0])
        right += step
        if right - left < min_width:
            continue
        boxes.append((left, top, right, bottom + step))
    return boxes


def bottom_gap(width, height, data, *, diff=DIFF_THRESHOLD, row_ratio=ROW_RATIO,
               left_skip_ratio=LEFT_SKIP_RATIO, right_skip_ratio=RIGHT_SKIP_RATIO,
               step=2, background_step=8):
    """Blank rows between the lowest ink row and the bottom edge of the strip.

    "Ink" means exactly what text_blocks() sees (same skips, same thresholds),
    but every row counts here, a time label included. Scanned from the bottom
    up, so the cost is the gap, not the strip. None when the strip has no ink
    at all or is unusable.

    At the bottom of a conversation the newest bubble sits a fixed padding above
    the bottom edge; scrolled up, content fills the strip and the gap is
    whatever the scroll position left - 0 when a message is cut by the edge.
    """
    if width < 8 or height < 8 or len(data) != width * height * 4:
        return None
    x0 = int(width * left_skip_ratio)
    x1 = width - int(width * right_skip_ratio)
    if x1 - x0 < 16:
        x0, x1 = 0, width
    br, bg, bb = _strip_background(width, height, data, max(1, background_step))
    columns = range(x0, x1, step)
    span = len(columns)
    for y in range(height - 1, -1, -1):
        row = y * width * 4
        hits = 0
        for x in columns:
            o = row + x * 4
            if abs(data[o] - br) + abs(data[o + 1] - bg) + abs(data[o + 2] - bb) > diff:
                hits += 1
        if hits / span > row_ratio:
            return height - 1 - y
    return None


def newest_bubble_box(width, height, data, **kwargs):
    """Return the bottom-most text block, or None."""
    boxes = text_blocks(width, height, data, **kwargs)
    return boxes[-1] if boxes else None

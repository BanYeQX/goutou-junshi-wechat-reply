"""Local system-time verification, never chat text recovery or name repair.

No capture, network, model, input, persistence or expected-time parameter.
An unresolved centered line remains unresolved. Do not choose a successful
scale, reinterpret a low score, or discard a time-shaped chat bubble.
"""
from collections import Counter
from dataclasses import replace

from visual_types import Box, VisualPause


def candidate_region(box, covered, width, height):
    return (box.inside(width, height) and box.top >= 8 and box.bottom <= height - 8
            and 8 <= box.height <= 50 and box.width <= min(300, width * 0.30)
            and abs((box.left + box.right) / 2 - width / 2) <= max(12, width * 0.06)
            and not any(box.overlaps(other) for other in covered))


def prepare_time_line(image, box, covered):
    """Uniform, reversible-in-memory contrast stretch; no binary threshold.

    Require the entire padded crop to avoid every bubble and its outside
    two-pixel ring to equal the ROI's background, not a media-card fill.
    """
    if not candidate_region(box, covered, image.width, image.height):
        raise VisualPause("时间行位置或边界未通过，保持不确定。")
    padded = Box(box.left - 8, box.top - 8, box.right + 8, box.bottom + 8)
    if not padded.inside(image.width, image.height) or any(padded.overlaps(other) for other in covered):
        raise VisualPause("时间行周边与消息块交叠，保持不确定。")
    background = Counter(image.getpixel((x, y)) for y in range(0, image.height, 12)
                         for x in (1, image.width - 2)).most_common(1)[0][0]
    crop = image.crop(padded.tuple()).convert("RGB")
    gray = None
    try:
        pixels = list(crop.get_flattened_data())
        if (max(background) - min(background) > 10
                or any(max(pixel) - min(pixel) > 10 for pixel in pixels)):
            raise VisualPause("时间行存在非灰度内容，保持不确定。")
        w, h = crop.size
        ring = (pixels[y * w + x] for y in range(h) for x in range(w)
                if x < 2 or x >= w - 2 or y < 2 or y >= h - 2)
        if any(pixel != background for pixel in ring):
            raise VisualPause("时间行周边背景不一致，可能有气泡或其他内容，保持不确定。")
        gray = crop.convert("L")
        lo, hi = gray.getextrema()
        bg = round(background[0] * 0.299 + background[1] * 0.587 + background[2] * 0.114)
        if hi - lo < 16 or min(abs(bg - lo), abs(bg - hi)) > 2:
            raise VisualPause("时间行对比度或背景未通过，保持不确定。")
        if bg < 128:
            table = [round(255 - min(255, max(0, value - bg) * 255 / max(1, hi - bg))) for value in range(256)]
        else:
            table = [round(min(255, max(0, value - lo) * 255 / max(1, bg - lo))) for value in range(256)]
        stretched = gray.point(table)
        try:
            return stretched.convert("RGB")
        finally:
            stretched.close()
    finally:
        crop.close()
        if gray is not None:
            gray.close()


def refine_time_lines(image, lines, covered, ocr, is_timestamp):
    """Global passes locate; strict local passes confirm a system-time value.

    No proposal with failed/missing dual-scale geometry may use this path.
    Only the dedicated typed layout records can carry a verification flag.
    """
    if not callable(getattr(ocr, "read_line_consistent", None)):
        return lines
    output = []
    for line in lines:
        if (not hasattr(line, "timestamp_verified") or not line.geometry_verified
                or not candidate_region(line.box, covered, image.width, image.height)):
            output.append(line)
            continue
        crop = None
        phase = "background"
        try:
            crop = prepare_time_line(image, line.box, covered)
            phase = "recognition"
            text = ocr.read_line_consistent(crop)
            phase = "format"
            if not is_timestamp(text):
                raise VisualPause("局部文字不是完整有效的系统时间，保持不确定。")
            output.append(replace(line, text=text, uncertainty="", timestamp_verified=True, uncertainty_code=""))
        except VisualPause:
            # Fixed labels only. Do not echo unknown OCR/error strings or use
            # the original global text as a fallback after local rejection.
            output.append(replace(line, text="", uncertainty="时间行候选局部校验未通过",
                                  timestamp_verified=False, uncertainty_code="timestamp_" + phase))
        finally:
            if crop is not None:
                crop.close()
    return tuple(output)

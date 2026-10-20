"""Conservative bubble/geometry classifier. No network, model or persistence."""
from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass, replace
from statistics import median

from PIL import Image, ImageChops

from visual_types import Box, Candidate, VisualPause, candidate_fingerprint, exact_title
from visual_sync import pixel_evidence
from visual_timestamps import refine_time_lines


@dataclass(frozen=True)
class Bubble:
    box: Box
    direction: str
    uncertainty: str


def frames_stable(first, second):
    return first.size == second.size and ImageChops.difference(first, second).getbbox() is None


def find_bubbles(image):
    """Require a solid fill component and a visible side tail, not text position.

    Unsupported themes, cut-off, oversized, tail-less media and ambiguous
    components remain unknown. This is not an image/voice semantic classifier.
    """
    factor = 2
    small = image.resize((max(1, image.width // factor), max(1, image.height // factor)), Image.Resampling.NEAREST).convert("RGB")
    width, height = small.size
    pixels = list(small.get_flattened_data())
    edge = [pixels[y * width + x] for y in range(0, height, 5) for x in (0, min(3, width - 1), max(0, width - 4), width - 1)]
    background = Counter(tuple(c // 8 for c in p) for p in edge).most_common(1)[0][0]
    bg = tuple(c * 8 + 4 for c in background)
    mask = bytearray(len(pixels))
    for i, (r, g, b) in enumerate(pixels):
        if g - r > 35 and g - b > 15 and g > 90:
            mask[i] = 2
        elif max(r, g, b) - min(r, g, b) < 25 and max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2])) >= 8:
            # Text pixels must not connect to the uniform bubble fill.
            if (sum(bg) < 360 and 35 <= (r + g + b) / 3 <= 160) or (sum(bg) >= 360 and min(r, g, b) >= 225):
                mask[i] = 1
    result = []
    for seed in range(len(mask)):
        color = mask[seed]
        if not color:
            continue
        mask[seed] = 0
        todo = deque([seed])
        count = 0
        x0, y0, x1, y1 = width, height, 0, 0
        lefts, rights = {}, {}
        while todo:
            i = todo.popleft()
            y, x = divmod(i, width)
            count += 1
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            lefts[y], rights[y] = min(lefts.get(y, x), x), max(rights.get(y, x), x)
            neighbours = []
            if x:
                neighbours.append(i - 1)
            if x + 1 < width:
                neighbours.append(i + 1)
            if y:
                neighbours.append(i - width)
            if y + 1 < height:
                neighbours.append(i + width)
            for neighbour in neighbours:
                if mask[neighbour] == color:
                    mask[neighbour] = 0
                    todo.append(neighbour)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if w * factor < 32 or h * factor < 24 or count < 150 or count / (w * h) < 0.60:
            continue
        box = Box(x0 * factor, y0 * factor, min(image.width, (x1 + 1) * factor), min(image.height, (y1 + 1) * factor))
        avatar_band = min(80, image.width * 0.10)
        if box.height <= 80 and box.width <= 80 and (box.right <= avatar_band or box.left >= image.width - avatar_band):
            continue
        direction, uncertainty = "unknown", "未确认普通文字气泡轮廓/方向"
        body_rows = range(y0 + max(2, h // 4), y1 - max(1, h // 6))
        body_left = median([lefts[y] for y in body_rows if y in lefts] or [x0])
        body_right = median([rights[y] for y in body_rows if y in rights] or [x1])
        left_tail, right_tail = body_left - x0 >= 2, x1 - body_right >= 2
        if left_tail and not right_tail and color == 1 and box.left < image.width * 0.35:
            direction, uncertainty = "friend", ""
        elif right_tail and not left_tail and color == 2 and box.right > image.width * 0.65:
            direction, uncertainty = "self", ""
        if box.top <= 2 or box.bottom >= image.height - 2 or box.left <= 2 or box.right >= image.width - 2:
            direction, uncertainty = "unknown", "气泡被区域边界裁切"
        if box.height > 280 or box.width > image.width * 0.90:
            uncertainty = "过大/跨区块，可能为文件、图片或复杂消息"
        result.append(Bubble(box, direction, uncertainty))
    small.close()
    return tuple(sorted(result, key=lambda b: (b.box.top, b.box.left)))


def media_or_system(text):
    return bool(re.search(r"\[?(图片|语音|视频|文件|表情|转账|收款|红包)\]?|微信转账|\.(pdf|docx?|xlsx?|zip|rar|mp4|png|jpe?g)(\b|$)|\b\d+(\.\d+)?\s*(KB|MB|GB)\b|^\d+\s*['\"″秒]+$|撤回了一条消息", text, re.I))


def timestamp(text):
    clock = r"(?:[01]?\d|2[0-3]):[0-5]\d"
    return bool(re.fullmatch(r"\s*(?:(?:今天|昨天|星期[一二三四五六日天]|周[一二三四五六日天])\s*)?" + clock + r"\s*|\d{4}年(?:[1-9]|1[0-2])月(?:[1-9]|[12]\d|3[01])日(?:\s+" + clock + r")?", text))


def system_timestamp(line, covered, width):
    # Exact text alone is insufficient: never hide a timestamp-shaped message
    # inside/crossing a bubble, off center, or in a large unexplained block.
    box = line.box
    return (not line.uncertainty and getattr(line, "timestamp_verified", True)
            and timestamp(line.text) and 8 <= box.height <= 50
            and box.width <= min(300, width * 0.30)
            and abs((box.left + box.right) / 2 - width / 2) <= max(12, width * 0.06)
            and not any(box.overlaps(bubble) for bubble in covered))


def assign_layout_lines(rows, covered, lines, width, height):
    """Attribute geometry, not uncertain text. Never make an unsafe row safe.

    Merge a gap only into a fully enclosing existing block. A gap in a safe
    bubble invalidates that bubble; an outside/crossing gap stays separate.
    """
    system_boxes, unknown_boxes = [], []
    for line in lines:
        if not line.box.inside(width, height):
            rows.append(Candidate("", "unknown", "unknown", Box(0, 0, width, height),
                                  candidate_fingerprint(b"", "unknown", "layout-bounds"),
                                  "整区复查行框越界，尚未定位；保持暂停"))
            continue
        owners = [i for i, box in enumerate(covered) if box.contains(line.box)]
        if getattr(line, "uncertainty", ""):
            if len(owners) == 1:
                index = owners[0]
                row = rows[index]
                reason = "整区复查与气泡识字存在不一致，未放行正文" if row.safe else row.uncertainty
                if not row.evidence_boxes:
                    reason += "；整区识字/定位疑点行已定位在此块内"
                rows[index] = replace(row, text="", kind="unknown" if row.safe else row.kind,
                                      uncertainty=reason, evidence_boxes=row.evidence_boxes + (line.box,))
            else:
                rows.append(Candidate("", "unknown", "unknown", line.box,
                                      candidate_fingerprint(b"", "unknown", str(line.box)),
                                      ("识字行触及消息区边界，可能被裁切；未按正文处理"
                                       if line.box.top <= 1 or line.box.bottom >= height - 1 else
                                       "已定位整区识字/定位疑点行；" + {
                                           "text_disagreement": "两尺度文字不一致",
                                           "geometry_disagreement": "两尺度行框定位不一致",
                                           "score_or_character": "行分数不足或含不明字符",
                                           "timestamp_background": "时间行局部校验未通过（背景或边界）",
                                           "timestamp_recognition": "时间行局部校验未通过（双尺度文字或行分数）",
                                           "timestamp_format": "时间行局部校验未通过（不是完整有效时间）",
                                       }.get(getattr(line, "uncertainty_code", ""), "不能归属唯一气泡") + "；未按正文处理"),
                                      evidence_boxes=(line.box,)))
                unknown_boxes.append(line.box)
            continue
        if owners:
            continue
        # A timestamp-shaped chat message inside a bubble is not discarded.
        if system_timestamp(line, covered, width):
            system_boxes.append(line.box)
            continue
        rows.append(Candidate("", "unknown", "unknown", line.box,
                              candidate_fingerprint(b"", "unknown", line.text),
                              "存在无法归属普通文字气泡的内容，需人工查看；未按正文处理",
                              evidence_boxes=(line.box,)))
        unknown_boxes.append(line.box)
    return rows, system_boxes, unknown_boxes


class VisualReader:
    def __init__(self, capture, ocr, title_ocr=None):
        self.capture = capture
        self.ocr = ocr
        self.title_ocr = title_ocr if title_ocr is not None else ocr
        self.last_evidence = None
        self.last_shadow_snapshot = None

    def title(self, calibration):
        image, = self.capture((calibration.title,))
        try:
            title, _ = self.title_ocr.read_consistent(image)
            if not exact_title(title, calibration.expected_title):
                # Do not read the message region after a failed title check.
                raise VisualPause("完整联系人标题不匹配或未识别清楚，未读取消息区。")
            return title, image.copy()
        finally:
            image.close()

    def _complete(self, title, rows, image, shadow_context, wait):
        rows = tuple(rows)
        if shadow_context is not None:
            if wait(0):
                raise VisualPause("本轮只读对照已停止或到期，结果作废。")
            from bubble_shadow import Bubble as TrackedBubble
            from bubble_image_adapter import enrich_snapshot
            context = replace(shadow_context, observed_title=title,
                              bubbles=tuple(TrackedBubble(row) for row in rows))
            try:
                enriched = enrich_snapshot(context, image)
            except ValueError:
                raise VisualPause("本轮气泡分块、裁切或外观证据不完整，停止只读对照；不猜测新旧。") from None
            if wait(0):
                raise VisualPause("本轮只读对照已停止或到期，结果作废。")
            self.last_shadow_snapshot = enriched
        return title, rows

    def poll(self, calibration, wait, *, shadow_context=None):
        self.last_evidence = None
        self.last_shadow_snapshot = None
        if shadow_context is not None:
            from bubble_shadow import BubbleShadowTracker, Snapshot
            if (not isinstance(shadow_context, Snapshot)
                    or BubbleShadowTracker._invalid(shadow_context)
                    or shadow_context.bubbles
                    or shadow_context.scope.binding != calibration.binding
                    or shadow_context.scope.profile_id != calibration.profile_id
                    or shadow_context.scope.expected_title != calibration.expected_title
                    or shadow_context.scope.message_roi != calibration.messages
                    or shadow_context.scope.calibration_revision != calibration.version):
                raise VisualPause("只读对照范围与本轮校准不符，未开始读取。")
        images = []
        try:
            title, title_a = self.title(calibration)
            images.append(title_a)
            first, = self.capture((calibration.messages,))
            images.append(first)
            if wait(0.45):
                raise VisualPause("识别已停止。")
            again, title_b = self.title(calibration)
            images.append(title_b)
            second, = self.capture((calibration.messages,))
            images.append(second)
            if title != again or not frames_stable(title_a, title_b) or not frames_stable(first, second):
                raise VisualPause("两帧画面不稳定、滚动或布局变化，已暂停；未将候选视为新消息。")
            # This is a bounded message ROI, not the desktop or another chat.
            # Default path retains hashes only. Explicit shadow mode additionally
            # derives bounded private bubble features before all images close.
            self.last_evidence = pixel_evidence(second)
            rows, covered = [], []
            bubbles = find_bubbles(second)
            for bubble in bubbles:
                crop = second.crop(bubble.box.tuple())
                try:
                    if bubble.uncertainty:
                        text, uncertainty, kind = "", bubble.uncertainty, "unknown"
                    else:
                        try:
                            text, _ = self.ocr.read_consistent(crop)
                            uncertainty = "" if text else "未识别到完整文字，不能确定是否为文字或媒体"
                            kind = "text_candidate" if text else "unknown"
                            if text and media_or_system(text):
                                kind = "non_text"
                                uncertainty = "图片/语音/文件/转账等候选，不当作普通文字"
                        except VisualPause:
                            text, uncertainty, kind = "", "正文识字分数、双尺度文字或定位未通过，需人工核对", "unknown"
                    rows.append(Candidate(text if kind == "text_candidate" else "", bubble.direction, kind, bubble.box,
                                          candidate_fingerprint(crop.tobytes(), bubble.direction, text), uncertainty))
                    covered.append(bubble.box)
                finally:
                    crop.close()
            # Unassigned OCR text is never silently treated as a message. This
            # catches timestamps (ignored only on exact pattern), file cards,
            # unsupported bubble styles and layout controls inside a bad ROI.
            try:
                scan = getattr(self.ocr, "scan_layout", self.ocr.recognize)
                all_lines = scan(second)
            except VisualPause:
                rows.append(Candidate("", "unknown", "unknown", Box(0, 0, second.width, second.height),
                                      candidate_fingerprint(second.tobytes(), "unknown", ""), "消息区域存在不能确认的识字间距/内容，需人工核对"))
                return self._complete(title, rows, second, shadow_context, wait)
            all_lines = refine_time_lines(second, all_lines, covered, self.ocr, timestamp)
            rows, system_boxes, unknown_boxes = assign_layout_lines(rows, covered, all_lines, second.width, second.height)
            self.last_evidence = replace(self.last_evidence, systems=tuple(system_boxes))
            # Image-only/media blocks with no OCR must not disappear silently.
            # Ignore only avatar side bands and exact recognized timestamps.
            edge_colors = [second.getpixel((x, y)) for y in range(0, second.height, 12) for x in (1, second.width - 2)]
            bg = Counter(edge_colors).most_common(1)[0][0]
            unassigned = []
            margin = round(min(80, second.width * 0.10))
            for y in range(6, second.height - 6, 8):
                for x in range(margin, second.width - margin, 8):
                    if any(b.left - 6 <= x <= b.right + 6 and b.top - 6 <= y <= b.bottom + 6 for b in covered + system_boxes + unknown_boxes):
                        continue
                    pixel = second.getpixel((x, y))
                    if max(abs(pixel[i] - bg[i]) for i in range(3)) > 25:
                        unassigned.append((x, y))
            if len(unassigned) > 12:
                box = Box(min(x for x, _ in unassigned), min(y for _, y in unassigned), max(x for x, _ in unassigned) + 1, max(y for _, y in unassigned) + 1)
                rows.append(Candidate("", "unknown", "non_text", box, candidate_fingerprint(b"", "unknown", str(box)),
                                      "检测到未归类视觉块，可能为图片/语音/文件；未当作普通文字"))
            return self._complete(title, sorted(rows, key=lambda row: (row.box.top, row.box.left)),
                                  second, shadow_context, wait)
        finally:
            for image in images:
                image.close()

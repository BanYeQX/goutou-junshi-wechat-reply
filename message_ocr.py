"""Strict offline message OCR, independent of contact identity and chat rules.

Uses the existing pinned local OCR weights uniformly, never an expected-text
hint or whichever backend happens to agree. Scores are line scores, not
calibrated probabilities or per-character confidence. Human review remains.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from local_ocr import OcrLine
from title_ocr import TITLE_OCR_VERSION, build_offline_engine
from visual_types import Box, VisualPause


MIN_MESSAGE_SCORE = 0.95
MAX_SCALED_SIDE = 4096
GEOMETRY_TOLERANCE = 4  # localization only; text comparison is still exact
MESSAGE_OCR_BACKEND = "rapidocr-" + TITLE_OCR_VERSION + "-strict-message"


@dataclass(frozen=True)
class LayoutLine(OcrLine):
    geometry_verified: bool = False
    timestamp_verified: bool = False
    uncertainty_code: str = ""


class LocalMessageOCR:
    def __init__(self, engine=None):
        # A dedicated sequential worker can share its pinned title engine's
        # weights. No expected title/text enters this adapter or the engine.
        self.engine = engine if engine is not None else build_offline_engine()

    def _recognize(self, image, scale, *, allow_uncertain):
        import numpy as np
        from PIL import Image
        if (not isinstance(image, Image.Image) or min(image.size) < 1
                or max(image.size) * 3 > MAX_SCALED_SIDE):
            raise VisualPause("正文区域无法完成独立双尺度识字，已暂停；未上传截图。")
        prepared = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS).convert("RGB")
        try:
            result = self.engine(np.asarray(prepared)[:, :, ::-1].copy())
        finally:
            prepared.close()
        texts = () if result.txts is None else tuple(result.txts)
        scores = () if result.scores is None else tuple(result.scores)
        boxes = () if result.boxes is None else tuple(result.boxes)
        if len(texts) != len(scores) or len(texts) != len(boxes) or len(texts) > 256:
            raise VisualPause("本地正文识字结果结构不完整，已暂停。")
        rows = []
        for text, score, corners in zip(texts, scores, boxes):
            polygon = np.asarray(corners, dtype=float)
            if (polygon.shape != (4, 2) or not np.isfinite(polygon).all()
                    or (polygon < 0).any() or (polygon[:, 0] > image.width * scale).any()
                    or (polygon[:, 1] > image.height * scale).any()):
                raise VisualPause("正文识字行框无效，已暂停。")
            box = Box(math.floor(float(polygon[:, 0].min()) / scale),
                      math.floor(float(polygon[:, 1].min()) / scale),
                      math.ceil(float(polygon[:, 0].max()) / scale),
                      math.ceil(float(polygon[:, 1].max()) / scale))
            if not box.inside(image.width, image.height):
                raise VisualPause("正文识字行框越界，已暂停。")
            valid = (math.isfinite(float(score)) and float(score) >= MIN_MESSAGE_SCORE
                     and isinstance(text, str) and bool(text.strip())
                     and not any(char in text for char in ("\ufffd", "\x00", "\n", "\r")))
            if not valid and not allow_uncertain:
                raise VisualPause("正文识字行分数不足或含不明字符，已暂停；仍需人工核对。")
            rows.append(OcrLine(text.strip() if valid else "", box,
                                "" if valid else "本地识字行分数不足或含不明字符"))
        return tuple(sorted(rows, key=lambda row: (row.box.top, row.box.left)))

    @staticmethod
    def _agree(first, second):
        return (first.text == second.text and not first.uncertainty and not second.uncertainty
                and all(abs(a - b) <= GEOMETRY_TOLERANCE
                        for a, b in zip(first.box.tuple(), second.box.tuple())))

    def recognize(self, image, scale=1.0):
        return self._recognize(image, scale, allow_uncertain=False)

    def read_consistent(self, image):
        first = self.recognize(image, 2.0)
        second = self.recognize(image, 3.0)
        if len(first) != len(second) or any(not self._agree(a, b) for a, b in zip(first, second)):
            raise VisualPause("正文双尺度识字或定位不一致，已暂停；需人工核对。")
        return "\n".join(row.text for row in first), first

    def read_line_consistent(self, image):
        """An already localized single line, not a replacement body detector.

        Caller must verify its geometry/background before using it as system
        metadata. Never accepts expected text. Restore upstream mutable flags
        even on error so the shared title engine still requires detection.
        """
        import numpy as np
        from PIL import Image
        if (not isinstance(image, Image.Image) or min(image.size) < 1
                or max(image.size) * 3 > MAX_SCALED_SIDE):
            raise VisualPause("局部单行区域无效，保持不确定。")
        readings = []
        for scale in (2, 3):
            prepared = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS).convert("RGB")
            previous = self.engine.use_det, self.engine.use_cls, self.engine.use_rec
            if previous != (True, False, True):
                prepared.close()
                raise VisualPause("局部识字引擎配置异常，保持不确定。")
            try:
                result = self.engine(np.asarray(prepared)[:, :, ::-1].copy(),
                                     use_det=False, use_cls=False, use_rec=True)
            finally:
                self.engine.use_det, self.engine.use_cls, self.engine.use_rec = previous
                prepared.close()
            texts = () if result.txts is None else tuple(result.txts)
            scores = () if result.scores is None else tuple(result.scores)
            if (len(texts) != 1 or len(scores) != 1 or not math.isfinite(float(scores[0]))
                    or float(scores[0]) < MIN_MESSAGE_SCORE or not isinstance(texts[0], str)
                    or not texts[0].strip() or any(c in texts[0] for c in ("\ufffd", "\x00", "\n", "\r"))):
                raise VisualPause("局部单行识字分数或字符未通过，保持不确定。")
            readings.append(texts[0].strip())
        if readings[0] != readings[1]:
            raise VisualPause("局部单行双尺度文字不一致，保持不确定。")
        return readings[0]

    def scan_layout(self, image):
        # Timestamps and stray lines need the same two scales and score gate.
        # Uncertain text is discarded, but its location is retained to pause.
        first = self._recognize(image, 2.0, allow_uncertain=True)
        second = self._recognize(image, 3.0, allow_uncertain=True)
        if len(first) != len(second):
            raise VisualPause("整区双尺度识字行数不一致，已暂停。")
        rows = []
        for a, b in zip(first, second):
            box = Box(min(a.box.left, b.box.left), min(a.box.top, b.box.top),
                      max(a.box.right, b.box.right), max(a.box.bottom, b.box.bottom))
            agreed = self._agree(a, b)
            geometry = all(abs(x - y) <= GEOMETRY_TOLERANCE for x, y in zip(a.box.tuple(), b.box.tuple()))
            code = ("score_or_character" if a.uncertainty or b.uncertainty else
                    "text_disagreement" if a.text != b.text else "geometry_disagreement" if not geometry else "")
            reason = {"score_or_character": "整区识字行分数不足或含不明字符",
                      "text_disagreement": "整区双尺度识字文字不一致",
                      "geometry_disagreement": "整区双尺度行框定位不一致"}.get(code, "")
            rows.append(LayoutLine(a.text if agreed else "", box, reason,
                                   geometry_verified=geometry, uncertainty_code=code))
        return tuple(rows)

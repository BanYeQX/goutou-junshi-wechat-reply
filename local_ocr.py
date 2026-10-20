"""Windows on-device OCR. Images remain in memory. No network or file input."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from collections import Counter

from visual_types import Box, VisualPause


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: Box
    uncertainty: str = ""


def located_line(words, box, allow_uncertain=False):
    try:
        return OcrLine(line_text(words), box)
    except VisualPause:
        if not allow_uncertain:
            raise
        # Do not retain uncertain text, replace characters, or widen gaps.
        return OcrLine("", box, "该行识字间距不确定，未按正文处理")


def line_text(words):
    """Reconstruct CJK words using their geometry, not a fuzzy title match.

    Windows inserts a separator between each recognized Han word. An actual
    wide/ambiguous gap is rejected instead of stripping arbitrary name spaces.
    """
    result = ""
    previous = None
    han = lambda char: bool(re.fullmatch(r"[\u3400-\u9fff]", char))
    for text, box in words:
        if not text:
            continue
        separator = ""
        if previous:
            old_text, old_box = previous
            gap = max(0, box.left - old_box.right)
            height = max(1, min(box.height, old_box.height))
            if han(old_text[-1]) and han(text[0]):
                if gap > height * 0.4:
                    raise VisualPause("汉字之间存在不能确认的间距，未猜测姓名或正文，请重新框选/人工核对。")
            elif old_text[-1].isascii() and text[0].isascii() and old_text[-1].isalnum() and text[0].isalnum():
                separator = " "
            elif gap > height * 0.4:
                separator = " "
        result += separator + text
        previous = text, box
    return result


class WindowsOCR:
    def __init__(self):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        available = [language.language_tag for language in OcrEngine.available_recognizer_languages]
        chosen = next((tag for tag in available if tag.lower().startswith("zh-hans")), None)
        if not chosen:
            raise VisualPause("本机未检测到 Windows 简体中文 OCR，未识别；不会替你安装语言包或修改系统。")
        self.engine = OcrEngine.try_create_from_language(Language(chosen))
        if self.engine is None:
            raise VisualPause("Windows 中文 OCR 未能启动，已停止。")
        self.language = chosen
        self.maximum = OcrEngine.max_image_dimension

    @staticmethod
    def languages():
        from winrt.windows.media.ocr import OcrEngine
        return [language.language_tag for language in OcrEngine.available_recognizer_languages]

    def recognize(self, image, scale=1.0):
        return self._recognize(image, scale, allow_uncertain=False)

    def scan_layout(self, image):
        """Locate bad lines without turning a single gap into a whole-ROI box.

        This does not authorize message text. Bubble recognition still uses
        strict recognize/read_consistent with the original agreement gates.
        """
        return self._recognize(image, 1.0, allow_uncertain=True)

    def _recognize(self, image, scale, *, allow_uncertain):
        from PIL import Image
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        if image.width < 1 or image.height < 1:
            raise VisualPause("OCR 区域为空。")
        factor = min(scale, self.maximum / max(image.size))
        prepared = image.resize((round(image.width * factor), round(image.height * factor)), Image.Resampling.LANCZOS).convert("RGBA")
        bitmap = SoftwareBitmap.create_copy_from_buffer(prepared.tobytes("raw", "BGRA"), BitmapPixelFormat.BGRA8, prepared.width, prepared.height)
        async def run():
            return await asyncio.wait_for(self.engine.recognize_async(bitmap), timeout=15)
        try:
            result = asyncio.run(run())
            angle = float(result.text_angle or 0) % 360
            self.last_angle = angle
            # TextAngle is an estimated overlay rotation, not confidence. Real
            # tests on unrotated Han text returned 2.2-2.5 degrees. Do not use
            # that noisy hint to assert that the window moved; window binding,
            # exact pixel stability and two OCR passes enforce those gates.
            rows = []
            for line in result.lines:
                words = list(line.words)
                boxes = [word.bounding_rect for word in words]
                if boxes:
                    box = Box(round(min(r.x for r in boxes) / factor), round(min(r.y for r in boxes) / factor),
                              round(max(r.x + r.width for r in boxes) / factor), round(max(r.y + r.height for r in boxes) / factor))
                    actual_words = [(word.text, Box(round(r.x / factor), round(r.y / factor), round((r.x + r.width) / factor), round((r.y + r.height) / factor))) for word, r in zip(words, boxes)]
                    rows.append(located_line(actual_words, box, allow_uncertain))
            return tuple(rows)
        except (TimeoutError, OSError) as exc:
            raise VisualPause("Windows 本地 OCR 超时或不可用，已暂停；未上传截图。") from None
        finally:
            bitmap.close()
            prepared.close()

    def read_consistent(self, image):
        from PIL import ImageOps
        # Bubble tails/green fill can be mistaken for tilted text. Normalize
        # foreground ink and add whitespace; do not relax agreement or accept
        # a failed pass. Both independently sized passes must still agree.
        gray = image.convert("L")
        sample = gray.resize((min(100, gray.width), min(100, gray.height)))
        background = Counter(sample.get_flattened_data()).most_common(1)[0][0]
        sample.close()
        if background < 120:
            table = [round(255 - min(255, max(0, value - background) * 255 / max(1, 255 - background))) for value in range(256)]
        else:
            table = [round(min(255, value * 255 / max(1, background))) for value in range(256)]
        # Preserve antialiased strokes; a hard binary threshold split 件 into
        # radicals in a real synthetic regression and must not be used here.
        clean = gray.point(table)
        padded = ImageOps.expand(clean, border=20, fill=255).convert("RGB")
        try:
            first = self.recognize(padded, 2.0)
            second = self.recognize(padded, 3.0)
        finally:
            padded.close()
            gray.close()
            clean.close()
        a = "\n".join(line.text.strip() for line in first).strip()
        b = "\n".join(line.text.strip() for line in second).strip()
        if a != b or "\ufffd" in a or "\x00" in a:
            raise VisualPause("两次本地识字结果不一致或含不明字符，已暂停，需人工核对。")
        return a, first

"""Local-only title OCR. No expected-name hints, fuzzy repair, capture or input.

Windows OCR confused a short, user-confirmed title in a real regression. This
engine is used uniformly for titles, not selected by whichever output matches
the expected name. Body OCR remains independent. Scores are not probabilities
of identity and never replace explicit target confirmation or exact matching.
"""
from __future__ import annotations

import hashlib
import math
from importlib.metadata import version
from pathlib import Path

from visual_types import VisualPause


TITLE_OCR_VERSION = "3.10.0"
MIN_TITLE_SCORE = 0.95
MODEL_HASHES = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
}


def array_only(image):
    """Never pass upstream's URL/path/encoded-image input options through."""
    import numpy as np
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise VisualPause("标题识别只接受本地内存像素，未读取外部输入。")
    return image


def verify_models(folder):
    for name, digest in MODEL_HASHES.items():
        path = Path(folder) / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise VisualPause("离线标题 OCR 资源缺失或校验失败，已暂停；不会联网下载。")


def build_offline_engine():
    import onnxruntime
    import rapidocr
    from rapidocr import RapidOCR
    if version("rapidocr") != TITLE_OCR_VERSION:
        raise VisualPause("离线标题 OCR 版本与已审阅版本不同，已暂停。")
    folder = Path(rapidocr.__file__).resolve().parent / "models"
    verify_models(folder)
    # Explicit, hash-checked local files bypass the upstream model downloader.
    # These exact weights contain the character dictionary in model metadata.
    onnxruntime.disable_telemetry_events()
    engine = RapidOCR(params={
        "Global.log_level": "critical", "Global.use_cls": False, "Global.text_score": 0.0,
        "Det.model_path": str(folder / "PP-OCRv6_det_small.onnx"),
        "Rec.model_path": str(folder / "PP-OCRv6_rec_small.onnx"),
        # Upstream defaults to a *minimum* short side of 736. A narrow
        # 600x120 title therefore becomes 3680x736 inside the detector, even
        # after our fixed scales. Bound its long side instead; retain the
        # same models, two scales, unique-line and >=.95 score checks.
        "Det.limit_type": "max", "Det.limit_side_len": 736,
        # Two intra-op workers stalled in native inference on this machine;
        # the otherwise-identical single-thread synthetic probe passed three
        # independent WinRT + ONNX workers. No OCR threshold/match relaxation.
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })
    engine.load_img = array_only
    return engine


class LocalTitleOCR:
    def __init__(self):
        self.engine = build_offline_engine()

    def _recognize(self, image, scale):
        import numpy as np
        from PIL import Image
        prepared = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS).convert("RGB")
        try:
            # Upstream ndarray inputs are BGR. No URL, path or raw-memory pointer
            # is accepted here. No visualization/export method is invoked.
            pixels = np.asarray(prepared)[:, :, ::-1].copy()
            result = self.engine(pixels)
        finally:
            prepared.close()
        texts = () if result.txts is None else tuple(result.txts)
        scores = () if result.scores is None else tuple(result.scores)
        if len(texts) != 1 or len(scores) != 1:
            raise VisualPause("完整标题未识别为唯一文字行，已暂停；未读取消息区。")
        text, score = texts[0], float(scores[0])
        if not math.isfinite(score) or score < MIN_TITLE_SCORE:
            raise VisualPause("标题识字分数不足，已暂停；未读取消息区。")
        if not isinstance(text, str) or not text.strip() or any(char in text for char in ("\ufffd", "\x00", "\n", "\r")):
            raise VisualPause("标题存在空白、多行或不明字符，已暂停。")
        return text.strip()

    def read_consistent(self, image):
        from PIL import Image
        if not isinstance(image, Image.Image) or min(image.size) < 1 or image.width * image.height > 1_000_000:
            raise VisualPause("标题像素区域无效，未识别。")
        first, second = self._recognize(image, 2.0), self._recognize(image, 3.0)
        if first != second:
            raise VisualPause("两次本地标题识字不一致，已暂停；未读取消息区。")
        return first, ()

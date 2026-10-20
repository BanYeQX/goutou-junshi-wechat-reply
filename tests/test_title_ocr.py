import dataclasses
import hashlib
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from title_ocr import LocalTitleOCR, TITLE_OCR_VERSION, array_only, build_offline_engine, verify_models
from visual_recognition import VisualReader
from visual_types import Box, Calibration, VisualPause, WindowBinding, exact_title


class LocalTitleTests(unittest.TestCase):
    def test_offline_engine_uses_single_native_thread_without_relaxing_rules(self):
        constructor, telemetry = Mock(), Mock()
        rapid = SimpleNamespace(__file__=str(Path(__file__)), RapidOCR=constructor)
        ort = SimpleNamespace(disable_telemetry_events=telemetry)
        with patch.dict(sys.modules, {"rapidocr": rapid, "onnxruntime": ort}), \
             patch("title_ocr.version", return_value=TITLE_OCR_VERSION), patch("title_ocr.verify_models") as verify:
            build_offline_engine()
        params = constructor.call_args.kwargs["params"]
        self.assertEqual(params["EngineConfig.onnxruntime.intra_op_num_threads"], 1)
        self.assertEqual(params["EngineConfig.onnxruntime.inter_op_num_threads"], 1)
        self.assertEqual(params["Det.limit_type"], "max")
        self.assertEqual(params["Det.limit_side_len"], 736)
        self.assertTrue(Path(params["Det.model_path"]).is_absolute())
        verify.assert_called_once()
        telemetry.assert_called_once()

    def test_detector_long_side_bound_avoids_short_title_overexpansion(self):
        from rapidocr.ch_ppocr_det.utils import DetPreProcess
        bounded = DetPreProcess(limit_side_len=736, limit_type="max")
        for width, height in ((600, 120), (900, 180), (204, 122), (306, 183), (2000, 50)):
            shape = bounded(np.zeros((height, width, 3), dtype=np.uint8)).shape
            self.assertLessEqual(max(shape[-2:]), 736)
        self.assertEqual(bounded(np.zeros((120, 600, 3), dtype=np.uint8)).shape, (1, 3, 128, 608))

    def make_ocr(self, texts=("小雨",), scores=(0.99,)):
        ocr = object.__new__(LocalTitleOCR)
        ocr.engine = Mock(return_value=SimpleNamespace(txts=texts, scores=scores))
        return ocr

    def test_consistent_title_uses_two_fixed_scales(self):
        ocr = self.make_ocr()
        image = Image.new("RGB", (100, 50), "white")
        with patch.object(ocr, "_recognize", return_value="小雨") as recognize:
            self.assertEqual(ocr.read_consistent(image)[0], "小雨")
        self.assertEqual([call.args[1] for call in recognize.call_args_list], [2.0, 3.0])

    def test_disagreeing_passes_still_pause(self):
        ocr = self.make_ocr()
        with patch.object(ocr, "_recognize", side_effect=["小雨", "干星"]):
            with self.assertRaisesRegex(VisualPause, "不一致"):
                ocr.read_consistent(Image.new("RGB", (100, 50)))

    def test_similar_character_is_never_repaired_to_expected_name(self):
        ocr = self.make_ocr(("干星",))
        text, _ = ocr.read_consistent(Image.new("RGB", (100, 50)))
        self.assertEqual(text, "干星")
        self.assertFalse(exact_title(text, "小雨"))

    def test_internal_name_spaces_are_preserved(self):
        ocr = self.make_ocr(("千 星",))
        text, _ = ocr.read_consistent(Image.new("RGB", (100, 50)))
        self.assertEqual(text, "千 星")
        self.assertFalse(exact_title(text, "小雨"))

    def test_low_nonfinite_or_missing_scores_pause(self):
        for score in (0.94, float("nan"), float("inf")):
            with self.assertRaises(VisualPause):
                self.make_ocr(scores=(score,)).read_consistent(Image.new("RGB", (100, 50)))
        with self.assertRaises(VisualPause):
            self.make_ocr(scores=()).read_consistent(Image.new("RGB", (100, 50)))

    def test_empty_multiple_and_malformed_lines_pause(self):
        for texts, scores in (((), ()), (("小雨", "其他"), (0.99, 0.99)), (("小雨\n其他",), (0.99,)), (("",), (0.99,)), (("千\ufffd",), (0.99,))):
            with self.assertRaises(VisualPause):
                self.make_ocr(texts, scores).read_consistent(Image.new("RGB", (100, 50)))

    def test_url_file_and_memory_pointer_inputs_are_not_accepted(self):
        for value in ("https://example.invalid/image.png", Path("image.png"), b"image", 123):
            with self.assertRaises(VisualPause):
                array_only(value)
        pixels = np.zeros((20, 40, 3), dtype=np.uint8)
        self.assertIs(array_only(pixels), pixels)

    def test_engine_only_receives_local_bgr_array(self):
        ocr = self.make_ocr()
        ocr.read_consistent(Image.new("RGB", (100, 50), (10, 20, 30)))
        for call in ocr.engine.call_args_list:
            pixels = call.args[0]
            self.assertIsInstance(pixels, np.ndarray)
            self.assertEqual(tuple(pixels[0, 0]), (30, 20, 10))

    def test_missing_or_corrupt_model_does_not_trigger_download(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            with patch("title_ocr.MODEL_HASHES", {"test.onnx": hashlib.sha256(b"expected").hexdigest()}):
                with self.assertRaisesRegex(VisualPause, "不会联网下载"):
                    verify_models(folder)
                (Path(folder) / "test.onnx").write_bytes(b"wrong")
                with self.assertRaises(VisualPause):
                    verify_models(folder)
                (Path(folder) / "test.onnx").write_bytes(b"expected")
                verify_models(folder)

    def test_title_interface_has_no_expected_name_hint(self):
        self.assertEqual(list(inspect.signature(LocalTitleOCR.read_consistent).parameters), ["self", "image"])

    def test_title_engine_is_separate_from_body_engine(self):
        binding = WindowBinding(1, 1, "test", "test", 700, 700, 144)
        c = Calibration(binding, "test", "小雨", Box(100, 10, 300, 60), Box(100, 80, 650, 620), True)
        title_ocr = self.make_ocr()
        body_ocr = SimpleNamespace(read_consistent=Mock(side_effect=AssertionError("body engine cannot select a title")))
        calls = []
        def capture(regions):
            calls.append(regions)
            return (Image.new("RGB", (200, 50)),)
        title, image = VisualReader(capture, body_ocr, title_ocr=title_ocr).title(c)
        image.close()
        self.assertEqual(title, "小雨")
        body_ocr.read_consistent.assert_not_called()
        title_ocr.engine.return_value.txts = ("干星",)
        with self.assertRaisesRegex(VisualPause, "标题不匹配"):
            VisualReader(capture, body_ocr, title_ocr=title_ocr).poll(c, lambda _: False)
        self.assertEqual(calls, [(c.title,), (c.title,)])


if __name__ == "__main__":
    unittest.main()

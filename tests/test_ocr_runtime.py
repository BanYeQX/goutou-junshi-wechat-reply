import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ocr_runtime
from visual_types import VisualPause


class RuntimeGuardTests(unittest.TestCase):
    def test_only_explicit_bundle_runtime_is_preloaded(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            expected = Path(folder) / "msvcp140.dll"
            expected.touch()
            kernel, runtime = Mock(), object()
            kernel.GetModuleHandleW.return_value = 0
            loader = Mock(side_effect=[kernel, runtime])
            with patch.object(ocr_runtime, "_runtime", None), patch.object(ocr_runtime.sys, "frozen", True, create=True), \
                 patch.object(ocr_runtime.sys, "_MEIPASS", folder, create=True), patch.object(ocr_runtime.ctypes, "WinDLL", loader):
                ocr_runtime.ensure_ocr_runtime()
                ocr_runtime.ensure_ocr_runtime()
                self.assertEqual(loader.call_count, 2)
                self.assertEqual(loader.call_args.args[0], str(expected))
                self.assertEqual(loader.call_args.kwargs["winmode"], 0x1100)

    def test_conflicting_loaded_runtime_is_rejected_not_replaced(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            (Path(folder) / "msvcp140.dll").touch()
            kernel = Mock()
            kernel.GetModuleHandleW.return_value = 123
            def filename(handle, buffer, length):
                buffer.value = str(Path(folder) / "winrt" / "msvcp140.dll")
                return len(buffer.value)
            kernel.GetModuleFileNameW.side_effect = filename
            loader = Mock(return_value=kernel)
            with patch.object(ocr_runtime, "_runtime", None), patch.object(ocr_runtime.sys, "frozen", True, create=True), \
                 patch.object(ocr_runtime.sys, "_MEIPASS", folder, create=True), patch.object(ocr_runtime.ctypes, "WinDLL", loader):
                with self.assertRaisesRegex(VisualPause, "不同来源"):
                    ocr_runtime.ensure_ocr_runtime()
            self.assertEqual(loader.call_count, 1)

    def test_missing_runtime_never_downloads_or_loads_fallback(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            loader = Mock(return_value=Mock())
            with patch.object(ocr_runtime, "_runtime", None), patch.object(ocr_runtime.sys, "frozen", True, create=True), \
                 patch.object(ocr_runtime.sys, "_MEIPASS", folder, create=True), patch.object(ocr_runtime.ctypes, "WinDLL", loader):
                with self.assertRaisesRegex(VisualPause, "不会安装或下载"):
                    ocr_runtime.ensure_ocr_runtime()
            self.assertEqual(loader.call_count, 1)


if __name__ == "__main__":
    unittest.main()

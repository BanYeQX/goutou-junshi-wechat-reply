"""Use one app-local C++ runtime before importing WinRT/ONNX.

No installation, DLL replacement or process injection. A known conflicting
module already loaded in this process is rejected rather than replaced.
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from visual_types import VisualPause


_runtime = None


def ensure_ocr_runtime():
    global _runtime
    if _runtime is not None:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint
    if getattr(sys, "frozen", False):
        wanted = Path(sys._MEIPASS) / "msvcp140.dll"
    else:
        directory = ctypes.create_unicode_buffer(32768)
        if not kernel.GetSystemDirectoryW(directory, len(directory)):
            raise VisualPause("无法定位本地 OCR 运行库，未开始识别。")
        wanted = Path(directory.value) / "msvcp140.dll"
    if not wanted.is_file():
        raise VisualPause("本地 OCR 运行库缺失，未开始识别；不会安装或下载。")
    handle = kernel.GetModuleHandleW("msvcp140.dll")
    if handle:
        loaded = ctypes.create_unicode_buffer(32768)
        if not kernel.GetModuleFileNameW(handle, loaded, len(loaded)) or Path(loaded.value).resolve() != wanted.resolve():
            raise VisualPause("检测到不同来源的 OCR 运行库，已暂停；请重新打开聊天台，不需重启微信。")
    # Absolute app/system path only. Keep the module reference alive.
    _runtime = ctypes.WinDLL(str(wanted), winmode=0x00000100 | 0x00001000)


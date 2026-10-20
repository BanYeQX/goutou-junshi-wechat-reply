"""Product's read-only Windows capture boundary. No input or activation APIs.

    Only bound Tencent Weixin window regions are captured, locally in memory.
    This module intentionally has no click, focus, key, clipboard or send method.
"""
from __future__ import annotations

import ctypes
import functools
from ctypes import wintypes
from pathlib import Path

from visual_types import Box, VisualPause, WindowBinding


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG), ("yppm", wintypes.LONG),
                ("used", wintypes.DWORD), ("important", wintypes.DWORD)]


class BitmapInfo(ctypes.Structure):
    _fields_ = [("header", BitmapInfoHeader), ("colors", wintypes.DWORD * 1)]


def physical_pixels(function):
    @functools.wraps(function)
    def wrapped(self, *args, **kwargs):
        old = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            if not old:
                raise VisualPause("无法建立本线程的物理像素坐标，未截取。")
            return function(self, *args, **kwargs)
        finally:
            if old:
                self.user.SetThreadDpiAwarenessContext(old)
    return wrapped


class WindowsCapture:
    def __init__(self):
        import win32api
        import win32gui
        import win32process
        self.api, self.gui, self.process = win32api, win32gui, win32process
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.GetDC.argtypes = [wintypes.HWND]
        self.user.GetDC.restype = wintypes.HDC
        self.user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user.ReleaseDC.restype = ctypes.c_int
        self.gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self.gdi.CreateCompatibleBitmap.restype = wintypes.HANDLE
        self.gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self.gdi.SelectObject.restype = wintypes.HANDLE
        self.gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        self.gdi.BitBlt.restype = wintypes.BOOL
        self.gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HANDLE, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BitmapInfo), wintypes.UINT]
        self.gdi.GetDIBits.restype = ctypes.c_int
        self.gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        self.gdi.DeleteObject.restype = wintypes.BOOL
        self.gdi.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi.DeleteDC.restype = wintypes.BOOL
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self.user.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.user.GetDpiForWindow.restype = wintypes.UINT
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetUserObjectInformationW.restype = wintypes.BOOL
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.user.CloseDesktop.restype = wintypes.BOOL

    def _exe(self, hwnd):
        _, pid = self.process.GetWindowThreadProcessId(hwnd)
        handle = self.api.OpenProcess(0x1000, False, pid)
        try:
            length = wintypes.DWORD(32768)
            text = ctypes.create_unicode_buffer(length.value)
            if not self.kernel.QueryFullProcessImageNameW(int(handle), 0, text, ctypes.byref(length)):
                raise OSError("无法读取窗口进程路径。")
            return pid, text.value
        finally:
            handle.Close()

    @staticmethod
    def trusted_exe(exe):
        candidate = Path(exe).resolve()
        allowed = (Path("C:/Program Files/Tencent/Weixin/Weixin.exe"), Path("C:/Program Files (x86)/Tencent/Weixin/Weixin.exe"))
        return any(candidate == path.resolve() for path in allowed)

    @physical_pixels
    def binding(self, hwnd):
        if not self.gui.IsWindow(hwnd):
            raise VisualPause("已绑定的微信窗口不存在，请重新校准。")
        pid, exe = self._exe(hwnd)
        if not self.trusted_exe(exe):
            raise VisualPause("目标不是已检查安装位置的微信窗口，拒绝读取。")
        left, top, right, bottom = self.gui.GetClientRect(hwnd)
        return WindowBinding(hwnd, pid, exe, self.gui.GetClassName(hwnd), right - left, bottom - top, self.user.GetDpiForWindow(hwnd))

    def windows(self):
        result = []
        def collect(hwnd, _):
            if not self.gui.IsWindowVisible(hwnd):
                return
            try:
                binding = self.binding(hwnd)
                if binding.width >= 400 and binding.height >= 300:
                    result.append((binding, self.gui.GetWindowText(hwnd)))
            except (OSError, VisualPause):
                pass
        self.gui.EnumWindows(collect, None)
        return result

    def _desktop_unlocked(self):
        desktop = self.user.OpenInputDesktop(0, False, 1)
        if not desktop:
            return False
        try:
            text = ctypes.create_unicode_buffer(256)
            length = wintypes.DWORD()
            return bool(self.user.GetUserObjectInformationW(desktop, 2, text, ctypes.sizeof(text), ctypes.byref(length))) and text.value.casefold() == "default"
        finally:
            self.user.CloseDesktop(desktop)

    def _screen_boxes(self, binding, regions):
        x, y = self.gui.ClientToScreen(binding.hwnd, (0, 0))
        return tuple(Box(x + r.left, y + r.top, x + r.right, y + r.bottom) for r in regions)

    @physical_pixels
    def verify(self, binding, regions):
        if not self._desktop_unlocked():
            raise VisualPause("桌面已锁定或不可访问，已暂停，未截屏。")
        if self.binding(binding.hwnd) != binding:
            raise VisualPause("微信窗口、进程、尺寸或 DPI 已改变，请重新校准。")
        if self.gui.IsIconic(binding.hwnd) or not self.gui.IsWindowVisible(binding.hwnd):
            raise VisualPause("微信已最小化或不可见，已暂停，未截屏。")
        if self.gui.GetForegroundWindow() != binding.hwnd:
            raise VisualPause("微信不在前台，已暂停。只读检查前请手动切到已绑定的微信窗口。")
        screen_boxes = self._screen_boxes(binding, regions)
        x0 = self.api.GetSystemMetrics(76)
        y0 = self.api.GetSystemMetrics(77)
        vw, vh = self.api.GetSystemMetrics(78), self.api.GetSystemMetrics(79)
        if any(r.left < x0 or r.top < y0 or r.right > x0 + vw or r.bottom > y0 + vh for r in screen_boxes):
            raise VisualPause("校准区域不完整地位于屏幕内，已暂停。")
        # Fail closed on any higher visible window intersecting a capture ROI,
        # including owned dialogs, notifications and always-on-top overlays.
        above = self.gui.GetTopWindow(0)
        while above and above != binding.hwnd:
            if self.gui.IsWindowVisible(above) and not self.gui.IsIconic(above):
                try:
                    rect = Box(*self.gui.GetWindowRect(above))
                    if rect.width > 1 and rect.height > 1 and any(rect.overlaps(r) for r in screen_boxes):
                        raise VisualPause("微信校准区域被其他窗口遮挡，已暂停，未截屏。")
                except self.gui.error:
                    raise VisualPause("窗口遮挡状态无法确认，已暂停。") from None
            above = self.gui.GetWindow(above, 2)
        if above != binding.hwnd:
            raise VisualPause("微信窗口层次无法确认，已暂停。")
        for r in screen_boxes:
            for px, py in ((r.left + 1, r.top + 1), (r.right - 2, r.top + 1), (r.left + 1, r.bottom - 2), (r.right - 2, r.bottom - 2), ((r.left + r.right) // 2, (r.top + r.bottom) // 2)):
                owner = self.gui.GetAncestor(self.gui.WindowFromPoint((px, py)), 2)
                if owner != binding.hwnd:
                    raise VisualPause("区域归属不明确或已遮挡，已暂停。")
        return screen_boxes

    @physical_pixels
    def capture(self, binding, regions):
        boxes = self.verify(binding, regions)
        images = []
        try:
            for box in boxes:
                images.append(self._grab_region(box))
            if self.verify(binding, regions) != boxes:
                raise VisualPause("截取期间窗口移动，画面已丢弃。")
            return tuple(images)
        except Exception:
            for image in images:
                image.close()
            raise

    def _grab_region(self, box):
        """Allocate/copy only the requested ROI, never capture a full desktop.

        Pillow's Windows ImageGrab(bbox=...) internally grabs the whole desktop
        before cropping, so it is deliberately not used by this connector.
        """
        from PIL import Image
        width, height = box.width, box.height
        if width <= 0 or height <= 0 or width * height > 30_000_000:
            raise VisualPause("区域为空或过大，未截取。")
        source = memory = bitmap = old = None
        try:
            source = self.user.GetDC(0)
            if not source:
                raise VisualPause("无法取得区域显示上下文，未截取。")
            memory = self.gdi.CreateCompatibleDC(source)
            bitmap = self.gdi.CreateCompatibleBitmap(source, width, height)
            if not memory or not bitmap:
                raise VisualPause("无法建立区域像素缓冲，未截取。")
            old = self.gdi.SelectObject(memory, bitmap)
            if not old or old == ctypes.c_void_p(-1).value:
                old = None
                raise VisualPause("区域位图未通过校验，未截取。")
            if not self.gdi.BitBlt(memory, 0, 0, width, height, source, box.left, box.top, 0x00CC0020):
                raise VisualPause("区域像素读取失败，已暂停。")
            self.gdi.SelectObject(memory, old)
            old = None
            info = BitmapInfo()
            info.header = BitmapInfoHeader(ctypes.sizeof(BitmapInfoHeader), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
            pixels = ctypes.create_string_buffer(width * height * 4)
            if self.gdi.GetDIBits(source, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
                raise VisualPause("区域像素不完整，画面已丢弃。")
            return Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX")
        finally:
            if old and memory:
                self.gdi.SelectObject(memory, old)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if memory:
                self.gdi.DeleteDC(memory)
            if source:
                self.user.ReleaseDC(0, source)

    def calibration_frame(self, binding):
        # One explicitly requested local calibration image. Never OCR the full
        # window; normal recognition captures only saved title/message ROIs.
        return self.capture(binding, (Box(0, 0, binding.width, binding.height),))[0]

"""Metadata/cursor for the user-run calibrator; import is inert. No capture/input.

All target-window reads reuse NativeGDI.describe/GetWindowRect. SetWindowPos
below is guarded to a caller-owned overlay HWND, NEVER the WeChat HWND.
"""
import ctypes as C
from ctypes import wintypes as W
import os

from maximized_calibration import CalibrationError, CANVAS, MODE, validate_snapshot


class MonitorInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("monitor", W.RECT), ("work", W.RECT),
                ("flags", W.DWORD), ("device", W.WCHAR * 32)]


def box(rect):
    return [rect.left, rect.top, rect.right, rect.bottom]


class CalibrationNative:
    def __init__(self, hwnd, pid, *, backend=None):
        # Only main(--run), never import/default CLI/tests, constructs real API.
        if backend is None:
            from capture_gdi import NativeGDI
            backend = NativeGDI()
            self._declare(backend.user)
        self.backend, self.user = backend, backend.user
        self.hwnd, self.pid = hwnd, pid
        self.overlay_hwnd = None
        self.monitor_callback = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)

    def _declare(self, user):
        def sig(name, result, *args):
            f = getattr(user, name)
            f.restype, f.argtypes = result, list(args)
        sig("IsZoomed", W.BOOL, W.HWND)
        sig("GetMonitorInfoW", W.BOOL, W.HANDLE, C.POINTER(MonitorInfo))
        sig("MonitorFromWindow", W.HANDLE, W.HWND, W.DWORD)
        callback = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)
        sig("EnumDisplayMonitors", W.BOOL, W.HDC, C.POINTER(W.RECT), callback, W.LPARAM)
        sig("GetCursorPos", W.BOOL, C.POINTER(W.POINT))
        sig("GetWindowDpiAwarenessContext", C.c_void_p, W.HWND)
        sig("SetWindowPos", W.BOOL, W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
        sig("GetWindowLongW", C.c_long, W.HWND, C.c_int)
        sig("GetLayeredWindowAttributes", W.BOOL, W.HWND, C.POINTER(W.DWORD), C.POINTER(W.BYTE), C.POINTER(W.DWORD))

    def snapshot(self):
        # No client-area origin or DWM visible-frame substitution.
        if not self.backend.is_pmv2():
            raise CalibrationError("exact_pmv2_required")
        scope = self.backend.describe(self.hwnd, expected_pid=self.pid)
        if not self.user.IsZoomed(self.hwnd):
            raise CalibrationError("maximized_window_required")
        monitors, errors = [], []
        def collect(handle, dc, area, data):
            try:
                info = MonitorInfo()
                info.size = C.sizeof(info)
                if not self.user.GetMonitorInfoW(handle, C.byref(info)):
                    raise CalibrationError("monitor_layout_query_failed")
                monitors.append({"device": info.device, "monitor_rect": box(info.monitor),
                                 "work_rect": box(info.work), "primary": bool(info.flags & 1)})
                return 1
            except Exception:
                errors.append(True)
                return 0
        if not self.user.EnumDisplayMonitors(None, None, self.monitor_callback(collect), 0) or errors:
            raise CalibrationError("monitor_layout_query_failed")
        target = self.user.MonitorFromWindow(self.hwnd, 0)  # No nearest-monitor fallback.
        info = MonitorInfo()
        info.size = C.sizeof(info)
        if not target or not self.user.GetMonitorInfoW(target, C.byref(info)):
            raise CalibrationError("window_monitor_query_failed")
        snapshot = {"window_rect": list(scope.rect), "window_origin": list(scope.rect[:2]),
                    "frame_size": [scope.rect[2]-scope.rect[0], scope.rect[3]-scope.rect[1]],
                    "dpi": scope.dpi, "dpi_awareness": MODE, "pmv2_verified": True,
                    "dpi_verification": "AreDpiAwarenessContextsEqual(-4)", "canvas": CANVAS,
                    "maximized": True, "window_monitor": info.device,
                    "monitors": sorted(monitors, key=lambda m: m["device"]),
                    "binding": {"hwnd": scope.hwnd, "pid": scope.pid, "created": scope.created,
                                "class_name": scope.class_name, "version": scope.version}}
        return validate_snapshot(snapshot)

    def cursor(self):
        if not self.backend.is_pmv2():
            raise CalibrationError("exact_pmv2_required")
        point = W.POINT()
        if not self.user.GetCursorPos(C.byref(point)):
            raise CalibrationError("cursor_query_failed")
        return point.x, point.y

    def own_window(self, hwnd):
        root = self.user.GetAncestor(hwnd, 2)  # GA_ROOT, Tk wrapper if present.
        pid = W.DWORD()
        if not root or not self.user.GetWindowThreadProcessId(root, C.byref(pid)) or pid.value != os.getpid():
            raise CalibrationError("not_an_owned_calibration_window")
        if root == self.hwnd:
            raise CalibrationError("not_an_owned_calibration_window")
        if not self.backend.is_pmv2(self.user.GetWindowDpiAwarenessContext(root)):
            raise CalibrationError("calibration_window_not_pmv2")
        return root

    def place_overlay(self, hwnd, virtual_rect):
        root = self.own_window(hwnd)
        l, t, r, b = virtual_rect
        # Absolute negative desktop coordinates: Tk's '-X' means right anchoring,
        # so use this OWN-window-only call, not Tk geometry signs.
        if not self.user.SetWindowPos(root, None, l, t, r-l, b-t, 0x0004 | 0x0010):
            raise CalibrationError("overlay_geometry_failed")  # NOZORDER | NOACTIVATE
        actual = W.RECT()
        if not self.user.GetWindowRect(root, C.byref(actual)) or box(actual) != list(virtual_rect):
            raise CalibrationError("overlay_geometry_failed")
        style = self.user.GetWindowLongW(root, -20)
        key, alpha, flags = W.DWORD(), W.BYTE(), W.DWORD()
        if (style & 0x20 or not self.user.GetLayeredWindowAttributes(root, C.byref(key), C.byref(alpha), C.byref(flags))
                or flags.value != 2 or not 1 <= alpha.value <= 254):
            raise CalibrationError("overlay_click_through_or_transparency_unknown")
        # Uniform nonzero alpha, no color-key holes, no WS_EX_TRANSPARENT.
        self.overlay_hwnd = root


def binding_from_document(document):
    from maximized_calibration import target_name
    target_name(document)                 # any calibrated contact, not one fixed name
    binding = document.get("binding", {})
    hwnd, pid = binding.get("hwnd"), binding.get("pid")
    if any(type(x) is not int or x <= 0 for x in (hwnd, pid)):
        raise CalibrationError("saved_contact_binding_required")
    return hwnd, pid

"""Narrow ctypes/GDI backend. Import does not load DLLs or query any window.

No pywin32/PIL/numpy, GDI desktop source, input, window/system mutation or disk output.
Physical-coordinate context is thread-local and restored, never system scaling.
Public OS metadata and GetProcessTimes are not process-address-space reads.
PrintWindow may block indefinitely: isolate live calls in a bounded worker.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
from functools import wraps
import os

from wechat_capture import CaptureError, PixelImage, WindowScope, capture_failure


class BitmapHeader(C.Structure):
    _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
                ("image_size", W.DWORD), ("xppm", W.LONG), ("yppm", W.LONG),
                ("used", W.DWORD), ("important", W.DWORD)]


class BitmapInfo(C.Structure):
    _fields_ = [("header", BitmapHeader), ("colors", W.DWORD*3)]


class FixedVersion(C.Structure):
    _fields_ = [(name, W.DWORD) for name in
                ("signature", "structure", "file_ms", "file_ls", "product_ms", "product_ls",
                 "mask", "flags", "os", "type", "subtype", "date_ms", "date_ls")]


def filetime_ticks(value):
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def physical_coordinates(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.physical_pixels():
            return method(self, *args, **kwargs)
    return wrapped


def metadata_errors(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as error:
            raise capture_failure(error, "window_metadata") from None
    return wrapped


class NativeGDI:
    def __init__(self, *, user=None, gdi=None, kernel=None, version=None):
        supplied = (user, gdi, kernel, version)
        if all(v is None for v in supplied):
            if os.name != "nt":
                raise CaptureError("windows_only", stage="backend_init")
            try:
                user = C.WinDLL("user32", use_last_error=True)
                gdi = C.WinDLL("gdi32", use_last_error=True)
                kernel = C.WinDLL("kernel32", use_last_error=True)
                version = C.WinDLL("version", use_last_error=True)
            except Exception:
                raise CaptureError("backend_library_load_failed", stage="backend_init") from None
            try:
                self._declare(user, gdi, kernel, version)
            except Exception:
                raise CaptureError("backend_api_declaration_failed", stage="backend_init") from None
        elif any(v is None for v in supplied):
            raise CaptureError("incomplete_native_fixture", stage="backend_init")
        self.user, self.gdi, self.kernel, self.version_api = user, gdi, kernel, version
        self._version_cache = None

    @staticmethod
    def _declare(u, g, k, v):
        def sig(dll, name, result, *args):
            function = getattr(dll, name)
            function.restype, function.argtypes = result, list(args)
        for name in ("IsWindow", "IsWindowVisible", "IsIconic"):
            sig(u, name, W.BOOL, W.HWND)
        sig(u, "GetWindowRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
        sig(u, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
        sig(u, "GetWindowTextW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
        sig(u, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
        sig(u, "GetDpiForWindow", W.UINT, W.HWND)
        sig(u, "GetThreadDpiAwarenessContext", C.c_void_p)
        sig(u, "GetAwarenessFromDpiAwarenessContext", C.c_int, C.c_void_p)
        sig(u, "AreDpiAwarenessContextsEqual", W.BOOL, C.c_void_p, C.c_void_p)
        sig(u, "SetThreadDpiAwarenessContext", C.c_void_p, C.c_void_p)
        sig(u, "GetWindowDC", W.HDC, W.HWND)
        sig(u, "ReleaseDC", C.c_int, W.HWND, W.HDC)
        sig(u, "PrintWindow", W.BOOL, W.HWND, W.HDC, W.UINT)
        sig(u, "WindowFromPoint", W.HWND, W.POINT)
        sig(u, "GetAncestor", W.HWND, W.HWND, W.UINT)
        sig(u, "GetDesktopWindow", W.HWND)
        sig(u, "GetSystemMetrics", C.c_int, C.c_int)
        sig(g, "CreateCompatibleDC", W.HDC, W.HDC)
        sig(g, "CreateCompatibleBitmap", W.HBITMAP, W.HDC, C.c_int, C.c_int)
        sig(g, "SelectObject", C.c_void_p, W.HDC, C.c_void_p)
        sig(g, "DeleteObject", W.BOOL, C.c_void_p)
        sig(g, "DeleteDC", W.BOOL, W.HDC)
        sig(g, "PatBlt", W.BOOL, W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.DWORD)
        sig(g, "GetDIBits", C.c_int, W.HDC, W.HBITMAP, W.UINT, W.UINT,
            C.c_void_p, C.POINTER(BitmapInfo), W.UINT)
        sig(k, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
        sig(k, "CloseHandle", W.BOOL, W.HANDLE)
        sig(k, "GetProcessTimes", W.BOOL, W.HANDLE, *([C.POINTER(W.FILETIME)]*4))
        sig(k, "QueryFullProcessImageNameW", W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
        sig(v, "GetFileVersionInfoSizeW", W.DWORD, W.LPCWSTR, C.POINTER(W.DWORD))
        sig(v, "GetFileVersionInfoW", W.BOOL, W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p)
        sig(v, "VerQueryValueW", W.BOOL, C.c_void_p, W.LPCWSTR, C.POINTER(C.c_void_p), C.POINTER(W.UINT))

    @contextmanager
    def _process_handle(self, pid, open_reason):
        C.set_last_error(0)  # Own ctypes thread-local copy, not a system setting.
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED only.
        if not handle:
            # Read immediately, before cleanup or ANY further native call.
            try:
                error_number = C.get_last_error()
            except Exception:
                error_number = None  # Failed diagnostics must not replace OpenProcess failure.
            raise CaptureError(open_reason, stage="window_metadata", win32_error=error_number)
        primary = None
        try:
            yield handle
        except BaseException as error:
            primary = capture_failure(error, "window_metadata")
        finally:
            try:
                if not self.kernel.CloseHandle(handle):
                    raise CaptureError("process_handle_release_failed", stage="process_handle_release")
            except BaseException as error:
                cleanup = capture_failure(error, "process_handle_release")
                if primary is None:
                    primary = cleanup
                else:
                    primary.add_cleanup(cleanup, "process_handle_release")
        if primary is not None:
            raise primary from None

    @metadata_errors
    def process_times(self, pid):
        if type(pid) is not int or pid <= 0:
            raise CaptureError("invalid_process_id")
        with self._process_handle(pid, "process_query_denied") as handle:
            creation, exit_time, kernel, user = (W.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(handle, C.byref(creation), C.byref(exit_time),
                                               C.byref(kernel), C.byref(user)):
                raise CaptureError("process_times_failed")
            return filetime_ticks(creation), filetime_ticks(kernel), filetime_ticks(user)

    def cpu_seconds(self, pid):
        _, kernel, user = self.process_times(pid)
        return (kernel+user)/10_000_000

    @metadata_errors
    def _file_version(self, pid, created):
        key = pid, created
        if self._version_cache is not None and self._version_cache[0] == key:
            return self._version_cache[1]
        with self._process_handle(pid, "version_process_query_failed") as handle:
            path, length = C.create_unicode_buffer(32768), W.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, C.byref(length)):
                raise CaptureError("process_path_query_failed")
        unused = W.DWORD()
        size = self.version_api.GetFileVersionInfoSizeW(path.value, C.byref(unused))
        if not 0 < size <= 1_048_576:
            raise CaptureError("version_info_unavailable")
        buffer = C.create_string_buffer(size)
        if not self.version_api.GetFileVersionInfoW(path.value, 0, size, buffer):
            raise CaptureError("version_info_unavailable")
        pointer, length = C.c_void_p(), W.UINT()
        if (not self.version_api.VerQueryValueW(buffer, "\\", C.byref(pointer), C.byref(length))
                or not pointer.value or length.value < C.sizeof(FixedVersion)):
            raise CaptureError("version_info_invalid")
        fixed = C.cast(pointer, C.POINTER(FixedVersion)).contents
        if fixed.signature != 0xFEEF04BD:
            raise CaptureError("version_signature_invalid")
        version = ".".join(str(n) for n in (fixed.file_ms >> 16, fixed.file_ms & 65535,
                                           fixed.file_ls >> 16, fixed.file_ls & 65535))
        self._version_cache = key, version
        return version

    @contextmanager
    def physical_pixels(self):
        try:
            old = self.user.GetThreadDpiAwarenessContext()
            if not old:
                raise CaptureError("thread_dpi_query_failed")
            awareness = self.user.GetAwarenessFromDpiAwarenessContext(old)
            if awareness not in (0, 1, 2):
                raise CaptureError("thread_dpi_query_failed")
        except CaptureError as error:
            raise capture_failure(error, "thread_dpi") from None
        except Exception:
            raise CaptureError("thread_dpi_query_failed", stage="thread_dpi") from None
        if awareness == 2 and self.is_pmv2(old):
            yield
            return
        try:
            previous = self.user.SetThreadDpiAwarenessContext(C.c_void_p(-4))  # Own thread, PMv2.
        except Exception:
            raise CaptureError("physical_dpi_context_required", stage="thread_dpi") from None
        if type(previous) is not int or not previous:
            raise CaptureError("physical_dpi_context_required", stage="thread_dpi")
        primary = None
        try:
            try:
                current = self.user.GetThreadDpiAwarenessContext()
                if self.user.GetAwarenessFromDpiAwarenessContext(current) != 2 or not self.is_pmv2(current):
                    raise CaptureError("physical_dpi_context_required")
            except Exception as error:
                raise capture_failure(error, "thread_dpi") from None
            yield
        except BaseException as error:
            primary = error
        finally:
            try:
                if not self.user.SetThreadDpiAwarenessContext(previous):
                    raise CaptureError("thread_dpi_restore_failed", stage="thread_dpi_restore")
            except BaseException as error:
                cleanup = capture_failure(error, "thread_dpi_restore")
                if primary is None:
                    primary = cleanup
                else:
                    primary = capture_failure(primary, "unclassified").add_cleanup(cleanup, "thread_dpi_restore")
        if primary is not None:
            raise primary from None

    def is_pmv2(self, context=None):
        """Exact V2 comparison; GetAwareness(...)=2 alone also accepts V1."""
        if context is None:
            context = self.user.GetThreadDpiAwarenessContext()
        return bool(context) and self.user.AreDpiAwarenessContextsEqual(context, C.c_void_p(-4)) == 1

    @physical_coordinates
    @metadata_errors
    def describe(self, hwnd, *, expected_pid=None):
        u = self.user
        if expected_pid is not None and (type(expected_pid) is not int or expected_pid <= 0):
            raise CaptureError("invalid_process_id", stage="binding_validation")
        pid_match = None
        # Never change system scaling / process-global DPI or guess coordinates.
        if not self.is_pmv2():
            raise CaptureError("physical_dpi_context_required", stage="thread_dpi")
        if not u.IsWindow(hwnd):
            raise CaptureError("window_handle_invalid")
        if not u.IsWindowVisible(hwnd):
            raise CaptureError("window_not_visible")
        if u.IsIconic(hwnd):
            raise CaptureError("window_minimized")
        rect, pid = W.RECT(), W.DWORD()
        name, title = C.create_unicode_buffer(256), C.create_unicode_buffer(512)
        for function, args, reason in (
                (u.GetWindowRect, (hwnd, C.byref(rect)), "window_rect_query_failed"),
                (u.GetWindowThreadProcessId, (hwnd, C.byref(pid)), "window_pid_query_failed"),
                (u.GetClassNameW, (hwnd, name, len(name)), "window_class_query_failed"),
                (u.GetWindowTextW, (hwnd, title, len(title)), "window_title_query_failed")):
            try:
                valid = function(*args)
            except Exception:
                raise CaptureError(reason, stage="window_metadata", pid_matches_saved=pid_match) from None
            if not valid:
                raise CaptureError(reason, stage="window_metadata", pid_matches_saved=pid_match)
            if reason == "window_pid_query_failed" and expected_pid is not None:
                pid_match = int(pid.value) == expected_pid
                if not pid_match:
                    raise CaptureError("window_binding_changed", stage="binding_validation", pid_matches_saved=False)
        try:
            dpi = int(u.GetDpiForWindow(hwnd))
            if dpi <= 0:
                raise CaptureError("window_dpi_query_failed")
            created, _, _ = self.process_times(int(pid.value))
            scope = WindowScope(int(hwnd), int(pid.value), created,
                                (rect.left, rect.top, rect.right, rect.bottom), dpi,
                                self._file_version(int(pid.value), created), name.value, title.value)
            scope.validate()
            return scope
        except Exception as error:
            primary = capture_failure(error, "window_metadata")
            primary.pid_matches_saved = pid_match
            raise primary from None

    @physical_coordinates
    def root_from_point(self, point):
        x, y = point
        # Off-desktop points cannot count as successful coverage.
        l, t, w, h = (int(self.user.GetSystemMetrics(n)) for n in (76, 77, 78, 79))
        if not l <= x < l+w or not t <= y < t+h:
            raise CaptureError("grid_point_off_desktop")
        hit = self.user.WindowFromPoint(W.POINT(x, y))
        root = self.user.GetAncestor(hit, 2) if hit else 0  # GA_ROOT, not ROOTOWNER.
        if not root or root == self.user.GetDesktopWindow():
            raise CaptureError("grid_hit_test_unknown")
        return int(root)

    @physical_coordinates
    def grab(self, hwnd, width, height):
        if (type(hwnd) is not int or hwnd <= 0 or type(width) is not int or type(height) is not int
                or width <= 0 or height <= 0 or width > 6000 or height > 4000 or width*height > 16_000_000):
            raise CaptureError("invalid_native_capture_scope")
        source = memory = bitmap = old = None
        image = error = None
        try:
            source = self.user.GetWindowDC(hwnd)
            if not source:
                raise CaptureError("window_dc_failed")
            memory = self.gdi.CreateCompatibleDC(source)
            if not memory:
                raise CaptureError("memory_dc_failed")
            bitmap = self.gdi.CreateCompatibleBitmap(source, width, height)
            if not bitmap:
                raise CaptureError("bitmap_allocation_failed")
            selected = self.gdi.SelectObject(memory, bitmap)
            if not selected or selected == C.c_void_p(-1).value:
                raise CaptureError("bitmap_selection_failed")
            old = selected
            if not self.gdi.PatBlt(memory, 0, 0, width, height, 0x42):
                raise CaptureError("bitmap_clear_failed")
            if not self.user.PrintWindow(hwnd, memory, 2):
                raise CaptureError("printwindow_failed")
            restored = self.gdi.SelectObject(memory, old)
            if not restored or restored == C.c_void_p(-1).value:
                raise CaptureError("bitmap_restore_failed")
            old = None  # GetDIBits requires bitmap NOT selected into a DC.
            info = BitmapInfo()
            info.header = BitmapHeader(C.sizeof(BitmapHeader), width, -height, 1, 32, 0,
                                       width*height*4, 0, 0, 0, 0)
            pixels = C.create_string_buffer(width*height*4)
            if self.gdi.GetDIBits(source, bitmap, 0, height, pixels, C.byref(info), 0) != height:
                raise CaptureError("incomplete_bitmap_read")
            image = PixelImage(width, height, pixels.raw)
        except Exception as exc:
            error = exc if isinstance(exc, CaptureError) else CaptureError("native_capture_failed")
        finally:
            failed = False
            actions = []
            if old and memory:
                actions.append((self.gdi.SelectObject, (memory, old)))
            if bitmap:
                actions.append((self.gdi.DeleteObject, (bitmap,)))
            if memory:
                actions.append((self.gdi.DeleteDC, (memory,)))
            if source:
                actions.append((self.user.ReleaseDC, (hwnd, source)))
            for function, args in actions:
                try:
                    value = function(*args)
                    if not value or value == C.c_void_p(-1).value:
                        failed = True
                except Exception:
                    failed = True
            if failed:
                error = CaptureError("native_cleanup_failed")
        if error is not None:
            raise error from None
        return image

"""Guarded, default-off window primitives adapted from Luofeng-Cloud (MIT).

Pinned origin d39948512b34e797c5763538f59a70af53ca7a97; see third_party/luofeng.
No Win32 calls/imports happen until explicitly supplied native API is invoked.
No clipboard implementation, global keyboard injection, auto-restoration,
thread-input attachment, topmost changes, desktop capture or retry exists here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import threading
import time

from visual_types import Box


class UnsupportedCapability(RuntimeError):
    """Fixed capability code only; no chat, clipboard or native error text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WindowActionGrant:
    # A future authorization UI must obtain human consent; creating this object
    # is NOT proof of consent. Nothing in the formal UI mints these grants.
    binding: object
    expires_at: float
    allowed: frozenset[str] = frozenset()
    revoked: threading.Event = field(default_factory=threading.Event, repr=False)


class NativeWindowAPI:
    """Lazy narrow Win32 facade. Does not find, open or change conversations."""
    def __init__(self):
        self._capture = None

    def _load(self):
        if self._capture is None:
            from ctypes import wintypes
            from visual_capture import WindowsCapture
            cap = WindowsCapture()
            cap.user.SetForegroundWindow.argtypes = [wintypes.HWND]
            cap.user.SetForegroundWindow.restype = wintypes.BOOL
            cap.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            cap.user.PostMessageW.restype = wintypes.BOOL
            self._capture = cap
        return self._capture

    def verify(self, binding):
        cap = self._load()
        return (cap._desktop_unlocked() and cap.binding(binding.hwnd) == binding
                and bool(cap.gui.IsWindowVisible(binding.hwnd)) and not cap.gui.IsIconic(binding.hwnd))

    def foreground(self):
        return self._load().gui.GetForegroundWindow()

    def set_foreground(self, hwnd):
        return bool(self._load().user.SetForegroundWindow(hwnd))

    def post_message(self, hwnd, message, wparam, lparam):
        return bool(self._load().user.PostMessageW(hwnd, message, wparam, lparam))


class WindowOperations:
    """Queueing a Win32 message is not proof the app handled it.

    Posted clicks cannot be retracted; real focus/queue quiescence must be
    independently accepted before allowing paste or acknowledging takeover.
    The current native candidate has no such acceptance or paste/send backend.
    """
    def __init__(self, binding, *, api=None, grant=None, clock=time.monotonic, sleep=time.sleep):
        self.binding, self.api, self.grant = binding, api, grant
        self.clock, self.sleep = clock, sleep

    def _before(self, operation, may_act):
        grant = self.grant
        if (self.api is None or grant is None or grant.binding != self.binding
                or type(grant.expires_at) not in (int, float) or not math.isfinite(grant.expires_at)
                or not 0 < grant.expires_at - self.clock() <= 90 or grant.revoked.is_set()
                or not isinstance(grant.allowed, frozenset)
                or operation not in grant.allowed):
            raise UnsupportedCapability("window_action_not_authorized")
        if self.api.verify(self.binding) is not True:
            raise UnsupportedCapability("window_binding_not_verified")
        may_act()  # Session, durable attempt and actual evidence checked here.
        if grant.revoked.is_set() or self.clock() >= grant.expires_at:
            raise UnsupportedCapability("window_action_revoked")

    def activate_wechat(self, may_act):
        self._before("activate", may_act)
        if self.api.foreground() != self.binding.hwnd:
            if not self.api.set_foreground(self.binding.hwnd):
                raise UnsupportedCapability("foreground_request_failed")
        self._before("activate", may_act)
        if self.api.foreground() != self.binding.hwnd:
            raise UnsupportedCapability("foreground_not_verified")

    def post_click_client_point(self, x, y, may_act):
        # Upstream LPARAM encoding + three local mouse messages. No global
        # cursor move. Partial posting is UNKNOWN to the caller, never retried.
        if (type(x) is not int or type(y) is not int
                or not 0 <= x < min(self.binding.width, 32768)
                or not 0 <= y < min(self.binding.height, 32768)):
            raise UnsupportedCapability("click_point_outside_bound_client")
        lparam = (y << 16) | (x & 0xffff)
        for message, wparam, delay in ((0x0200, 0, .01), (0x0201, 1, .02), (0x0202, 0, .03)):
            self._before("click", may_act)
            if self.api.foreground() != self.binding.hwnd:
                raise UnsupportedCapability("foreground_not_verified")
            if not self.api.post_message(self.binding.hwnd, message, wparam, lparam):
                raise UnsupportedCapability("mouse_message_not_confirmed")
            self.sleep(delay)


@dataclass(frozen=True)
class EditorLocation:
    binding: object
    box: object
    observed_at: float
    proven: bool = False


class LuofengWindowActions:
    """Compose primitives, but never invent a locator/clipboard/send backend.

    Locator, preserving paste port and submit port are injected capabilities;
    none is implemented by this migration. A paste port must not clear drafts
    or restore a stale text-only clipboard. An OS call return is not delivery.
    """
    def __init__(self, window_ops, *, locator=None, focus_verifier=None, paste_port=None, submit_port=None,
                 quiescence_reader=None):
        self.ops, self.locator = window_ops, locator
        self.focus_verifier = focus_verifier
        self.paste_port, self.submit_port = paste_port, submit_port
        self.quiescence_reader = quiescence_reader
        self._window_started = self._paste_started = self._submit_started = False

    def quiescent(self):
        try:
            if self._window_started and (self.quiescence_reader is None
                    or self.quiescence_reader(self.ops.binding) is not True):
                return False
            for started, port in ((self._paste_started, self.paste_port),
                                  (self._submit_started, self.submit_port)):
                if started and port.quiescent() is not True:
                    return False
            return True
        except Exception:
            return False

    def write_text(self, text, may_act):
        # Check missing capabilities before even focusing the window.
        if self.locator is None or self.focus_verifier is None or self.paste_port is None:
            raise UnsupportedCapability("editor_locator_focus_or_preserving_paste_unsupported")
        may_act()
        location = self.locator()
        if not isinstance(location, EditorLocation) or not isinstance(location.box, Box):
            raise UnsupportedCapability("editor_location_not_proven")
        box, binding = location.box, self.ops.binding
        if (location.proven is not True or location.binding != binding
                or not 0 <= self.ops.clock() - location.observed_at <= 2
                or not box.inside(binding.width, binding.height)):
            raise UnsupportedCapability("editor_location_not_proven")
        self._window_started = True
        self.ops.activate_wechat(may_act)
        self.ops.post_click_client_point((box.left + box.right) // 2, (box.top + box.bottom) // 2, may_act)
        may_act()
        if self.focus_verifier(location) is not True:
            raise UnsupportedCapability("editor_focus_not_verified")
        may_act()
        self._paste_started = True
        self.paste_port(text, may_act)

    def submit(self, may_act):
        if self.submit_port is None:
            raise UnsupportedCapability("verified_submit_port_unsupported")
        self.ops._before("submit", may_act)
        if self.ops.api.foreground() != self.ops.binding.hwnd:
            raise UnsupportedCapability("foreground_not_verified")
        may_act()
        self._submit_started = True
        self.submit_port(may_act)

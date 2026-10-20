"""Default-off preserving clipboard candidate; no native calls on import.

Original project implementation, NOT the upstream text-only save/restore code.
Uses Win32 OpenClipboard for compare-and-restore under one lock. Unknown,
owner-managed and non-HGLOBAL formats are rejected BEFORE clearing anything.
No native paste/key backend is supplied. An injected consumer must establish
actual input readback AND that no queued paste can consume later clipboard data.
Sequence checks do not make clipboard consumption and key dispatch atomic.
Native calls/delayed rendering require a bounded worker; none is launched here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import threading
import time

from luofeng_window_ops import UnsupportedCapability


MAX_BYTES = 8 * 1024 * 1024
# Explicit HGLOBAL formats only. A bitmap, metafile, private/OLE/registered
# format anywhere in the clipboard rejects the ENTIRE operation, not just it.
HGLOBAL_FORMATS = frozenset((1, 7, 8, 13, 15, 16, 17))


@dataclass(frozen=True)
class ClipboardItem:
    format: int
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class ClipboardGrant:
    expires_at: float
    authorized: bool = False
    revoked: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass(frozen=True)
class ConsumptionEvidence:
    input_text: str | None = field(default=None, repr=False)
    quiescent: bool = False


def _validate_items(items):
    if (not isinstance(items, tuple) or len(items) > 32
            or len({item.format for item in items if isinstance(item, ClipboardItem)}) != len(items)):
        raise UnsupportedCapability("clipboard_snapshot_invalid")
    total = 0
    for item in items:
        if (not isinstance(item, ClipboardItem) or type(item.format) is not int
                or item.format not in HGLOBAL_FORMATS or type(item.data) is not bytes
                or not item.data):
            raise UnsupportedCapability("clipboard_format_not_preservable")
        total += len(item.data)
        if total > MAX_BYTES:
            raise UnsupportedCapability("clipboard_snapshot_too_large")


class PreservingClipboardPort:
    """Injectable paste port for LuofengWindowActions; disabled by default.

    backend.session() supplies owner/locked(); locked view supplies sequence(),
    current_owner(), snapshot(), replace(). All compare/restore steps occur in
    one locked block. No retries, text logs, files or previous-value replay.
    Consumer(text, check_ticket, may_act) is NOT implemented for real WeChat.
    Missing consumer rejects before even opening/reading the system clipboard.
    """
    def __init__(self, *, backend=None, consumer=None, grant=None, enabled=False, clock=time.monotonic):
        self.backend, self.consumer, self.grant = backend, consumer, grant
        self.enabled, self.clock = enabled, clock
        self._busy = threading.Lock()
        self._consumption_unknown = False

    def quiescent(self):
        # A returned/failed consumer may still have queued a paste. This latch
        # never clears merely on __call__ exit, timeout or another invocation.
        return not self._busy.locked() and not self._consumption_unknown

    def _authorize(self, may_act):
        grant = self.grant
        if (self.enabled is not True or grant is None or grant.authorized is not True
                or type(grant.expires_at) not in (int, float) or not math.isfinite(grant.expires_at)
                or not 0 < grant.expires_at - self.clock() <= 90 or grant.revoked.is_set()):
            raise UnsupportedCapability("clipboard_not_authorized")
        may_act()
        if grant.revoked.is_set() or self.clock() >= grant.expires_at:
            raise UnsupportedCapability("clipboard_permission_revoked")

    def __call__(self, text, may_act):
        self._authorize(may_act)
        if self.backend is None or self.consumer is None:
            raise UnsupportedCapability("clipboard_backend_or_consumer_unsupported")
        if self._consumption_unknown:
            raise UnsupportedCapability("prior_paste_queue_unknown_not_retriable")
        if (not isinstance(text, str) or not text.strip() or len(text) > 120
                or any(c in text for c in "\x00\r\n")):
            raise UnsupportedCapability("clipboard_payload_invalid")
        try:
            payload = (ClipboardItem(13, text.encode("utf-16-le", errors="strict") + b"\0\0"),)
        except UnicodeError:
            raise UnsupportedCapability("clipboard_payload_invalid") from None
        if not self._busy.acquire(blocking=False):
            raise UnsupportedCapability("clipboard_transaction_inflight")
        try:
            with self.backend.session() as api:
                original = None
                ticket = None
                changed = False
                consumption_started = False
                quiescent = False
                try:
                    with api.locked() as clip:
                        self._authorize(may_act)
                        sequence = clip.sequence()
                        if type(sequence) is not int or sequence <= 0:
                            raise UnsupportedCapability("clipboard_sequence_unavailable")
                        original = clip.snapshot()
                        _validate_items(original)
                        if clip.sequence() != sequence:
                            raise UnsupportedCapability("clipboard_changed_during_snapshot")
                        self._authorize(may_act)
                        changed = True  # Even a failing replace may have emptied it.
                        clip.replace(payload)
                        ticket = clip.sequence()
                        if (type(ticket) is not int or ticket <= 0 or ticket == sequence
                                or clip.current_owner() != api.owner):
                            raise UnsupportedCapability("clipboard_stage_unconfirmed")

                    def check_ticket():
                        self._authorize(may_act)
                        with api.locked() as clip:
                            if clip.sequence() != ticket or clip.current_owner() != api.owner:
                                raise UnsupportedCapability("clipboard_changed_before_consumption")

                    check_ticket()
                    consumption_started = True
                    self._consumption_unknown = True
                    evidence = self.consumer(text, check_ticket, may_act)
                    quiescent = isinstance(evidence, ConsumptionEvidence) and evidence.quiescent is True
                    if quiescent:
                        self._consumption_unknown = False
                    if (not isinstance(evidence, ConsumptionEvidence) or evidence.quiescent is not True
                            or evidence.input_text != text):
                        raise UnsupportedCapability("paste_consumption_or_readback_unproven")
                finally:
                    # Cleanup permission covers only restoring THIS transaction's
                    # original bytes while our exact sequence/owner still holds.
                    # It grants no paste/key operations after pause/expiry.
                    if changed:
                        if type(ticket) is not int or ticket <= 0:
                            raise UnsupportedCapability("clipboard_stage_unknown_no_restore")
                        if consumption_started and not quiescent:
                            # A queued Ctrl+V could still consume the old user's
                            # data if we restored now. No restore or automatic
                            # retry is safe; leave this attempt UNKNOWN.
                            raise UnsupportedCapability("paste_queue_unknown_no_restore")
                        with api.locked() as clip:
                            if clip.sequence() != ticket or clip.current_owner() != api.owner:
                                # Preserve the user's new content, even if identical
                                # text. Never compare text to infer ownership.
                                raise UnsupportedCapability("clipboard_changed_preserved_no_restore")
                            clip.replace(original)
        finally:
            self._busy.release()


class NativeClipboardBackend:
    """Lazy Win32 HGLOBAL implementation. No open/read/write on construction.

    Unsupported formats, including registered HTML/PNG/OLE, refuse the complete
    paste instead of saving text alone. Images/files are never silently dropped.
    GetClipboardData may render delayed data and block: use a bounded worker.
    No real-system acceptance has been performed. This is not a paste backend.
    """
    @contextmanager
    def session(self):
        api = _Win32Clipboard()
        try:
            yield api
        finally:
            api.close()


class _Win32Clipboard:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c = c
        self.user = c.WinDLL("user32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        signatures = (
            (self.user, "CreateWindowExW", [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c.c_int, c.c_int,
                                           c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p], w.HWND),
            (self.user, "DestroyWindow", [w.HWND], w.BOOL),
            (self.user, "OpenClipboard", [w.HWND], w.BOOL),
            (self.user, "CloseClipboard", [], w.BOOL),
            (self.user, "EmptyClipboard", [], w.BOOL),
            (self.user, "GetClipboardSequenceNumber", [], w.DWORD),
            (self.user, "GetClipboardOwner", [], w.HWND),
            (self.user, "EnumClipboardFormats", [w.UINT], w.UINT),
            (self.user, "GetClipboardData", [w.UINT], w.HANDLE),
            (self.user, "SetClipboardData", [w.UINT, w.HANDLE], w.HANDLE),
            (self.kernel, "GlobalAlloc", [w.UINT, c.c_size_t], w.HANDLE),
            (self.kernel, "GlobalSize", [w.HANDLE], c.c_size_t),
            (self.kernel, "GlobalLock", [w.HANDLE], c.c_void_p),
            (self.kernel, "GlobalUnlock", [w.HANDLE], w.BOOL),
            (self.kernel, "GlobalFree", [w.HANDLE], w.HANDLE),
        )
        for lib, name, args, result in signatures:
            function = getattr(lib, name)
            function.argtypes, function.restype = args, result
        # Our own message-only window, never the WeChat HWND/clipboard owner.
        self.owner = self.user.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, -3, None, None, None)
        self._locked = False
        if not self.owner:
            raise UnsupportedCapability("clipboard_owner_window_unavailable")

    def close(self):
        if self.owner:
            owner, self.owner = self.owner, None
            if not self.user.DestroyWindow(owner):
                raise UnsupportedCapability("clipboard_owner_cleanup_failed")

    @contextmanager
    def locked(self):
        if self._locked or not self.user.OpenClipboard(self.owner):
            raise UnsupportedCapability("clipboard_busy_or_unavailable")
        self._locked = True
        try:
            yield self
        finally:
            self._locked = False
            if not self.user.CloseClipboard():
                raise UnsupportedCapability("clipboard_unlock_unconfirmed")

    def _require_lock(self):
        if not self._locked:
            raise UnsupportedCapability("clipboard_lock_required")

    def sequence(self):
        self._require_lock()
        return int(self.user.GetClipboardSequenceNumber())

    def current_owner(self):
        self._require_lock()
        return self.user.GetClipboardOwner()

    def _formats(self):
        result, current = [], 0
        while True:
            self.c.set_last_error(0)
            current = self.user.EnumClipboardFormats(current)
            if not current:
                if self.c.get_last_error():
                    raise UnsupportedCapability("clipboard_enumeration_failed")
                return tuple(result)
            if current in result or len(result) >= 32 or current not in HGLOBAL_FORMATS:
                raise UnsupportedCapability("clipboard_format_not_preservable")
            result.append(current)

    def snapshot(self):
        self._require_lock()
        formats, items, total = self._formats(), [], 0
        for format_id in formats:
            handle = self.user.GetClipboardData(format_id)
            size = self.kernel.GlobalSize(handle) if handle else 0
            if not size or total + size > MAX_BYTES:
                raise UnsupportedCapability("clipboard_data_unavailable_or_too_large")
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                raise UnsupportedCapability("clipboard_data_lock_failed")
            try:
                items.append(ClipboardItem(format_id, self.c.string_at(pointer, size)))
            finally:
                self.c.set_last_error(0)
                if not self.kernel.GlobalUnlock(handle) and self.c.get_last_error():
                    raise UnsupportedCapability("clipboard_data_unlock_failed")
            total += size
        if self._formats() != formats:
            raise UnsupportedCapability("clipboard_formats_changed_during_snapshot")
        return tuple(items)

    def replace(self, items):
        self._require_lock()
        _validate_items(items)
        allocated = []
        try:
            # Allocate all memory BEFORE EmptyClipboard. Never free handles after
            # SetClipboardData transferred them to the OS.
            for item in items:
                handle = self.kernel.GlobalAlloc(0x0002, len(item.data))
                if not handle:
                    raise UnsupportedCapability("clipboard_allocation_failed")
                allocated.append([item.format, handle])
                pointer = self.kernel.GlobalLock(handle)
                if not pointer:
                    raise UnsupportedCapability("clipboard_write_lock_failed")
                try:
                    self.c.memmove(pointer, item.data, len(item.data))
                finally:
                    self.c.set_last_error(0)
                    if not self.kernel.GlobalUnlock(handle) and self.c.get_last_error():
                        raise UnsupportedCapability("clipboard_write_unlock_failed")
            if not self.user.EmptyClipboard():
                raise UnsupportedCapability("clipboard_empty_failed")
            for entry in allocated:
                if not self.user.SetClipboardData(entry[0], entry[1]):
                    raise UnsupportedCapability("clipboard_replace_unknown")
                entry[1] = None
        finally:
            for _, handle in allocated:
                if handle and self.kernel.GlobalFree(handle):
                    raise UnsupportedCapability("clipboard_memory_cleanup_failed")


def main():
    import json
    print(json.dumps(dict(kind="preserving_clipboard_candidate", status="not_started",
                          native_backend_loaded=False, clipboard_operations=0, inputs=0, sends=0)))


if __name__ == "__main__":
    main()

"""Clipboard-based input readback. 尚未接入任何发送入口。

Reads the actual text in the input box instead of OCR-ing a picture:
write a sentinel, select-all + copy, read the clipboard, restore the original
clipboard, compare exactly. The caret and the font are irrelevant here.

Two rules this module enforces:
  * an empty box does NOT clear the clipboard, so "still the sentinel" means
    EMPTY - it must never be reported as MATCH;
  * the original clipboard is restored in a finally block and verified; a failed
    restore turns the result into UNCERTAIN even when the text matched.

MATCH only means the visible text matched. It is not sending permission.
"""
from __future__ import annotations

from datetime import datetime
import secrets

from input_readback import Readback, Verdict

SENTINEL_PREFIX = "wxreadback"
MAX_CONSECUTIVE_SENTINELS = 3


class PortError(Exception):
    """Raised by an injected port (keyboard or clipboard) when it cannot act."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def make_sentinel(now=None):
    """Random enough that no real chat text can collide with it."""
    moment = now or datetime.now()
    return f"{SENTINEL_PREFIX}-{moment.strftime('%Y%m%d%H%M%S%f')}-{secrets.token_hex(4)}"


def _uncertain(reason, **meta):
    return Readback(Verdict.UNCERTAIN, reason, **meta)


def _looks_like_wrong_focus(text, expected_text):
    """A long or multi-line copy means the selection was not the input box."""
    if "\n" in text or "\r" in text:
        return True
    return len(text) > max(4, 3 * len(expected_text))


def _compare(text, expected_text):
    if text == expected_text:
        return Readback(Verdict.MATCH, "exact_clipboard_text_match", ocr_text=text)
    return Readback(Verdict.MISMATCH, "exact_text_mismatch", ocr_text=text)


DEFAULT_CLIPBOARD_POLICY = "text_only"


def read_input_by_clipboard(expected_text, *, keyboard, clipboard, timeout=2.0,
                            policy=DEFAULT_CLIPBOARD_POLICY):
    """Return Readback(text, verdict, reason). See module docstring.

    keyboard:  select_all(), copy(timeout=...)
    clipboard: snapshot(), restore(items), write_text(str), read_text() -> str|None,
               sequence() -> int

    policy:
      "strict"    - the whole clipboard must be preservable, otherwise UNCERTAIN.
      "text_only" - when the clipboard holds formats we cannot preserve (rich
                    text, private ids), keep only the text, restore only the
                    text, and mark the result with a `_text_only_restore` reason
                    suffix. Never silent: the caller can see it degraded.
    """
    if not isinstance(expected_text, str) or not expected_text:
        return _uncertain("expected_text_empty")
    if policy not in ("strict", "text_only"):
        return _uncertain("invalid_clipboard_policy")

    sentinel = make_sentinel()
    degraded = False
    original = None
    saved_text = None
    try:
        original = clipboard.snapshot()
    except PortError as error:
        if policy == "strict":
            return _uncertain(f"clipboard_snapshot_failed:{error.reason}")
        try:
            saved_text = clipboard.read_text()
        except Exception:
            saved_text = None
        degraded = True
    except Exception:
        if policy == "strict":
            return _uncertain("clipboard_snapshot_failed")
        degraded = True

    text = None
    copied = False
    problem = None

    try:
        try:
            clipboard.write_text(sentinel)
        except PortError as error:
            problem = f"clipboard_write_failed:{error.reason}"
        except Exception:
            problem = "clipboard_write_failed"

        if problem is None:
            try:
                keyboard.select_all()
                keyboard.copy(timeout=timeout)
                copied = True
            except TimeoutError:
                problem = "copy_timeout"
            except PortError as error:
                problem = f"keyboard_failed:{error.reason}"
            except Exception:
                problem = "keyboard_failed"

        if problem is None:
            try:
                text = clipboard.read_text()
            except PortError as error:
                problem = f"clipboard_read_failed:{error.reason}"
            except Exception:
                problem = "clipboard_read_failed"
    finally:
        restored, restore_reason = True, ""
        try:
            if original is not None:
                clipboard.restore(original)
            elif degraded:
                # Only the text survives; formats are lost by design here.
                if saved_text is None:
                    clipboard.write_text("")
                else:
                    clipboard.write_text(saved_text)
        except PortError as error:
            restored, restore_reason = False, error.reason
        except Exception:
            restored = False
            restore_reason = "restore_exception"

    suffix = "_text_only_restore" if degraded else ""

    if problem is not None:
        return _uncertain(problem + suffix)
    if not restored:
        # The text may well be correct; the clipboard still must not be lost.
        return _uncertain((f"clipboard_restore_failed:{restore_reason}"
                           if restore_reason else "clipboard_restore_failed") + suffix)

    if text is None:
        # No text format after the copy. Either nothing was selected, or the
        # copy produced a non-text format.
        return _uncertain("clipboard_non_text_after_copy" + suffix)

    if text == sentinel:
        return Readback(Verdict.UNCERTAIN, "input_empty" + suffix, ocr_text="")

    if not copied or text == "":
        return Readback(Verdict.UNCERTAIN, "input_empty" + suffix, ocr_text=text)

    if _looks_like_wrong_focus(text, expected_text):
        return _uncertain("possible_wrong_focus" + suffix, ocr_text=text)

    result = _compare(text, expected_text)
    if not degraded:
        return result
    return Readback(result.verdict, result.reason + suffix, ocr_text=result.ocr_text)


class NativeKeyboard:
    """Ctrl+A then Ctrl+C through keybd_event. No other key is ever sent."""

    def __init__(self, sleep=None):
        import time as _time
        self._sleep = sleep or _time.sleep

    def _tap(self, vk):
        import win32api
        import win32con
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(vk, 0, 0, 0)
        self._sleep(0.05)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

    def select_all(self):
        self._tap(0x41)  # A

    def copy(self, timeout=2.0):
        self._tap(0x43)  # C
        self._sleep(0.25)

    def paste(self):
        self._tap(0x56)  # V
        self._sleep(0.35)

    def send_enter(self):
        """A single Enter. No modifiers, no repeats."""
        import win32api
        import win32con
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        self._sleep(0.05)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)


class NativeClipboardPort:
    """Thin per-operation wrapper over luofeng_clipboard._Win32Clipboard.

    The clipboard is opened only for the duration of one operation: holding it
    across the Ctrl+C would block the application from writing its own copy.
    """

    def __init__(self, backend=None):
        from luofeng_clipboard import _Win32Clipboard
        self._backend = backend or _Win32Clipboard()

    def snapshot(self):
        from luofeng_clipboard import UnsupportedCapability
        try:
            with self._backend.locked():
                return self._backend.snapshot()
        except UnsupportedCapability as error:
            raise PortError(str(error)) from None

    def restore(self, items):
        from luofeng_clipboard import UnsupportedCapability
        try:
            with self._backend.locked():
                self._backend.replace(items)
        except UnsupportedCapability as error:
            raise PortError(str(error)) from None

    def _write(self, text):
        from luofeng_clipboard import ClipboardItem
        data = (text + "\x00").encode("utf-16-le")
        self.restore((ClipboardItem(13, data),))  # CF_UNICODETEXT

    def write_text(self, text):
        self._write(text)

    def read_text(self):
        """Read only the text formats.

        The application's own copy may add formats we cannot preserve (rich
        text, HTML, private ids). Snapshotting the whole clipboard after the
        copy therefore fails, and it must not: we only need the text, and the
        clipboard is restored from the pre-copy snapshot regardless.
        """
        from luofeng_clipboard import UnsupportedCapability
        try:
            with self._backend.locked():
                for fmt in (13, 1):
                    handle = self._backend.user.GetClipboardData(fmt)
                    if not handle:
                        continue
                    size = self._backend.kernel.GlobalSize(handle)
                    if not size:
                        continue
                    pointer = self._backend.kernel.GlobalLock(handle)
                    if not pointer:
                        raise PortError("clipboard_data_lock_failed")
                    try:
                        raw = self._backend.c.string_at(pointer, size)
                    finally:
                        self._backend.kernel.GlobalUnlock(handle)
                    if fmt == 13:
                        if len(raw) % 2:
                            raw = raw[:-1]
                        return raw.decode("utf-16-le", "replace").rstrip("\x00")
                    return raw.rstrip(b"\x00").decode("mbcs", "replace")
                return None
        except UnsupportedCapability as error:
            raise PortError(str(error)) from None

    def sequence(self):
        with self._backend.locked():
            return self._backend.sequence()

    def close(self):
        self._backend.close()

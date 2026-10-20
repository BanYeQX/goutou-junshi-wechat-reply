"""Read the chat the user has open in WeChat, once, for the 军师 tab. READ-ONLY.

One capture of the calibrated (maximized) WeChat window, then OCR: the title
must name the contact selected in the hosting window, and the visible text
blocks become 我 / 对方 lines. No click, no scroll, no keys, no clipboard.
What is read goes into the 军师 input box for the user to check - OCR can
misread a character or a side - and is analysed only when the user asks.

The user chose (2026-10-20 17:2x) to allow any contact they have selected,
not only the calibrated 小雨: the title check is against that contact's name.
"""
from __future__ import annotations

import threading
import time

from advisor import MAX_TURN_CHARS, Turn

_ocr_engine = None
_ocr_lock = threading.Lock()


class ReadError(ValueError):
    pass


def read_from_frame(frame, *, expected_title, calibration, ocr, window_rect, limit=20):
    """frame (wechat_capture.PixelImage) -> list[Turn]. Pure: no window access."""
    from hosting_run import TITLE_FRAME, _ocr_text, collect_context, passes_geometry
    name = (expected_title or "").strip()
    if not name:
        raise ReadError("先在左侧选中联系人。")
    if not passes_geometry(window_rect, calibration["window_rect"]):
        raise ReadError("微信窗口不是标定时的最大化布局：请把微信最大化后再读取。")
    seen = _ocr_text(ocr, frame.crop(TITLE_FRAME)).replace(" ", "")
    if name.replace(" ", "") not in seen:
        raise ReadError(f"微信里打开的不是「{name}」的聊天（标题识别为：{seen or '空'}）。"
                        "请先在微信里点开和 TA 的聊天。")
    items = collect_context(frame, tuple(calibration["messages_frame"]), ocr, limit)
    # Centred blocks (direction None) are notices or unclear: left out, never guessed.
    turns = [Turn("other" if incoming else "me", text.strip()[:MAX_TURN_CHARS])
             for text, incoming in items if incoming is not None and text.strip()]
    if not turns:
        raise ReadError("屏幕上没有识别到文字消息（图片、表情、语音不读）。")
    return turns


def _engine():
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            from title_ocr import build_offline_engine
            _ocr_engine = build_offline_engine()
        return _ocr_engine


def read_visible_chat(expected_title, *, limit=20) -> list[Turn]:
    """Live: capture the calibrated WeChat window once and read it."""
    import hosting_run
    import win32gui
    from capture_gdi import NativeGDI
    from wechat_capture import CaptureAdapter, CapturePermission

    calibration = hosting_run.load_calibration()
    try:
        window_rect = win32gui.GetWindowRect(calibration["hwnd"])
        api = NativeGDI()
        scope = api.describe(calibration["hwnd"], expected_pid=calibration["pid"])
    except Exception:
        raise ReadError("找不到标定时的微信窗口（微信可能重启过）：请重新标定。") from None
    adapter = CaptureAdapter(scope, CapturePermission(scope, time.monotonic() + 30, True,
                                                      threading.Event()), backend=api)
    try:
        frame, _, _ = adapter.capture()
    except Exception as error:
        raise ReadError("截取微信窗口失败：" + type(error).__name__) from None
    finally:
        adapter.close()
    return read_from_frame(frame, expected_title=expected_title, calibration=calibration,
                           ocr=_engine(), window_rect=window_rect, limit=limit)

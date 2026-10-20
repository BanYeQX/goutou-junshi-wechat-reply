"""Hosting runner: wires the real adapters to the state machine.

Three modes, and the default is the safest one:

    --dry-run     observe and print decisions. Never types, never pastes,
                  never presses Enter. Use this to watch the loop work.
    --stage-only  additionally pastes the reply and reads it back, but never
                  presses Enter.
    --live        pastes, reads back, records the commit in the ledger, presses
                  Enter once and verifies. Requires --confirm with the exact
                  contact name, and a ledger with no unfinished commit.

Nothing here is allowed to guess: a missing calibration, a moved window, an
unfinished ledger entry or a wrong title all stop the loop before any input.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from hosting_ledger import Ledger
from hosting_loop import Decision, Loop
from hosting_session import BottomAnchor, Geometry, Reading, Session, passes_geometry

REPO = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
CALIBRATION = __import__("os").path.join(REPO, "data", "visual_calibration.json")
# The conversation title lives at the top of the message pane. Measured on the
# maximized layout; it is NOT a fraction of the window width - the chat list is
# only ~16% wide when maximized, so a ratio crop lands on empty space.
TITLE_FRAME = (434, 55, 760, 135)
LOOP_INTERVAL_S = 0.25
# How far left of TITLE_FRAME the list/chat divider may have moved. Live
# 2026-10-20 20:2x: the divider sat at x=401, the fixed crop from 434 cut the
# left half of "千" off and OCR read "F星" - hosting refused to start.
TITLE_DIVIDER_SEARCH = 200


def title_box(frame):
    """TITLE_FRAME, but starting where the chat pane really begins.

    WeChat's contact list can be dragged narrower or wider; the title follows
    the chat pane. Scan the middle row of the title band for the edge from the
    list background to the chat title bar and start just right of it - never
    left, so the list (and its search box) can not supply the contact's name.
    Not found (other theme, synthetic frame): the fixed TITLE_FRAME.
    """
    left, top, right, bottom = TITLE_FRAME
    width, height = frame.size
    y = (top + bottom) // 2
    if not (0 <= y < height and right <= width):
        return TITLE_FRAME
    data = frame.tobytes()

    def grey(x):
        o = (y * width + x) * 4
        return (data[o] + data[o + 1] + data[o + 2]) / 3

    # The title bar's own background, sampled where no name is drawn.
    samples = sorted(grey(int(width * f)) for f in (0.5, 0.6, 0.7))
    bar = samples[1]
    if samples[2] - samples[0] > 4:
        return TITLE_FRAME
    edge = None
    for x in range(max(1, left - TITLE_DIVIDER_SEARCH), min(left + TITLE_DIVIDER_SEARCH, width - 13)):
        # First column of a flat stretch of title-bar colour right after a
        # clearly different colour: the divider. List pixels are not the bar
        # colour, so the search box or its icons can not end up in the crop.
        if (abs(grey(x - 1) - bar) >= 12
                and all(abs(grey(x + i) - bar) <= 4 for i in range(12))):
            edge = x
            break
    if edge is None:
        return TITLE_FRAME
    return (edge, top, max(right, edge + 200), bottom)
LEDGER = __import__("os").path.join(REPO, "data", "send-ledger.jsonl")
# How many blocks above a changed newest block may be read to find the newest
# block seen before it (merge window: 3 messages, plus our own reply).
CONTINUITY_LOOKBACK = 5


def load_calibration(path=CALIBRATION):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    block = data["maximized_calibration"]
    snapshot = block["snapshot"]
    regions = block["regions"]
    return {
        "title": block["confirmed_target"],
        "window_rect": tuple(snapshot["window_rect"]),
        "messages_frame": tuple(regions["messages"]["frame_rect"]),
        "input_frame": tuple(regions["input"]["frame_rect"]),
        "input_screen": tuple(regions["input"]["screen_rect"]),
        # Blank rows under the newest bubble when the chat is at the bottom.
        # Not measured yet: without it the first frame of each run is used.
        "messages_bottom_gap": regions["messages"].get("bottom_gap"),
        "send_screen_center": tuple(regions["send"]["screen_center"]),
        "hwnd": snapshot["binding"]["hwnd"],
        "pid": snapshot["binding"]["pid"],
    }


def build_adapters(calibration):
    """Real adapters. Imported lazily so --dry-run works without them."""
    from capture_gdi import NativeGDI
    from title_ocr import build_offline_engine
    from wechat_capture import CaptureAdapter, CapturePermission

    api = NativeGDI()
    scope = api.describe(calibration["hwnd"], expected_pid=calibration["pid"])
    permission = CapturePermission(scope, time.monotonic() + 3600, True, threading.Event())
    adapter = CaptureAdapter(scope, permission, backend=api)
    ocr = build_offline_engine()
    frame_box = tuple(calibration["messages_frame"])
    anchor = BottomAnchor(calibration.get("messages_bottom_gap"))

    def capture():
        frame, _, _ = adapter.capture()
        return frame

    def read(frame, changed):
        import win32gui
        return read_frame(frame, changed, calibration=calibration, ocr=ocr,
                          window_rect=win32gui.GetWindowRect(calibration["hwnd"]),
                          anchor=anchor)

    def collect(limit=10):
        return collect_context(capture(), frame_box, ocr, limit)

    return capture, read, adapter, collect


def _ocr_text(ocr, image):
    """OCR one BGRA crop (a wechat_capture.PixelImage) into a single string."""
    import numpy as np
    array = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape(image.height, image.width, 4)
    rgb = np.ascontiguousarray(array[:, :, :3][:, :, ::-1])
    return "".join(getattr(ocr(rgb), "txts", None) or ()).strip()


def read_frame(frame, changed, *, calibration, ocr, window_rect, anchor):
    """The real reader: one captured frame -> Reading. No window access here;
    the caller passes the window rect, so this runs offline on PixelImage frames.

    The scroll check runs on every frame, changed or not: an unchanged frame
    while scrolled up is still not the bottom, and a pending message must not be
    answered from it.
    """
    from bubble_box import bottom_gap, is_incoming, text_blocks

    geometry_ok = passes_geometry(window_rect, calibration["window_rect"])
    title_ok = True
    if not geometry_ok:
        return Reading(geometry_ok=False, title_ok=title_ok)
    strip = frame.crop(tuple(calibration["messages_frame"]))
    scrolled = anchor.scrolled(bottom_gap(strip.width, strip.height, strip.tobytes()))
    if not changed:
        return Reading(geometry_ok=True, title_ok=title_ok, scrolling=scrolled)

    # Verify the conversation whenever we are about to interpret a change:
    # acting on the wrong chat is the one mistake that cannot be undone.
    title_text = _ocr_text(ocr, frame.crop(title_box(frame)))
    if calibration["title"] not in title_text.replace(" ", ""):
        return Reading(geometry_ok=True, title_ok=False, title_seen=title_text)
    if scrolled:
        # The bottom-most block is history. Do not read it: not even an
        # unreadable one may count, that would pause as a "new" emoji.
        return Reading(geometry_ok=True, title_ok=title_ok, scrolling=True)

    blocks = text_blocks(strip.width, strip.height, strip.tobytes())
    if not blocks:
        return Reading(geometry_ok=True, title_ok=title_ok)

    newest = blocks[-1]
    text = _ocr_text(ocr, strip.crop(newest))
    if not text:
        # An emoji, an image or a system notice: the newest thing is not
        # text we can answer. Never fall back to an older message.
        return Reading(geometry_ok=True, title_ok=title_ok, unreadable=True)
    direction = is_incoming(newest, strip.width)

    def above():
        # Nearest first and lazy: the loop stops at the first match, so a
        # normal event reads one extra block. Unreadable blocks are skipped.
        for block in reversed(blocks[-1 - CONTINUITY_LOOKBACK:-1]):
            seen = _ocr_text(ocr, strip.crop(block))
            if seen:
                yield seen

    return Reading(text=text, incoming=direction,
                   geometry_ok=True, title_ok=title_ok, above=above)


def wait_for_foreground(hwnd, stable=2.0, limit=90.0):
    """Wait until WeChat stays in the foreground. Returns True on success."""
    import win32gui
    deadline = time.monotonic() + limit
    since = None
    while time.monotonic() < deadline:
        if win32gui.GetForegroundWindow() == hwnd:
            since = since or time.monotonic()
            if time.monotonic() - since >= stable:
                return True
        else:
            since = None
        time.sleep(0.2)
    return False


def activate(hwnd):
    """Bring the target forward ourselves. ALT unlocks the foreground lock."""
    import win32api
    import win32con
    import win32gui
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        time.sleep(0.05)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    time.sleep(0.45)
    return win32gui.GetForegroundWindow() == hwnd


def stage_reply_real(reply, calibration, attempts=2, stop_event=None, notes=None):
    """Paste the reply into the focused box and read it back. Never Enter.

    `notes`, when a list, receives one fixed reason code per attempt. Under the
    GUI there is no console, so the prints below are lost; the caller shows
    these instead.
    """
    import win32gui
    from hosting_stage import StageOutcome, stage_reply
    from input_readback_clipboard import NativeClipboardPort, NativeKeyboard

    hwnd = calibration["hwnd"]
    guard = lambda: (not (stop_event and stop_event.is_set())
                     and win32gui.GetForegroundWindow() == hwnd)
    last = None
    port = NativeClipboardPort()
    try:
        for attempt in range(1, attempts + 1):
            if stop_event and stop_event.is_set():
                return None, "stop_requested"
            if not activate(hwnd):
                last = None
                print(f"    attempt {attempt}: could not bring WeChat forward", flush=True)
                if notes is not None:
                    notes.append(f"{attempt}:activate_failed")
                continue
            last = stage_reply(reply, keyboard=NativeKeyboard(), clipboard=port, guard=guard)
            print(f"    attempt {attempt}: stage={last.outcome.value} "
                  f"reason={last.reason}", flush=True)
            if notes is not None:
                notes.append(f"{attempt}:{last.outcome.value}/{last.reason}")
            if last.outcome is StageOutcome.STAGED:
                return last, ""
            if last.outcome is StageOutcome.DRAFT_PRESENT:
                return last, "draft_in_input"
    finally:
        try:
            port.close()
        except Exception:
            pass
    return last, "not_staged"


def _same_reply(seen, sent):
    """Compare a delivery echo with what we sent.

    Delivery verification reads the bubble back through OCR, so it can differ by
    punctuation or spacing from the string we pasted. Requiring byte equality
    turned ordinary deliveries into "could not confirm", which freezes the whole
    bot with an UNKNOWN - far worse than the thing it was trying to detect.

    The strict verbatim comparison still happens earlier, on the clipboard, before
    Enter is ever pressed. This one only decides "did our bubble appear".
    """
    from hosting_loop import _skeleton
    a, b = _skeleton(seen or ""), _skeleton(sent or "")
    if not a or not b:
        return False
    return a == b


def _ratio(old_bytes, new_bytes):
    if not old_bytes or len(old_bytes) != len(new_bytes):
        return 1.0
    from hosting_session import differing_bytes
    return differing_bytes(old_bytes, new_bytes) / len(old_bytes)


# A pixel is "ink" when some channel differs from the box background by more
# than this. Faint anti-aliasing below it is ignored on both sides of Enter.
# Was 48. Live 2026-10-20 21:5x: an EMPTY box showed WeChat's grey placeholder
# hint, max 97 from the background (brightest pixel grey 123), measured as a
# 197 px "leftover draft" -> a sent message was frozen as UNKNOWN. Typed text
# is near white (~190 from the dark background, ~235 from the light one), so
# 140 keeps every real character, the caret and a lone glyph, and drops the hint.
INK_TOLERANCE = 140
# Ink columns closer than this belong to one run: strokes of one glyph ("川",
# strokes ~6px apart) and neighbouring glyphs merge, so a character is never
# mistaken for a caret.
INK_GAP = 8
# A run this narrow or narrower is the blinking caret, not text. A lone narrow
# glyph ("1", "!") is dropped too and ends as UNKNOWN - the safe direction.
CARET_WIDTH = 3
# A draft narrower than this cannot be judged at all (region likely wrong).
MIN_DRAFT_WIDTH = 6


def draft_width(image):
    """Horizontal extent, in pixels, of text in an input-box crop.

    Counts ink columns after merging nearby runs and dropping caret-sized ones,
    so the answer is "how much text is there", independent of the caret's blink
    and of how long the reply is.
    """
    from PIL import Image, ImageChops
    if getattr(image, "mode", None) == "BGRA":
        # Live frames are wechat_capture.PixelImage: raw BGRA bytes, no PIL API.
        # Assuming PIL here crashed the first live commit (14:19) after Enter.
        rgb = Image.frombytes("RGB", image.size, image.tobytes(), "raw", "BGRX")
    else:
        rgb = image.convert("RGB")
    width, height = rgb.size
    colors = rgb.getcolors(maxcolors=width * height)
    background = max(colors)[1] if colors else (0, 0, 0)
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, background))
    r, g, b = diff.split()
    mask = ImageChops.lighter(ImageChops.lighter(r, g), b).point(
        lambda v: 255 if v > INK_TOLERANCE else 0)
    # Box-resizing to one row averages each column exactly: >0 means ink.
    columns = mask.resize((width, 1), Image.BOX).tobytes()
    runs, start, last = [], None, None
    for x, value in enumerate(columns):
        if not value:
            continue
        if start is None:
            start = x
        elif x - last > INK_GAP:
            runs.append((start, last))
            start = x
        last = x
    if start is not None:
        runs.append((start, last))
    return sum(end - begin + 1 for begin, end in runs if end - begin + 1 > CARET_WIDTH)


def verify_delivery(reply, *, baseline, capture, box, input_box, threshold,
                    max_draft_left=0.25, timeout=6.0, trace=None):
    """Decide whether pressing Enter actually sent the message.

    The old check OCR'd our own bubble back and required the text to match. That
    failed on perfectly ordinary sends (a wrapped line, a dropped glyph) and the
    bot froze with an UNKNOWN - the user saw "回复很慢" followed by "禁止重发".

    The reliable signal is structural, not textual: before Enter the input box
    held the reply; after a successful send it is empty again, and the
    conversation moved. So: the draft must be gone from the input box AND the
    message area must change. No OCR, no text guessing.

    "Gone" is measured as the draft's width relative to itself, not as a share
    of the whole box. The first version used "input pixels changed > 1%", which
    is about 4 characters on the calibrated box (live: ~0.0026 per character),
    so a one-character "行" would have frozen the bot. It also never checked the
    box was empty: an IME candidate popping up counted as "cleared".

    If Enter did nothing (dialog stole it, focus lost) the box keeps its text and
    this reports "not confirmed". If the baseline shows no measurable draft the
    region cannot be trusted, and this also reports "not confirmed".

    When `trace` is a dict it receives the measurements and timing, so an UNKNOWN
    says *which* signal never moved. Numbers only, never pixels or text.
    """
    old_message = baseline.crop(box).tobytes()
    old_input_image = baseline.crop(input_box)
    old_input = old_input_image.tobytes()
    before = draft_width(old_input_image)
    least_left = None
    peak_input = peak_message = 0.0
    samples = 0
    start = time.monotonic()
    delivered = False
    while time.monotonic() - start < timeout:
        time.sleep(0.15)
        current = capture()
        samples += 1
        input_image = current.crop(input_box)
        after = draft_width(input_image)
        least_left = after if least_left is None else min(least_left, after)
        # Diagnostic only, kept comparable with the first live samples.
        peak_input = max(peak_input, _ratio(old_input, input_image.tobytes()))
        message_ratio = _ratio(old_message, current.crop(box).tobytes())
        peak_message = max(peak_message, message_ratio)
        if before < MIN_DRAFT_WIDTH or after > max_draft_left * before:
            continue
        if message_ratio <= threshold:
            continue
        # The draft left the box and the conversation moved: the message was sent.
        delivered = True
        break
    if trace is not None:
        trace.update(delivered=delivered, samples=samples,
                     elapsed=round(time.monotonic() - start, 2),
                     draft_before=before, draft_left_min=least_left,
                     max_draft_left=max_draft_left, peak_input=round(peak_input, 4),
                     peak_message=round(peak_message, 4), threshold=threshold)
    return delivered


def record_verify_trace(ledger, commit_id, trace):
    """Append the verifier's numbers next to the ledger, never into it.

    The ledger decides whether we may send again; this file only explains a
    verdict after the fact. Losing it must never affect a send, so every error
    is swallowed. No reply text and no pixels are written.
    """
    import os
    path = getattr(ledger, "path", None)
    if not path or not trace:
        return
    try:
        row = {"commit_id": commit_id,
               "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **trace}
        with open(os.path.join(os.path.dirname(path), "verify-trace.jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def stub_generator(text):
    """Offline placeholder used by --offline-reply. Never used for real sends
    unless the caller asks for it explicitly."""
    return "收到"


def safe_model_failure(error):
    """Allowlisted local diagnostics only; never display raw provider bodies.

    Do not normalize or accept an invalid reply here. Failures still pause,
    and nothing is retried. A historical generic error cannot be recovered.
    """
    messages = {
        "模型没有返回规定格式，本条未发送。": ("model_format_invalid", "模型未返回规定的 JSON 格式"),
        "模型动作无效，本条未发送。": ("model_action_invalid", "模型返回了无效动作"),
        "模型文本格式无效，本条未发送。": ("model_text_invalid", "模型文本字段格式无效"),
        "模型回复为空或过长，本条未发送。": ("model_reply_limits", "模型回复为空、超120字或超过允许行数"),
        "模型设置或联系人已改变，本轮草稿请求作废。": ("model_config_changed", "模型设置或联系人发生变化，请求已作废"),
        "核对上下文与当前联系人不一致。": ("model_contact_mismatch", "上下文与当前联系人不一致"),
        "核对上下文格式无效。": ("model_context_invalid", "上下文格式无效"),
        "核对上下文只接受明确的普通文字及收发方向。": ("model_context_invalid", "上下文角色或正文格式无效"),
        "核对文字为空或过长。": ("model_context_limits", "来信或上下文长度超限"),
        "DeepSeek 未返回可用文本，本条没有发送。": ("model_no_text", "DeepSeek 未返回可用文本"),
        "当前 Windows 用户无法解密已保存的密钥，请在设置中重新填写。": ("model_credentials", "当前用户无法解密模型凭证"),
        "本轮草稿请求已暂停或失效。": ("model_request_stale", "模型任务已暂停或失效"),
        "ChatGPT 账号已改变，本轮草稿请求作废。": ("model_account_changed", "ChatGPT 账号发生变化，请求已作废"),
        "ChatGPT 计划额度或共享权限不可用，请在 ChatGPT 设置中检查用量。": ("model_quota_or_permission", "ChatGPT 计划额度或共享权限不可用"),
    }
    for text in (
        "OpenAI 请求没有完整完成，本条未发送。", "OpenAI 回复正文未完整完成，本条未发送。",
        "OpenAI 请求失败或未完整完成，本条未发送。", "OpenAI 流式连接在完成前中断，本条未发送。",
        "OpenAI 没有返回可用文本，本条未发送。", "OpenAI 流式正文索引无效，本条未发送。",
        "OpenAI 流式正文格式无效，本条未发送。", "OpenAI 流式正文格式无效或过长，本条未发送。",
    ):
        messages[text] = ("model_response_incomplete", "模型响应未完整完成或无效")
    messages["模型拒绝了这次请求，本条未发送。"] = ("model_refusal", "模型拒绝本次请求")
    for label in ("DeepSeek", "OpenAI API", "ChatGPT 计划"):
        messages[f"连接 {label} 失败或超时，请检查网络。本条没有发送。"] = ("model_network_timeout", label + " 连接失败或超时")
        messages[f"请先在设置中允许将当前联系人的上下文发送至 {label}。"] = ("model_consent_missing", label + " 消息传输许可未开启")
        messages[f"请先在设置中配置 {label} API Key。"] = ("model_credentials", label + " 尚未配置凭证")
        for suffix, code, note in (
            (" 凭证无效或已过期，请重新配置或登录。", "model_http_401", "凭证无效或过期"),
            (" 余额不足。", "model_http_402", "余额不足"),
            (" 未授权使用此模型或额度。", "model_http_403", "模型或额度权限不足"),
            (" 额度不足或请求频繁，请检查用量。", "model_http_429", "额度不足或请求频繁"),
        ):
            messages[label + suffix] = (code, label + "：" + note)
        for status in range(400, 600):
            messages[f"{label} 返回 HTTP {status}，请检查模型与账号权限。"] = (
                f"model_http_{status}", f"{label} 返回 HTTP {status}")
    # Only exact known ValueError messages are safe to display; never echo
    # arbitrary exception text, chained errors, URLs or response content.
    if type(error) is ValueError and str(error) in messages:
        return messages[str(error)]
    if isinstance(error, TimeoutError):
        return "model_network_timeout", "模型连接超时"
    if isinstance(error, json.JSONDecodeError):
        return "model_response_json_invalid", "模型服务响应不是有效 JSON"
    category = type(error).__name__ if type(error) in (ValueError, RuntimeError, TypeError, KeyError, OSError) else "Exception"
    return "model_error_unclassified", "未分类模型错误（" + category + "）；未显示原始异常内容"


def collect_context(frame, box, ocr, limit=10):
    """Read the last few readable text blocks and tag them by direction.

    Returns chronological (text, incoming) pairs. Unreadable blocks (emoji,
    images, notices) are skipped rather than guessed: a missing line is better
    than an invented one.
    """
    import numpy as np
    from bubble_box import is_incoming, text_blocks

    strip = frame.crop(box)
    blocks = text_blocks(strip.width, strip.height, strip.tobytes())
    if not blocks:
        return []
    candidates = blocks[-(limit + 1):]
    items = []
    for block in candidates:
        cut = strip.crop(block)
        array = np.frombuffer(cut.tobytes(), dtype=np.uint8).reshape(cut.height, cut.width, 4)
        rgb = np.ascontiguousarray(array[:, :, :3][:, :, ::-1])
        text = "".join(getattr(ocr(rgb), "txts", None) or ()).strip()
        if not text:
            continue
        items.append((text, is_incoming(block, strip.width)))
    return items


def fold_context(context, limit=30):
    """Turn the read messages into a single user-role message.

    DeepSeek's JSON mode returns whitespace - finish_reason "stop", a handful of
    tokens, no content - as soon as the conversation contains assistant-role
    turns. Measured on the real prompt: no history worked, three history turns
    returned "       ", ten returned "    ". Folding the same words into one
    user message with 我/对方 labels brings valid JSON back every time, and keeps
    the content the model needs.

    This is a workaround for the provider, not a judgement about the chat.
    """
    if not context:
        return []
    lines = []
    for item in context[-limit:]:
        text = item.get("text", "")
        if not text:
            continue
        speaker = "对方" if item.get("incoming") else "我"
        lines.append(f"{speaker}：{text[:2000]}")
    if not lines:
        return []
    folded = ("最近的对话记录（只用于理解上下文，不要逐条回应，只回答最后一条）：\n"
              + "\n".join(lines))
    return [{"role": "user", "content": folded}]


def build_model(calibration, log=print):
    """Real generator backed by chat_core.ChatAI (deepseek / openai / chatgpt).

    The provider comes from the saved settings, so switching services is a
    configuration change, not a code change.
    """
    from chat_core import APP_ROOT, ChatAI, Store

    store = Store(APP_ROOT)
    ai = ChatAI(store)
    config = store.config()
    profile = next(p for p in config["profiles"] if p["id"] == config["selected"])
    if profile["name"] != calibration["title"]:
        raise ValueError("selected_contact_differs_from_calibration")
    scope = "visual:" + calibration["title"]

    def generate(text, context=None):
        history = fold_context(context)
        # A malformed model answer is not a send: nothing has been staged, so
        # asking once more cannot duplicate a message. Without this, one bad JSON
        # reply stopped the whole session - which the user saw as "it answers
        # once and then goes quiet for the second message".
        last_error = None
        for attempt in (1, 2):
            try:
                return ai.decide(profile, scope, text, confirmed_context=history,
                                 expected_config=config)
            except ValueError as error:
                message = str(error)
                retryable = ("模型没有返回规定格式" in message
                             or "模型动作无效" in message
                             or "模型文本格式无效" in message)
                if not retryable:
                    raise
                last_error = error
                log(f"    model reply was malformed (attempt {attempt}/2); "
                    f"asking once more", flush=True)
                time.sleep(0.4)
        raise last_error

    return generate


def enable_pmv2():
    """Per-Monitor V2 before anything reads or creates a window.

    Without it GetWindowRect returns scaled virtual coordinates and every
    comparison against the calibrated rect fails for no visible reason.
    """
    import ctypes
    user32 = ctypes.windll.user32
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    if not user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
        # Already established is acceptable only if the PROCESS is V2 too;
        # a thread override alone must not disguise a PMv1 process.
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        user32.GetDpiAwarenessContextForProcess.argtypes = [ctypes.c_void_p]
        user32.GetDpiAwarenessContextForProcess.restype = ctypes.c_void_p
        if not user32.AreDpiAwarenessContextsEqual(
                user32.GetDpiAwarenessContextForProcess(kernel32.GetCurrentProcess()), ctypes.c_void_p(-4)):
            raise RuntimeError("process_per_monitor_v2_required")
    user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not user32.AreDpiAwarenessContextsEqual(
            user32.GetThreadDpiAwarenessContext(), ctypes.c_void_p(-4)):
        raise RuntimeError("per_monitor_v2_required")
    return "per_monitor_v2"


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="observe only (default)")
    mode.add_argument("--stage-only", action="store_true", help="paste and read back, never Enter")
    mode.add_argument("--live", action="store_true", help="paste, read back and press Enter once")
    parser.add_argument("--confirm", default="", help="exact contact name, required for --live")
    parser.add_argument("--offline-reply", action="store_true",
                        help="use the placeholder reply instead of the model")
    parser.add_argument("--seconds", type=float, default=120.0)
    args = parser.parse_args(argv)
    awareness = enable_pmv2()

    if args.live and not args.confirm:
        print("refusing --live without --confirm <contact>", flush=True)
        return 2

    calibration = load_calibration()
    if args.live and args.confirm != calibration["title"]:
        print(f"refusing: --confirm {args.confirm!r} != calibrated target "
              f"{calibration['title']!r}", flush=True)
        return 2

    ledger = Ledger(LEDGER)
    if ledger.blocks_start():
        print("refusing to start: an unfinished commit exists and must be "
              "acknowledged by a human.", flush=True)
        print("  " + ledger.describe_unfinished(), flush=True)
        return 3

    print(f"mode={'live' if args.live else 'stage-only' if args.stage_only else 'dry-run'} "
          f"dpi={awareness} target={calibration['title']} "
          f"window={calibration['window_rect']}", flush=True)

    return run_hosting(calibration, ledger,
                       live=args.live, stage_only=args.stage_only,
                       dry_run=args.dry_run, offline_reply=args.offline_reply,
                       seconds=args.seconds)


def run_hosting(calibration, ledger, *, live=False, stage_only=False,
                 dry_run=True, offline_reply=False, seconds=3600.0,
                 log=print, stop_event=None):
    """The single hosting loop. CLI and GUI both call this.

    log(message) receives every step; stop_event lets a caller stop the loop
    between frames without killing the process.
    """
    stop_event = stop_event if stop_event is not None else threading.Event()

    def event(kind, **values):
        # GUI consumes structured messages, not fragile parsing of chat text.
        log("@hosting " + json.dumps({"kind": kind, **values}, ensure_ascii=False), flush=True)

    if ledger.blocks_start():
        event("system", text="账本存在未完成发送，待人工确认", state="待人工确认")
        return 3
    if stop_event.is_set():
        return 0
    capture, read, adapter, collect = build_adapters(calibration)
    try:
        if stop_event.is_set():
            return 0
        first = capture()
        initial = read(first, True)
        if not initial.geometry_ok or not initial.title_ok:
            event("system", text="暂停：窗口几何或会话标题不匹配", state="已暂停")
            return 2
        if initial.scrolling:
            # The baseline (and, without a calibrated padding, the bottom
            # anchor) come from this frame, so it must show the bottom.
            event("system", text="未启动：聊天区不在最新消息底部（或底边被遮挡）。"
                  "请滚到最底部后重新开始", state="已暂停")
            return 2
        if stop_event.is_set():
            return 0
        generate = stub_generator if offline_reply else build_model(calibration, log)
        loop = Loop()
        loop.note_baseline(initial.text, initial.incoming, initial.above)
        first_pending = [first]
        latest = [None]

        def session_capture():
            return first_pending.pop() if first_pending else capture()

        def session_read(frame, changed):
            reading = read(frame, changed)
            latest[0] = reading
            return reading

        session = Session(capture=session_capture, read=session_read,
                          geometry=Geometry(calibration["window_rect"],
                                            calibration["messages_frame"]), loop=loop)
        deadline = time.monotonic() + seconds
        event("system", text="会话校验通过；首帧仅建基线，不补历史", state="托管中")
        held = [""]
        while time.monotonic() < deadline and not stop_event.is_set():
            version = loop.version
            decision, detail = session.step()
            if loop.version != version and latest[0] and latest[0].text:
                # Only what the loop accepted as new. A repaint of the same
                # bubble, or history seen while scrolled, is not a message.
                event("incoming", text=latest[0].text)
            if stop_event.is_set():
                break
            log(f"[{time.strftime('%H:%M:%S')}] {decision.value:9s} {detail}", flush=True)
            if loop.held != held[0]:
                if loop.held == "scrolled":
                    event("system", text="聊天区不在最底部：暂不处理，回到底部后自动继续")
                elif loop.held:
                    event("system", text="最新消息与上一条接不上（可能刚滚动过）：暂不处理，"
                          "回到底部后自动继续")
                else:
                    event("system", text="已回到底部，继续托管")
                held[0] = loop.held
            if decision is Decision.GENERATE:
                context = []
                if not offline_reply:
                    try:
                        started_context = time.monotonic()
                        items = collect()
                        # The newest item is the message itself; it is passed
                        # separately as `text` so the model never sees it twice.
                        if items and items[-1][0] == detail:
                            items = items[:-1]
                        context = [{"text": text, "incoming": incoming}
                                   for text, incoming in items]
                        log(f"    context={len(context)} msgs in "
                            f"{time.monotonic() - started_context:.2f}s "
                            f"{[c['text'] for c in context][:4]}", flush=True)
                    except Exception as error:
                        log(f"    context failed: {error}", flush=True)
                        context = []
                started_model = time.monotonic()
                try:
                    if stop_event.is_set():
                        break
                    outcome = (stub_generator(detail) if offline_reply
                               else generate(detail, context))
                except Exception as error:
                    code, reason = safe_model_failure(error)
                    log(f"    model failed: {code}", flush=True)
                    event("system", text=f"暂停：模型请求失败 [{code}]：{reason}", state="已暂停")
                    loop.pause("model_failed")
                    break
                if isinstance(outcome, str):
                    reply, action = outcome, "reply"
                else:
                    action = outcome.action
                    reply = outcome.text
                log(f"    model action={action} reply={reply!r} "
                      f"in {time.monotonic() - started_model:.2f}s"
                      f"{' reason=' + outcome.reason if not isinstance(outcome, str) else ''}",
                      flush=True)
                if stop_event.is_set():
                    break
                if action != "reply":
                    if action in ("handoff", "stop"):
                        loop.pause(f"model_{action}")
                        log(f"    paused: model_{action}", flush=True)
                        event("system", text=f"暂停：模型要求 {action}", state="已暂停")
                        break
                    session.generation_skipped()
                    event("system", text="模型选择等待，本轮未发送")
                    continue
                event("reply", text=reply)
                session.generation_finished(reply)
                if dry_run:
                    continue
                if live or stage_only:
                    log("    preparing foreground with activate(); no center-click focus...",
                          flush=True)
                    if stop_event.is_set():
                        break
                    started_stage = time.monotonic()
                    stage_notes = []
                    result, problem = stage_reply_real(reply, calibration, stop_event=stop_event,
                                                       notes=stage_notes)
                    if result is None:
                        log(f"    STAGE aborted: {problem}", flush=True)
                    else:
                        log(f"    stage={result.outcome.value} reason={result.reason} "
                              f"in {time.monotonic() - started_stage:.2f}s "
                              f"text={result.text!r}", flush=True)
                    if stage_only:
                        log("    stage-only: stopping without pressing Enter", flush=True)
                        break
                    if result is None or result.outcome.value != "STAGED":
                        log("    not staged: refusing to press Enter", flush=True)
                        codes = "，".join(stage_notes) or problem or "无原因码"
                        if result is not None and result.outcome.value == "DRAFT_PRESENT":
                            text = ("暂停：输入框里已有内容（文字或图片），草稿保护生效，"
                                    "未粘贴、未提交。请先处理输入框再重新开始")
                        else:
                            text = "暂停：未通过暂存/草稿保护/逐字回读；未提交"
                            if stage_notes and all("copy_did_not_run" in n for n in stage_notes):
                                # Live 21:4x: Ctrl+C copied nothing - keyboard
                                # focus was not in WeChat's input box.
                                text = ("暂停：微信输入框没有键盘焦点（复制没有生效）。请在微信输入框里点一下，"
                                        "托管期间别点微信的其他区域，再重新开始；未粘贴、未提交")
                        event("system", text=f"{text} [{codes}]", state="已暂停")
                        break
                    if stop_event.is_set():
                        event("system", text="停止已请求；保留输入内容，未提交")
                        break
                    decision, detail = session.readback_finished(
                        True, result.reason)
                    log(f"    {decision.value} {detail}", flush=True)
                    if decision is not Decision.COMMIT:
                        break
                    from hosting_stage import commit_and_verify
                    from input_readback_clipboard import NativeKeyboard
                    baseline = capture()
                    if stop_event.is_set():
                        break

                    class StopAwareKeyboard:
                        def send_enter(self):
                            if stop_event.is_set():
                                raise RuntimeError("stop_requested_before_enter")
                            NativeKeyboard().send_enter()

                    verify_trace = {}
                    delivered, commit_id = commit_and_verify(
                        reply=reply,
                        target=calibration["title"],
                        ledger=ledger,
                        keyboard=StopAwareKeyboard(),
                        verify=lambda: verify_delivery(
                            reply, baseline=baseline, capture=capture,
                            box=tuple(calibration["messages_frame"]),
                            input_box=tuple(calibration["input_frame"]),
                            threshold=0.004, trace=verify_trace),
                    )
                    decision, detail = session.commit_finished(delivered)
                    log(f"    delivered={delivered} commit={commit_id} "
                          f"{decision.value} {detail}", flush=True)
                    log(f"    verify {json.dumps(verify_trace, ensure_ascii=False)}", flush=True)
                    record_verify_trace(ledger, commit_id, verify_trace)
                    event("verification", text=reply, delivered=delivered,
                          trace=verify_trace)
                    if not delivered:
                        log("    UNKNOWN: frozen, next start will refuse", flush=True)
                    if decision is Decision.PAUSE:
                        break
            if decision is Decision.PAUSE:
                if detail == "unreadable_new_message":
                    event("incoming", text="[非文字] 未处理")
                if detail == "user_took_over" and latest[0] and latest[0].text:
                    event("outgoing", text=latest[0].text)
                event("system", text="暂停：" + detail,
                      state="待人工确认" if detail == "unknown_after_commit" else "已暂停")
                log(f"    paused: {detail}", flush=True)
                break
            # 0.25 s, was 0.5 s: a step costs ~0.1 s now that the frame
            # comparison is vectorised, so new messages are noticed sooner.
            stop_event.wait(LOOP_INTERVAL_S)
    except KeyboardInterrupt:
        log("interrupted", flush=True)
    finally:
        adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

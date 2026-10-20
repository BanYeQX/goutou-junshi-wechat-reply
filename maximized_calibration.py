"""Pure physical-pixel calibration contracts. No GUI/native/capture/input imports.

New data is namespaced; legacy client-relative ROIs are NOT reinterpreted as
PrintWindow coordinates. All rectangles are [left, top, right, bottom).
"""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

KEY = "maximized_calibration"
REGIONS = ("messages", "input", "send")
MODE = "per_monitor_v2"
CANVAS = "GetWindowRect/GetWindowDC/PrintWindow"
TEST_TEXT = "标定测试，勿发送"
CLEANUP_TEXT = "测试文字已清除、未发送。"


class CalibrationError(ValueError):
    pass


def ints(value, length):
    if not isinstance(value, (list, tuple)) or len(value) != length or any(type(n) is not int for n in value):
        raise CalibrationError("invalid_integer_coordinates")
    return list(value)


def rect(value):
    l, t, r, b = ints(value, 4)
    if r <= l or b <= t:
        raise CalibrationError("empty_or_reversed_rectangle")
    return [l, t, r, b]


def screen_to_frame(rect, window_origin):
    """Translate physical pixels only. No scaling, clipping, guessing or I/O."""
    l, t, r, b = ints(rect, 4)
    if r <= l or b <= t:
        raise CalibrationError("empty_or_reversed_rectangle")
    x, y = ints(window_origin, 2)
    return [l-x, t-y, r-x, b-y]


def inside(inner, outer):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def center(box):
    l, t, r, b = rect(box)
    return [(l+r)//2, (t+b)//2]


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise CalibrationError("invalid_snapshot")
    if (snapshot.get("dpi_awareness") != MODE or snapshot.get("pmv2_verified") is not True
            or snapshot.get("dpi_verification") != "AreDpiAwarenessContextsEqual(-4)"
            or snapshot.get("canvas") != CANVAS):
        raise CalibrationError("exact_pmv2_required")
    if snapshot.get("maximized") is not True:
        raise CalibrationError("maximized_window_required")
    box = rect(snapshot.get("window_rect"))
    if ints(snapshot.get("window_origin"), 2) != box[:2]:
        raise CalibrationError("window_origin_mismatch")
    if ints(snapshot.get("frame_size"), 2) != [box[2]-box[0], box[3]-box[1]]:
        raise CalibrationError("frame_size_mismatch")
    if type(snapshot.get("dpi")) is not int or snapshot["dpi"] <= 0:
        raise CalibrationError("invalid_dpi")
    binding = snapshot.get("binding", {})
    if any(type(binding.get(k)) is not int or binding[k] <= 0 for k in ("hwnd", "pid", "created")):
        raise CalibrationError("invalid_window_binding")
    if not all(type(binding.get(k)) is str and binding[k] for k in ("class_name", "version")):
        raise CalibrationError("invalid_window_binding")
    monitors = snapshot.get("monitors")
    if not isinstance(monitors, list) or not monitors or len(monitors) > 32:
        raise CalibrationError("invalid_monitor_layout")
    names = []
    for monitor in monitors:
        name = monitor.get("device") if isinstance(monitor, dict) else None
        if type(name) is not str or not name or name in names:
            raise CalibrationError("invalid_monitor_layout")
        names.append(name)
        if not inside(rect(monitor.get("work_rect")), rect(monitor.get("monitor_rect"))):
            raise CalibrationError("invalid_monitor_layout")
        if type(monitor.get("primary")) is not bool:
            raise CalibrationError("invalid_monitor_layout")
    if snapshot.get("window_monitor") not in names:
        raise CalibrationError("invalid_window_monitor")
    return snapshot


def assert_geometry(saved, current):
    validate_snapshot(saved)
    validate_snapshot(current)
    # Exact snapshot, including monitor layout, origin, DPI, binding and V2.
    # A moved window invalidates even if screen_to_frame could translate it.
    if saved != current:
        raise CalibrationError("geometry_dpi_or_monitor_changed_recalibrate")


def validate_regions(snapshot, regions):
    validate_snapshot(snapshot)
    if not isinstance(regions, dict) or set(regions) != set(REGIONS):
        raise CalibrationError("three_regions_required")
    monitor = next(m for m in snapshot["monitors"] if m["device"] == snapshot["window_monitor"])
    for name in REGIONS:
        region = regions[name]
        if not isinstance(region, dict):
            raise CalibrationError("invalid_region")
        screen, frame = rect(region.get("screen_rect")), rect(region.get("frame_rect"))
        if not inside(screen, snapshot["window_rect"]) or not inside(screen, monitor["monitor_rect"]):
            raise CalibrationError("region_outside_window_or_monitor")
        if frame != screen_to_frame(screen, snapshot["window_origin"]):
            raise CalibrationError("screen_frame_conversion_mismatch")
        if not inside(frame, [0, 0, *snapshot["frame_size"]]):
            raise CalibrationError("region_outside_frame")
    for i, name in enumerate(REGIONS):
        for other in REGIONS[i+1:]:
            if overlap(regions[name]["screen_rect"], regions[other]["screen_rect"]):
                raise CalibrationError("regions_overlap")
    send = regions["send"]
    if send.get("screen_center") != center(send["screen_rect"]) or send.get("frame_center") != center(send["frame_rect"]):
        raise CalibrationError("send_center_mismatch")
    if send.get("measurement_condition") != "input_nonempty":
        raise CalibrationError("send_button_not_measured_with_nonempty_input")


def target_name(document):
    """The contact the saved 区域校准 binding was made for. Any contact the
    user calibrated; this used to accept only one hard-coded name."""
    name = document.get("expected_title") if isinstance(document, dict) else None
    if (not isinstance(name, str) or not name.strip() or len(name) > 80
            or document.get("target_kind") != "contact"):
        raise CalibrationError("saved_contact_binding_required")
    return name.strip()


def build_calibration(snapshot, screen_rects, *, empty_confirmed, typed_confirmed, cleanup_confirmation,
                      borders_confirmed, target, clock=None):
    validate_snapshot(snapshot)
    if not isinstance(target, str) or not target.strip() or len(target) > 80:
        raise CalibrationError("saved_contact_binding_required")
    if empty_confirmed is not True or typed_confirmed is not True or borders_confirmed is not True:
        raise CalibrationError("human_confirmations_required")
    if cleanup_confirmation != CLEANUP_TEXT:
        raise CalibrationError("cleanup_not_confirmed")
    if set(screen_rects) != set(REGIONS):
        raise CalibrationError("three_regions_required")
    regions = {name: {"screen_rect": rect(screen_rects[name]),
                      "frame_rect": screen_to_frame(screen_rects[name], snapshot["window_origin"])} for name in REGIONS}
    regions["send"].update(screen_center=center(regions["send"]["screen_rect"]),
                           frame_center=center(regions["send"]["frame_rect"]), measurement_condition="input_nonempty")
    result = {"schema_version": 1, "snapshot": deepcopy(snapshot), "regions": regions,
              "confirmed_target": target.strip(), "rect_format": "[left,top,right,bottom)",
              "input_empty_before_test_confirmed": True, "test_text_manually_typed_confirmed": True,
              "test_text": TEST_TEXT, "cleanup_confirmation": cleanup_confirmation,
              "borders_confirmed": True, "reloaded_borders_confirmed": False,
              "saved_at": (clock() if clock else datetime.now().astimezone().isoformat(timespec="seconds"))}
    validate_calibration(result)
    return result


def validate_calibration(value, current=None):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1 or not isinstance(value.get("confirmed_target"), str)
            or not value["confirmed_target"].strip() or len(value["confirmed_target"]) > 80):
        raise CalibrationError("invalid_calibration_schema")
    if value.get("rect_format") != "[left,top,right,bottom)":
        raise CalibrationError("invalid_rectangle_format")
    validate_regions(value.get("snapshot"), value.get("regions"))
    if (value.get("input_empty_before_test_confirmed") is not True
            or value.get("test_text_manually_typed_confirmed") is not True or value.get("borders_confirmed") is not True
            or value.get("test_text") != TEST_TEXT or value.get("cleanup_confirmation") != CLEANUP_TEXT
            or type(value.get("reloaded_borders_confirmed")) is not bool):
        raise CalibrationError("human_confirmations_required")
    if current is not None:
        assert_geometry(value["snapshot"], current)
    return value


def crop_frame(image, value, region_name, current):
    """For a PrintWindow frame only; never crop it with screen coordinates."""
    validate_calibration(value, current)
    if list(image.size) != value["snapshot"]["frame_size"] or region_name not in REGIONS:
        raise CalibrationError("frame_size_or_region_mismatch")
    # PixelImage.crop accepts one (l,t,r,b) tuple, as does Pillow.Image.crop.
    return image.crop(tuple(value["regions"][region_name]["frame_rect"]))


class CalibrationStore:
    """Cancel is no write. Preserve unrelated fields, refuse concurrent changes."""
    def __init__(self, path):
        self.path = Path(path)
        self.original = self.path.read_bytes()
        if len(self.original) > 1_048_576:
            raise CalibrationError("configuration_too_large")
        self.document = json.loads(self.original.decode("utf-8"))
        self.pending = None
        if not isinstance(self.document, dict):
            raise CalibrationError("invalid_configuration")

    def save(self, value, current):
        validate_calibration(value, current)
        if self.path.read_bytes() != self.original:
            raise CalibrationError("configuration_changed_no_overwrite")
        merged = deepcopy(self.document)
        merged[KEY] = deepcopy(value)
        raw = (json.dumps(merged, ensure_ascii=False, indent=2)+"\n").encode("utf-8")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".calibration-", suffix=".tmp", dir=self.path.parent, delete=False) as stream:
                temp = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.read_bytes() != self.original:
                raise CalibrationError("configuration_changed_no_overwrite")
            os.replace(temp, self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        self.original, self.document = raw, merged
        # Read the bytes actually on disk; callers must preview these, not drafts.
        return self.load(current)

    def load(self, current):
        document = json.loads(self.path.read_text(encoding="utf-8"))
        return validate_calibration(document.get(KEY), current)

    def stage(self, value, current):
        """Persist/reload a disposable candidate, leave real configuration alone."""
        validate_calibration(value, current)
        self.cancel()
        with tempfile.NamedTemporaryFile(prefix=".calibration-preview-", suffix=".json", dir=self.path.parent, delete=False) as stream:
            self.pending = Path(stream.name)
            stream.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        return validate_calibration(json.loads(self.pending.read_text(encoding="utf-8")), current)

    def cancel(self):
        """Discard only this tool's preview. NEVER restore an old config value."""
        if self.pending is not None:
            self.pending.unlink(missing_ok=True)
            self.pending = None


def result_report(value=None):
    if value is None:
        return ("本轮是否改变了某个结论：具体变化。\n\n"
                "标定工具就绪，真实坐标待用户标定。\n"
                "三个screen_rect/frame_rect、window_origin/window_rect/frame_size、DPI模式、"
                "按钮测量条件、清理及重载边框确认均待用户操作；未写入猜测坐标。\n")
    validate_calibration(value)
    s = value["snapshot"]
    lines = ["本轮是否改变了某个结论：具体变化。", "", "# 最大化标定结果（用户手动操作）", "",
             "真实坐标已保存；不是发送测试。", "",
             "| 区域 | screen_rect | frame_rect |", "| --- | --- | --- |"]
    for name in REGIONS:
        r = value["regions"][name]
        lines.append(f"| {name} | {r['screen_rect']} | {r['frame_rect']} |")
    lines += ["", f"window_origin={s['window_origin']}; window_rect={s['window_rect']}; frame_size={s['frame_size']}",
              f"DPI={s['dpi']}; dpi_awareness={s['dpi_awareness']}; 精确V2核验={s['dpi_verification']}; maximized={s['maximized']}",
              f"显示器={s['monitors']}; 目标显示器={s['window_monitor']}",
              f"发送按钮screen_center={value['regions']['send']['screen_center']}; frame_center={value['regions']['send']['frame_center']}",
              "按钮测量条件：输入框非空；固定测试文字由用户手动输入，程序未代填。",
              f"清理确认（用户声明，不是OCR）：{value['cleanup_confirmation']}",
              f"临时保存重载边框及中心点人工确认={value['reloaded_borders_confirmed']}；正式提交后再次从实际配置读回显示。",
              f"保存时间={value['saved_at']}", "", "几何、DPI、最大化或显示器布局有任何变化即失效；不自动平移/缩放旧配置。",
              f"复测：手动最大化并确认{value['confirmed_target']} → 双击calibrate-maximized.cmd → 加载已存标定 → 人工核对边框和中心点。无需截图或发送。",
              "本报告是人工声明/元数据，不是发送或截图验证。", ""]
    return "\n".join(lines)

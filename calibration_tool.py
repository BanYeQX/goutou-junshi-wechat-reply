"""User-launched, ten-minute maximized calibrator. Default invocation is inert.

Never capture, OCR, focus WeChat, inject input, touch clipboard or call models.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import time

from maximized_calibration import (CalibrationError, CalibrationStore, REGIONS, TEST_TEXT,
    CLEANUP_TEXT, assert_geometry, build_calibration, result_report, target_name)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "data" / "visual_calibration.json"
# Local and private like the calibration itself (data/ is never committed).
REPORT = ROOT / "data" / "calibration-result.md"
LABELS = {"messages": "消息区（不含会话列表、标题、工具栏和输入区）", "input": "文字输入框（不含发送按钮）", "send": "实际可见的发送按钮"}


def public_reason(error):
    if isinstance(error, CalibrationError):
        return str(error)  # Only this module's fixed public codes.
    # CaptureError.reason is already allowlisted by the existing backend.
    from wechat_capture import CaptureError
    return error.reason if isinstance(error, CaptureError) else "native_or_file_operation_failed"


class CalibrationTool:
    def __init__(self, root, native, store, *, clock=time.monotonic, report_path=REPORT):
        import tkinter as tk
        from tkinter import messagebox
        self.tk, self.messagebox = tk, messagebox
        self.root, self.native, self.store, self.clock = root, native, store, clock
        self.target = target_name(store.document)
        self.report_path = report_path
        self.deadline = clock()+600
        self.snapshot = self.value = None
        self.regions, self.start = {}, None
        self.overlay = self.canvas = None
        self.virtual_origin = (0, 0)
        self.closed = self.accepted = self.empty_confirmed = self.typed_confirmed = False
        self.phase = "initial"
        root.title("最大化标定 · 不截图、不输入、不发送")
        root.geometry("900x680")
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        self.status = tk.StringVar(value=f"先手动切到{self.target}并最大化微信。工具不会替你切换、输入或清空内容。")
        tk.Label(root, textvariable=self.status, wraplength=750, justify="left", font=("Microsoft YaHei UI", 12)).pack(padx=20, pady=18)
        tk.Label(root, justify="left", text=(f"① 确认{self.target}、最大化，检查没有草稿/附件/输入法组合内容。\n"
            "② 点击空态确认后，自己在微信输入下方固定文字（不要发送）。\n"
            "③ 返回本工具，框选消息区、输入框、可见的发送按钮。\n"
            "④ 自己清除测试文字，回来逐字确认清理，再保存和重载核对。\n\n"
            "一轮最多10分钟；取消不覆盖旧配置。按钮不可见就取消，不能猜位置。"), font=("Microsoft YaHei UI", 11)).pack(padx=20, pady=8)
        tk.Label(root, text=TEST_TEXT, font=("Microsoft YaHei UI", 16), fg="#b34022").pack(pady=8)
        self.begin_button = tk.Button(root, text="确认空态，开始本轮标定", command=self.begin)
        self.begin_button.pack(pady=4)
        self.typed_button = tk.Button(root, text="已手动输入固定文字且未发送 → 开始框选", command=self.typed_ready, state="disabled")
        self.typed_button.pack(pady=4)
        self.verify_button = tk.Button(root, text="加载已存标定，仅复测边框（不输入文字）", command=self.verify_existing)
        self.verify_button.pack(pady=4)
        self.cleanup_entry = tk.Entry(root, width=55)
        self.save_button = tk.Button(root, text="确认清理，临时保存并重载核对", command=self.save_and_preview)
        tk.Button(root, text="取消并保留旧配置", command=self.cancel).pack(pady=5)
        root.after(250, self.tick)

    def guarded(self, action):
        try:
            self.ensure_time()
            return action()
        except Exception as error:
            # No exception text/path/credential/chat logging. Public UI message.
            self.fail(public_reason(error))

    def ensure_time(self):
        if self.closed or self.clock() >= self.deadline:
            raise CalibrationError("ten_minute_calibration_deadline")

    def current(self):
        self.ensure_time()
        current = self.native.snapshot()
        if self.snapshot is not None:
            assert_geometry(self.snapshot, current)
        return current

    def begin(self):
        def action():
            if not self.messagebox.askyesno("空态人工确认", f"请确认：顶部完整显示{self.target}、微信已最大化，输入框没有已有草稿、附件或未提交的输入法组合内容。\n\n如有任何内容，请选否并保留，不要清空。是否全部符合？", parent=self.root):
                self.cancel()
                return
            self.snapshot = self.current()
            self.empty_confirmed = True
            self.phase = "await_manual_typing"
            self.begin_button.configure(state="disabled")
            self.verify_button.configure(state="disabled")
            self.typed_button.configure(state="normal")
            self.status.set("现在请手动切到微信，输入“"+TEST_TEXT+"”，不要按回车/发送。按钮实际可见后，回本工具点开始框选。")
        self.guarded(action)

    def typed_ready(self):
        def action():
            if self.phase != "await_manual_typing" or not self.empty_confirmed:
                raise CalibrationError("invalid_calibration_phase")
            self.current()
            if not self.messagebox.askyesno("测量条件人工确认", "是否已由你手动输入“"+TEST_TEXT+"”且未发送，并且发送按钮现在实际可见？\n不可见请选否，停止本轮，不猜位置。", parent=self.root):
                self.cancel()
                return
            self.typed_confirmed = True
            self.phase = "select"
            self.show_overlay(select=True)
        self.guarded(action)

    def virtual_rect(self):
        monitors = self.snapshot["monitors"]
        return [min(m["monitor_rect"][0] for m in monitors), min(m["monitor_rect"][1] for m in monitors),
                max(m["monitor_rect"][2] for m in monitors), max(m["monitor_rect"][3] for m in monitors)]

    def show_overlay(self, *, select=False):
        self.current()
        self.root.withdraw()
        top = self.tk.Toplevel(self.root)
        self.overlay = top
        top.withdraw()
        top.overrideredirect(True)
        top.protocol("WM_DELETE_WINDOW", self.cancel)
        top.attributes("-alpha", 0.30)  # NOT transparentcolor; entire window hit-tests.
        top.attributes("-topmost", True)
        bounds = self.virtual_rect()
        self.virtual_origin = tuple(bounds[:2])
        top.geometry(f"{bounds[2]-bounds[0]}x{bounds[3]-bounds[1]}+0+0")
        self.canvas = self.tk.Canvas(top, bg="#152b3a", highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        bar = self.tk.Frame(self.canvas, bg="white")
        self.overlay_status = self.tk.Label(bar, fg="black", bg="white", font=("Microsoft YaHei UI", 12))
        self.overlay_status.pack(side="left", padx=8)
        self.overlay_accept = self.tk.Button(bar, command=self.accept_overlay)
        self.overlay_accept.pack(side="left", padx=8)
        self.tk.Button(bar, text="取消（保留旧配置）", command=self.cancel).pack(side="left", padx=8)
        target_monitor = next(m for m in self.snapshot["monitors"] if m["device"] == self.snapshot["window_monitor"])
        # Virtual desktop's upper-left can be a gap in an L-shaped layout.
        # Put OWN controls on the measured target monitor, never in that gap.
        self.canvas.create_window(target_monitor["work_rect"][0]-bounds[0]+12,
                                  target_monitor["work_rect"][1]-bounds[1]+12, anchor="nw", window=bar)
        top.bind("<Escape>", lambda event: self.cancel())
        top.bind("<BackSpace>", self.undo)
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        top.deiconify()
        top.update_idletasks()
        self.native.place_overlay(top.winfo_id(), bounds)
        self.overlay_accept.configure(text=("确认三框准确 → 手动清理测试文字" if select else
            "确认实际配置边框准确，结束复测" if self.phase == "verify" else "确认重载三框准确，正式保存"),
                                      state="disabled" if select else "normal")
        self.draw()
        self.overlay_status.configure(text=("依次拖框："+LABELS[REGIONS[0]] if select else "已从保存文件重载：请核对三框及发送中心十字"))
        # Only focuses THIS owned surface; no SetForegroundWindow/WeChat input.
        top.focus_set()

    def press(self, event):
        def action():
            if self.phase == "select" and len(self.regions) < 3:
                self.current()
                self.start = self.native.cursor()
        self.guarded(action)
        return "break"

    def motion(self, event):
        def action():
            if self.start is not None:
                self.canvas.delete("drag")
                x, y = self.native.cursor()
                a, b = self.virtual_origin
                self.canvas.create_rectangle(self.start[0]-a, self.start[1]-b, x-a, y-b, outline="yellow", width=3, tags="drag")
        self.guarded(action)
        return "break"

    def release(self, event):
        def action():
            if self.start is None or self.phase != "select":
                return
            x, y = self.native.cursor()
            l, t = self.start
            self.start = None
            value = [min(l,x), min(t,y), max(l,x), max(t,y)]
            self.current()
            from maximized_calibration import rect, inside, overlap
            rect(value)
            monitor = next(m for m in self.snapshot["monitors"] if m["device"] == self.snapshot["window_monitor"])
            if not inside(value, self.snapshot["window_rect"]) or not inside(value, monitor["monitor_rect"]):
                raise CalibrationError("region_outside_window_or_monitor")
            if any(overlap(value, r) for r in self.regions.values()):
                raise CalibrationError("regions_overlap")
            name = REGIONS[len(self.regions)]
            self.regions[name] = value
            self.draw()
            self.overlay_status.configure(text=("三框已选；Backspace可重画最后一框" if len(self.regions)==3 else "下一框："+LABELS[REGIONS[len(self.regions)]]))
            self.overlay_accept.configure(state="normal" if len(self.regions)==3 else "disabled")
        self.guarded(action)
        return "break"

    def undo(self, event=None):
        if self.phase == "select" and self.regions:
            self.regions.pop(next(reversed(self.regions)))
            self.start = None
            self.draw()
            self.overlay_status.configure(text="重画："+LABELS[REGIONS[len(self.regions)]])
            self.overlay_accept.configure(state="disabled")
        return "break"

    def draw(self):
        self.canvas.delete("region")
        self.canvas.delete("drag")
        a, b = self.virtual_origin
        for name, value in self.regions.items():
            l, t, r, bottom = value
            color = {"messages":"#00ff88", "input":"#ffff00", "send":"#ff8844"}[name]
            self.canvas.create_rectangle(l-a, t-b, r-a, bottom-b, outline=color, width=3, tags="region")
            self.canvas.create_text(l-a+4, t-b+4, anchor="nw", fill=color, text=name, tags="region")
            if name == "send":
                x, y = (l+r)//2-a, (t+bottom)//2-b
                self.canvas.create_line(x-8,y,x+8,y, fill=color,width=3,tags="region")
                self.canvas.create_line(x,y-8,x,y+8, fill=color,width=3,tags="region")

    def accept_overlay(self):
        def action():
            self.current()
            if self.phase == "select":
                if len(self.regions) != 3:
                    raise CalibrationError("three_regions_required")
                self.hide_overlay()
                self.phase = "await_cleanup"
                self.typed_button.configure(state="disabled")
                self.status.set("三框待保存。请手动切到微信，只清除你刚输入的“"+TEST_TEXT+"”，不要发送。回来在下框逐字填写：“"+CLEANUP_TEXT+"”")
                self.cleanup_entry.pack(pady=6)
                self.save_button.pack(pady=4)
            elif self.phase == "reload":
                value = deepcopy(self.value)
                value["reloaded_borders_confirmed"] = True
                self.value = self.store.save(value, self.current())
                self.accepted = True  # Explicit final confirmation: no later old-value restoration.
                self.store.cancel()  # Remove owned preview, not the real file.
                self.regions = {name: self.value["regions"][name]["screen_rect"] for name in REGIONS}
                self.draw()  # Same accepted borders, now re-read from actual final file.
                self.finish_report()
            elif self.phase == "verify":
                # Read-only retest does not modify the calibration or its time.
                self.finish_report()
        self.guarded(action)

    def hide_overlay(self):
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = self.canvas = None
        self.root.deiconify()

    def save_and_preview(self):
        def action():
            if self.phase != "await_cleanup":
                raise CalibrationError("invalid_calibration_phase")
            self.value = build_calibration(self.snapshot, self.regions, empty_confirmed=self.empty_confirmed,
                typed_confirmed=self.typed_confirmed, borders_confirmed=True,
                cleanup_confirmation=self.cleanup_entry.get(), target=self.target)
            self.value = self.store.stage(self.value, self.current())
            # Preview MUST come from a saved/reloaded file, not the canvas draft.
            # The real config remains unchanged until explicit final acceptance.
            self.regions = {name: self.value["regions"][name]["screen_rect"] for name in REGIONS}
            self.phase = "reload"
            self.show_overlay()
        self.guarded(action)

    def verify_existing(self):
        def action():
            if not self.messagebox.askyesno("当前会话确认", f"请手动确认：当前顶部完整显示{self.target}，微信已最大化。这里只重载边框，不用输入测试文字。是否确认？", parent=self.root):
                return
            self.snapshot = self.current()
            self.value = self.store.load(self.snapshot)
            self.regions = {name: self.value["regions"][name]["screen_rect"] for name in REGIONS}
            self.phase = "verify"
            self.show_overlay()
        self.guarded(action)

    def finish_report(self):
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(result_report(self.value), encoding="utf-8")
        self.accepted = True
        # Keep the final-file borders visible behind this owned completion box.
        self.messagebox.showinfo("正式配置已重载", "后方边框现在来自正式配置的读回。已完成三框与中心点核对，未截图、未发送。\n后续复测请用“加载已存标定”。几何/DPI/显示器布局变化后必须重新标定。", parent=self.overlay or self.root)
        self.close()

    def fail(self, code):
        self.hide_overlay()
        self.status.set("本轮停止："+code+"。若你已手动输入测试文字，请自行保留或清理，工具不会操作微信。未确认清理不记为已清理。")
        self.messagebox.showerror("标定未完成", self.status.get(), parent=self.root)
        self.cancel(show=False)

    def cancel(self, *, show=True):
        if self.closed:
            return
        try:
            if not self.accepted:
                self.store.cancel()
            # The topmost selection layer must not cover a cancel/error dialog
            # parented to the (previously withdrawn) control window.
            self.hide_overlay()
            if show:
                self.messagebox.showinfo("已取消", "旧配置保留。若你已输入测试文字，请自行处理；工具没有清空或发送，也不声明你已清理。", parent=self.root)
        except Exception:
            self.messagebox.showerror("未覆盖外部修改", "配置在本轮期间被其他程序改过，工具没有覆盖。请保留现状人工核对；本轮未验收。", parent=self.root)
        finally:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.root.destroy()

    def tick(self):
        if self.closed:
            return
        try:
            self.ensure_time()
            if self.snapshot is not None:
                self.current()
        except Exception:
            self.fail("timeout_or_geometry_dpi_monitor_changed")
            return
        self.root.after(500, self.tick)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="User only: creates the owned calibration UI; no screenshots/input")
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({"status": "not_started", "screenshots": 0, "input_actions": 0}))
        return 0
    # Independent one-shot process lifetime cap, even if a native metadata call
    # or dialog blocks Tk's after callback. No child/WeChat process is killed.
    import os
    import threading
    watchdog = threading.Timer(600, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    root = tool = None
    try:
        from calibration_native import CalibrationNative, binding_from_document
        store = CalibrationStore(CONFIG)
        native = CalibrationNative(*binding_from_document(store.document))
        # BEFORE creating Tk or reading coordinates. Reuse capture's exact V2
        # context, not Tk scaling(), process-global DPI or a guessed multiplier.
        with native.backend.physical_pixels():
            import tkinter as tk
            root = tk.Tk()
            root.update_idletasks()
            native.own_window(root.winfo_id())
            tool = CalibrationTool(root, native, store)
            root.mainloop()
        return 0
    except Exception as error:
        print("标定未完成："+public_reason(error)+"。请用普通非管理员上下文，保留现状；未截图或输入，未自动重试。")
        return 2
    finally:
        watchdog.cancel()
        if tool is not None and not tool.closed:
            tool.cancel(show=False)
        elif root is not None and tool is None:
            root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())

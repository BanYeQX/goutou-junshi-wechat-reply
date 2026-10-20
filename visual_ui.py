"""Local-only rectangle calibration. Never serializes or uploads its image."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from visual_capture import WindowsCapture
from visual_types import Box, Calibration, VisualPause


class CalibrationWizard:
    def __init__(self, root, profile, callback):
        self.root, self.profile, self.callback = root, profile, callback
        self.backend = WindowsCapture()
        self.events = queue.Queue()
        self.closed = False
        self.image = self.photo = None
        self.boxes = []
        self.stage = 0
        self.window = tk.Toplevel(root)
        self.window.title("区域校准 · 第一阶段仅本地只读")
        self.window.geometry("780x430+80+60")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.configure(bg="white")
        self.note = tk.StringVar(value="先在微信手动打开目标单聊。本工具不自动切换或搜索聊天。")
        tk.Label(self.window, textvariable=self.note, bg="white", wraplength=730, justify="left", padx=18, pady=15).pack(fill="x")
        self.setup = tk.Frame(self.window, bg="white", padx=18)
        self.setup.pack(fill="both", expand=True)
        self.targets = {"文件传输助手（只读测试，不改变联系人资料）": ("test:filehelper", "文件传输助手", "filehelper")}
        if profile:
            self.targets["所选联系人：" + profile["name"]] = (profile["id"], profile["name"], "contact")
        self.target = tk.StringVar(value=next(iter(self.targets)))
        ttk.Combobox(self.setup, textvariable=self.target, values=list(self.targets), state="readonly").pack(fill="x", pady=6)
        self.windows = self.backend.windows()
        self.picker = ttk.Combobox(self.setup, values=[f"微信窗口 {i + 1} · {binding.width}×{binding.height} · {title}" for i, (binding, title) in enumerate(self.windows)], state="readonly")
        self.picker.pack(fill="x", pady=8)
        if len(self.windows) == 1:
            self.picker.current(0)
        self.confirmed = tk.BooleanVar(value=False)
        tk.Checkbutton(self.setup, text="我已打开上面指定的唯一单聊，并确认不是群聊；同意仅本机框选和只读识别", variable=self.confirmed, bg="white", wraplength=730, justify="left").pack(anchor="w", pady=12)
        tk.Label(self.setup, text="校准时仅在本机内存显示所选微信窗口供你框选，不识别聊天列表、不上传截图、不写图片文件。\n框选完整标题和消息区；消息区不要包含列表、工具栏或输入框。\n完成校准不会自动识别或发送。", bg="white", justify="left", wraplength=730).pack(fill="x", pady=8)
        self.begin_button = tk.Button(self.setup, text="开始区域校准（5秒后截取）", command=self.begin, bg="#24735C", fg="white", padx=14, pady=9)
        self.begin_button.pack(pady=10)
        self.window.after(100, self.drain)

    def begin(self):
        index = self.picker.current()
        if index < 0 or not self.confirmed.get():
            return messagebox.showinfo("尚未确认目标", "请选择微信窗口，并由你本人确认已打开正确的唯一单聊。", parent=self.window)
        self.binding = self.windows[index][0]
        self.chosen = self.targets[self.target.get()]
        self.begin_button.config(state="disabled")
        self.countdown(5)

    def countdown(self, seconds):
        if self.closed:
            return
        if seconds:
            self.note.set(f"{seconds}秒后仅截取你选定的微信窗口，请现在手动切到该微信窗口。不会输入或发送。")
            self.window.after(1000, lambda: self.countdown(seconds - 1))
            return
        def worker():
            try:
                result = self.backend.calibration_frame(self.binding)
                if self.closed:
                    result.close()
                else:
                    self.events.put((result, ""))
            except Exception as error:
                self.events.put((None, str(error) if isinstance(error, VisualPause) else "校准截取失败；未操作微信。请确认窗口可见且在前台。"))
        threading.Thread(target=worker, daemon=True).start()

    def drain(self):
        try:
            image, error = self.events.get_nowait()
        except queue.Empty:
            if not self.closed:
                self.window.after(100, self.drain)
            return
        if self.closed:
            if image:
                image.close()
            return
        if error:
            self.note.set(error)
            self.begin_button.config(state="normal")
        else:
            self.show_image(image)
        self.window.after(100, self.drain)

    def show_image(self, image):
        self.setup.destroy()
        self.image = image
        self.factor = min(1.0, (self.root.winfo_screenwidth() - 110) / image.width, (self.root.winfo_screenheight() - 240) / image.height)
        scaled = image.resize((round(image.width * self.factor), round(image.height * self.factor)), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(scaled)
        scaled.close()
        self.window.geometry(f"{self.photo.width() + 36}x{self.photo.height() + 170}+25+25")
        self.note.set("第1步：拖框圈住完整联系人标题，不能含列表、按钮或消息正文。所有画面仅在本机内存。")
        self.canvas = tk.Canvas(self.window, width=self.photo.width(), height=self.photo.height(), highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=18)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<ButtonPress-1>", self.down)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.up)
        footer = tk.Frame(self.window, bg="white")
        footer.pack(fill="x", padx=18, pady=10)
        tk.Button(footer, text="重新框选", command=self.reset).pack(side="left")
        self.save_button = tk.Button(footer, text="保存校准（不开始读取）", command=self.save, state="disabled")
        self.save_button.pack(side="right")
        # Only our local calibration window is raised; WeChat is never activated.
        self.window.deiconify()
        self.window.lift()

    def down(self, event):
        if self.stage >= 2:
            return
        self.start_point = (max(0, event.x), max(0, event.y))
        self.current_rectangle = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#E36A21" if self.stage == 0 else "#24735C", width=3, tags="calibration")

    def move(self, event):
        if self.stage < 2 and hasattr(self, "start_point"):
            self.canvas.coords(self.current_rectangle, *self.start_point, min(self.photo.width(), max(0, event.x)), min(self.photo.height(), max(0, event.y)))

    def up(self, event):
        if self.stage >= 2 or not hasattr(self, "start_point"):
            return
        x, y = self.start_point
        del self.start_point
        end_x, end_y = min(self.photo.width(), max(0, event.x)), min(self.photo.height(), max(0, event.y))
        box = Box(round(min(x, end_x) / self.factor), round(min(y, end_y) / self.factor), round(max(x, end_x) / self.factor), round(max(y, end_y) / self.factor))
        if box.width < 20 or box.height < 15:
            self.canvas.delete(self.current_rectangle)
            return
        self.boxes.append(box)
        self.stage += 1
        if self.stage == 1:
            self.note.set("第2步：框选该单聊的消息区域；排除左侧列表、上方标题、下方输入框及工具栏。尽量包含完整左右气泡。")
        else:
            self.note.set("请核对橙框完整标题、绿框仅消息区。保存后才可手动按“只读检测”；不会自动读取或发送。")
            self.save_button.config(state="normal")

    def reset(self):
        self.canvas.delete("calibration")
        self.boxes.clear()
        self.stage = 0
        self.save_button.config(state="disabled")
        self.note.set("第1步：重新框选完整联系人标题。")

    def save(self):
        try:
            calibration = Calibration(self.binding, self.chosen[0], self.chosen[1], self.boxes[0], self.boxes[1], True, self.chosen[2]).validate()
            if self.backend.binding(self.binding.hwnd) != self.binding:
                raise VisualPause("框选期间微信尺寸或窗口改变，请重新校准。")
            self.callback(calibration)
            self.close()
        except VisualPause as error:
            messagebox.showinfo("校准未保存", str(error), parent=self.window)

    def close(self):
        self.closed = True
        if self.image:
            self.image.close()
        self.photo = self.image = None
        while not self.events.empty():
            image, _ = self.events.get_nowait()
            if image:
                image.close()
        self.window.destroy()

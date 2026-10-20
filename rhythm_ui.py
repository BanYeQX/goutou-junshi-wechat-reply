"""聊天节奏 K 线 panel: import a CSV (or pick a demo) and draw daily candles.

Local only: no model, no network, message text is never kept. The disclaimer
sits under the chart on purpose - the curve is message direction, not a score.
"""
from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

import chat_rhythm
from app import C, FONT

UP, DOWN = C["green"], C["amber"]


class RhythmPanel:
    def __init__(self, parent, root):
        self.root = root
        self.series = ()
        self.source = ""
        self.frame = tk.Frame(parent, bg=C["bg"])
        top = tk.Frame(self.frame, bg=C["bg"])
        top.pack(fill="x", pady=(14, 8))
        tk.Button(top, text="导入聊天 CSV…", command=self.import_csv, bg=C["green"], fg="white",
                  relief="flat", bd=0, padx=14, pady=8, cursor="hand2").pack(side="left")
        tk.Label(top, text="或看示例", bg=C["bg"], fg=C["muted"], font=(FONT, 9)).pack(side="left", padx=(16, 6))
        self.demo_name = tk.StringVar(value="")
        demos = ttk.Combobox(top, values=list(chat_rhythm.DEMOS), textvariable=self.demo_name,
                             state="readonly", width=14)
        demos.pack(side="left")
        demos.bind("<<ComboboxSelected>>", lambda _e: self.show_demo(self.demo_name.get()))
        self.info = tk.Label(self.frame, text="", bg=C["bg"], fg=C["muted"], font=(FONT, 9), anchor="w")
        self.info.pack(fill="x", pady=(0, 6))

        self.canvas = tk.Canvas(self.frame, bg=C["panel"], highlightthickness=1,
                                highlightbackground=C["line"], height=360)
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        note = tk.Label(self.frame, text=chat_rhythm.DISCLAIMER + "\nCSV 表头：timestamp,sender,message；"
                        "sender 填 me（我）或 other（对方），时间从早到晚。",
                        bg=C["bg"], fg=C["muted"], font=(FONT, 9), justify="left", anchor="w")
        note.pack(fill="x", pady=(8, 0), side="bottom")
        note.bind("<Configure>", lambda e: note.config(wraplength=max(e.width - 8, 200)))
        # Packed after the note so the note keeps its lines at small sizes.
        self.canvas.pack(fill="both", expand=True)

    def import_csv(self):
        path = filedialog.askopenfilename(parent=self.root, title="选择聊天 CSV",
                                          filetypes=[("CSV", "*.csv")])
        if path:
            self.load(path)

    def load(self, path):
        try:
            series = chat_rhythm.load_csv(path)
        except chat_rhythm.RhythmError as error:
            self.info.config(text=str(error), fg=C["amber"])
            return False
        self.demo_name.set("")
        self.show(series, Path(path).name)
        return True

    def show_demo(self, name):
        self.demo_name.set(name)
        self.show(chat_rhythm.demo(name), "示例：" + name + "（虚构数据）")

    def show(self, series, source):
        self.series, self.source = tuple(series), source
        mine = sum(c.mine for c in self.series)
        theirs = sum(c.theirs for c in self.series)
        self.info.config(text=f"{source} · {len(self.series)} 天 · 我 {mine} 条 / 对方 {theirs} 条",
                         fg=C["muted"])
        self.draw()

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 160)
        if not self.series:
            canvas.create_text(width / 2, height / 2, text="导入 CSV 或选一个示例", fill=C["muted"],
                               font=(FONT, 11))
            return
        left, right, top, bottom = 52, 16, 18, 34
        low = min(min(c.low for c in self.series), 0)
        high = max(max(c.high for c in self.series), 0)
        span = max(high - low, 1)

        def y(value):
            return top + (high - value) * (height - top - bottom) / span

        zero = y(0)
        canvas.create_line(left, zero, width - right, zero, fill=C["line"], dash=(4, 3))
        for value in sorted({high, 0, low}):
            canvas.create_text(left - 8, y(value), text=str(value), anchor="e", fill=C["muted"], font=(FONT, 8))
        count = len(self.series)
        step = (width - left - right) / count
        body = max(min(step * 0.6, 26), 2)
        label_every = max(1, round(count / 8))
        for index, candle in enumerate(self.series):
            x = left + step * (index + 0.5)
            colour = UP if candle.close > candle.open else DOWN if candle.close < candle.open else C["muted"]
            canvas.create_line(x, y(candle.high), x, y(candle.low), fill=colour, width=1)
            y1, y2 = sorted((y(candle.open), y(candle.close)))
            canvas.create_rectangle(x - body / 2, y1, x + body / 2, max(y2, y1 + 2), fill=colour, outline=colour)
            if index % label_every == 0 or index == count - 1:
                canvas.create_text(x, height - bottom + 14, text=candle.day[5:] if candle.day[4:5] == "-"
                                   else candle.day, fill=C["muted"], font=(FONT, 8))

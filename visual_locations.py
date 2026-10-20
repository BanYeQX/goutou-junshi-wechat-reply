"""Read-only geometry schematic. No screenshot, OCR, capture, model or input."""
from __future__ import annotations

import tkinter as tk


def location_rows(rows, width, height):
    """Geometry/status only; never include chat text, hashes or identities."""
    if width <= 0 or height <= 0:
        raise ValueError("Invalid message region")
    result = []
    for index, row in enumerate(rows, 1):
        if not row.box.inside(width, height) or any(not box.inside(width, height) for box in row.evidence_boxes):
            raise ValueError("Location outside calibrated message region")
        result.append({"index": index, "safe": row.safe, "box": row.box.tuple(),
                       "evidence": tuple(box.tuple() for box in row.evidence_boxes)})
    return tuple(result)


class LocationPreview:
    def __init__(self, root, rows, width, height, is_current):
        locations = location_rows(rows, width, height)
        self.is_current, self.closed = is_current, False
        self.window = tk.Toplevel(root)
        self.window.title("位置核对 · 仅示意框，不是微信截图")
        self.window.configure(bg="white")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(self.window, text="消息区位置示意（不是截图，不显示聊天正文）\n绿框：文字候选；红框：不确定块；红色虚线：该块内具体疑点行。编号与结果区一致。",
                 bg="white", justify="left", padx=14, pady=10).pack(anchor="w")
        available_w = min(720, root.winfo_screenwidth() - 160)
        available_h = min(470, root.winfo_screenheight() - 330)
        factor = min(available_w / width, available_h / height)
        canvas = tk.Canvas(self.window, width=round(width * factor) + 24, height=round(height * factor) + 24,
                           bg="#F5F4EF", highlightthickness=0)
        canvas.pack(padx=14)
        canvas.create_rectangle(12, 12, width * factor + 12, height * factor + 12, outline="#727C76")
        def coords(box):
            return tuple(value * factor + 12 for value in box)
        for item in locations:
            color = "#24735C" if item["safe"] else "#BB4728"
            canvas.create_rectangle(*coords(item["box"]), outline=color, width=2)
            x, y, _, _ = coords(item["box"])
            canvas.create_text(x + 4, y + 3, text=str(item["index"]), fill=color, anchor="nw")
            for evidence in item["evidence"]:
                canvas.create_rectangle(*coords(evidence), outline="#BB4728", width=2, dash=(4, 3))
        details = tk.Text(self.window, width=76, height=6, wrap="word", relief="flat", padx=14, pady=10)
        details.pack(fill="both", expand=True)
        for item in locations:
            x0, y0, x1, y1 = item["box"]
            label = "文字候选" if item["safe"] else "不确定"
            details.insert("end", f"{item['index']}. {label}：消息区内 x={x0}–{x1}, y={y0}–{y1}；内部疑点行 {len(item['evidence'])} 处\n")
        details.config(state="disabled")
        tk.Label(self.window, text="只展示这一轮已有位置；不再次截取、不操作微信、不代表人工核对通过。\n停止、重测、切换联系人或结果更新后，此窗口自动关闭。", bg="white", justify="left", padx=14, pady=8).pack(anchor="w")
        tk.Button(self.window, text="关闭位置示意", command=self.close).pack(pady=(0, 10))
        self.window.after(150, self.check)

    def check(self):
        if self.closed:
            return
        if not self.is_current():
            self.close()
            return
        self.window.after(150, self.check)

    def close(self):
        if not self.closed:
            self.closed = True
            self.window.destroy()

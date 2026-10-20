"""One-run consent only. Opening this window NEVER starts a capture."""
import tkinter as tk

from shadow_integration import ShadowConsent


class ShadowConsentWindow:
    def __init__(self, root, calibration, on_confirm):
        self.calibration = calibration
        self.on_confirm = on_confirm
        self.closed = False
        self.window = tk.Toplevel(root)
        self.window.title("新算法只读对照 · 本次确认")
        self.window.transient(root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _: self.close())
        frame = tk.Frame(self.window, padx=18, pady=16)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="仅本次本地只读对照，不是自动代聊", anchor="w").pack(fill="x")
        tk.Label(frame, text="目标：" + calibration.expected_title, anchor="w").pack(fill="x", pady=(6, 0))
        tk.Label(frame, text=("只截取已校准的完整标题和消息区域，在本机做文字识别与气泡外观对照。\n"
            "不截取整窗/输入框/会话列表，不保存或上传截图，不调用模型、不输入或发送。\n"
            "确认后5秒内手动切到微信并保持前台；无需发测试消息。\n"
            "本轮最多30秒内发起采样、最多12次快照；到期或停止时丢弃在途结果。\n"
            "本工具不验证这些消息是否尚未回复；重复或失去锚点时结束，不猜测。"),
            justify="left", anchor="w", wraplength=590).pack(fill="x", pady=8)
        self.bottom = tk.BooleanVar(master=self.window, value=False)
        self.operations = tk.BooleanVar(master=self.window, value=False)
        tk.Checkbutton(frame, text="我确认当前位于此单聊最新底部", variable=self.bottom,
                       command=self.refresh).pack(anchor="w")
        tk.Checkbutton(frame, text="本轮不翻历史、不在电脑回复、不改变微信窗口大小", variable=self.operations,
                       command=self.refresh).pack(anchor="w")
        actions = tk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        tk.Button(actions, text="取消，不读取", command=self.close).pack(side="left")
        self.confirm = tk.Button(actions, text="同意仅本次本地只读对照", command=self.approve, state="disabled")
        self.confirm.pack(side="right")
        self.window.grab_set()

    def refresh(self):
        self.confirm.config(state="normal" if self.bottom.get() and self.operations.get() else "disabled")

    def approve(self):
        if self.closed or not self.bottom.get() or not self.operations.get():
            return
        consent = ShadowConsent(self.calibration, True, True, True)
        self.close()
        self.on_confirm(consent)

    def close(self):
        if not self.closed:
            self.closed = True
            self.window.grab_release()
            self.window.destroy()

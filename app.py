"""狗头军师聊天台 — local, native Windows app. No sending on first launch."""
from __future__ import annotations

import queue
import json
import hashlib
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from chat_core import APP_ROOT, ChatAI, PROVIDER_NAMES, Store, new_profile, skill_text
from plan_auth import USAGE_URL
from engine import Engine
from wechat_bridge import compatibility
from visual_monitor import ReadOnlyMonitor
from visual_types import Calibration, VisualPause, save_calibration
from title_ocr import TITLE_OCR_VERSION
from visual_drafts import ReviewSession
from visual_sync import safe_alignment_diagnostic


C = {"bg": "#F5F4EF", "panel": "#FFFFFF", "ink": "#252C2A", "muted": "#727C76",
     "line": "#E1E5DF", "green": "#24735C", "soft": "#EAF3EC", "amber": "#8C5C29", "sand": "#FBF0DD"}
FONT = "Microsoft YaHei UI"


class App:
    def __init__(self, root):
        self.root = root
        self.store = Store()
        self.ai = ChatAI(self.store)
        self.engine = Engine(self.store, self.ai)
        self.sandbox = new_profile("本地演练")
        self.preview_busy = False
        self.names = []
        self.logs = []
        self.draft_token = ""
        self.current_profile = None
        self.visual = ReadOnlyMonitor(self.visual_selected)
        self.visual_last = None
        self.visual_shadow_report = None
        self.visual_shadow_dialog = None
        self.visual_location = None
        self.visual_review = ReviewSession()
        self.visual_draft_window = None
        self.visual_job_busy = False
        self.visual_draft_stats = {"jobs_started": 0, "jobs_completed": 0, "stale_results_discarded": 0}
        self.visual_calibration = None
        self.visual_path = self.store.data / "visual_calibration.json"
        if self.visual_path.exists():
            try:
                self.visual_calibration = Calibration.from_dict(json.loads(self.visual_path.read_text(encoding="utf-8")))
            except (ValueError, TypeError, KeyError):
                pass
        self.root.title("狗头军师 · 聊天台")
        width = min(1180, self.root.winfo_screenwidth() - 80)
        height = min(800, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(980, width), min(690, height))
        self.root.configure(bg=C["bg"])
        self.root.option_add("*Font", (FONT, 10))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _: self.pause())
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=C["panel"], padding=6)
        self.build()
        self.refresh_profiles()
        self.refresh_bridge()
        self.root.after(150, self.drain)
        if self.store.config()["autostart"]:
            self.root.after(1000, self.start)

    def label(self, parent, text, size=10, color=None, bg=None, **kw):
        return tk.Label(parent, text=text, font=(FONT, size), fg=color or C["ink"], bg=bg or C["panel"], **kw)

    def button(self, parent, text, command, primary=False, **kw):
        return tk.Button(parent, text=text, command=command, bg=C["green"] if primary else C["panel"],
                         fg="white" if primary else C["ink"], activebackground=C["soft"], relief="flat", bd=0,
                         padx=16, pady=kw.pop("pady", 10), cursor="hand2", **kw)

    def build(self):
        side = tk.Frame(self.root, bg=C["panel"], width=255, padx=22, pady=26)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.label(side, "GOUTOU  /  CHAT", 10, C["green"]).pack(anchor="w")
        self.label(side, "狗头军师", 24).pack(anchor="w", pady=(10, 0))
        self.label(side, "自然聊，也保留分寸。", 10, C["muted"]).pack(anchor="w", pady=(8, 32))
        self.label(side, "指定联系人", 11).pack(anchor="w")
        self.label(side, "一次只接管一位，资料各自独立", 9, C["muted"]).pack(anchor="w", pady=(6, 12))
        self.contact_list = tk.Listbox(side, relief="flat", bd=0, bg=C["bg"], fg=C["ink"],
            selectbackground=C["green"], selectforeground="white", activestyle="none", height=9,
            exportselection=False, font=(FONT, 11), highlightthickness=0)
        self.contact_list.pack(fill="x")
        self.contact_list.bind("<<ListboxSelect>>", self.select_contact)
        self.button(side, "+ 添加联系人", lambda: self.profile_dialog()).pack(fill="x", pady=(10, 0))
        self.button(side, "编辑当前资料", self.edit_profile).pack(fill="x")
        tk.Frame(side, bg=C["line"], height=1).pack(fill="x", pady=20)
        self.button(side, "设置与我的口吻", self.settings_dialog).pack(fill="x")
        self.button(side, "查看运行记录", self.show_logs).pack(fill="x")
        self.button(side, "清除当前聊天上下文", self.clear_context).pack(fill="x")
        foot = tk.Frame(side, bg=C["panel"])
        foot.pack(side="bottom", fill="x")
        self.label(foot, "ESC  随时暂停接管", 9, C["green"]).pack(anchor="w")
        self.provider_hint = tk.StringVar(value="本机配置 · " + PROVIDER_NAMES[self.store.config()["provider"]])
        self.label(foot, "", 9, C["muted"], textvariable=self.provider_hint).pack(anchor="w", pady=(8, 0))

        main = tk.Frame(self.root, bg=C["bg"], padx=28, pady=23)
        main.pack(side="left", expand=True, fill="both")
        top = tk.Frame(main, bg=C["bg"])
        top.pack(fill="x")
        self.label(top, "聊天工作台", 21, bg=C["bg"]).pack(side="left")
        self.button(top, "暂停 / 我来聊", self.pause).pack(side="right", padx=(8, 0))
        self.start_button = self.button(top, "开始接管", self.start, primary=True)
        self.start_button.pack(side="right")
        self.managed_window_button = self.button(top, "打开托管窗口", self.open_managed_window, primary=True)
        self.managed_window_button.pack(side="right", padx=(0, 8))

        self.banner = self.label(main, "正在检查微信连接……", 10, C["amber"], C["sand"],
                                 anchor="w", justify="left", wraplength=800, padx=14, pady=13)
        self.banner.pack(fill="x", pady=(18, 15))
        row = tk.Frame(main, bg=C["bg"])
        row.pack(fill="x", pady=(0, 12))
        self.mode = tk.StringVar(value=self.store.config()["mode"])
        self.mode_buttons = []
        for text, value in (("自动回复", "auto"), ("逐条确认", "review")):
            radio = tk.Radiobutton(row, text=text, variable=self.mode, value=value, command=self.change_mode,
                           bg=C["bg"], fg=C["ink"], activebackground=C["bg"], selectcolor=C["soft"])
            radio.pack(side="left", padx=(0, 12))
            self.mode_buttons.append(radio)
        self.view_mode = tk.StringVar(value="练习")
        self.view = ttk.Combobox(row, textvariable=self.view_mode, values=("练习", "微信", "视觉只读"), state="readonly", width=9)
        self.view.pack(side="right")
        self.view.bind("<<ComboboxSelected>>", lambda _: self.render_history())

        visual_bar = tk.Frame(main, bg=C["soft"], padx=8, pady=4)
        visual_bar.pack(fill="x", pady=(0, 10))
        self.label(visual_bar, "新版微信 · 本地识别 / 人工核对草稿（不发送）", 9, C["green"], C["soft"]).pack(anchor="w")
        visual_controls = tk.Frame(visual_bar, bg=C["soft"])
        visual_controls.pack(fill="x")
        for title, command in (("区域校准", self.visual_calibrate), ("只读检测", lambda: self.visual_start(True)),
                               ("开始识别", lambda: self.visual_start(False)), ("停止识别", self.visual_stop)):
            self.button(visual_controls, title, command, pady=5).pack(side="left", padx=(0, 5), pady=3)
        self.visual_confirm_button = self.button(visual_controls, "我已核对本轮结果", self.visual_confirm, pady=5)
        self.visual_confirm_button.pack(side="left", padx=(4, 0))
        self.visual_confirm_button.config(state="disabled")
        self.visual_map_button = self.button(visual_controls, "位置核对", self.visual_show_locations, pady=5)
        self.visual_map_button.pack(side="left", padx=(4, 0))
        self.visual_map_button.config(state="disabled")
        review_controls = tk.Frame(visual_bar, bg=C["soft"])
        review_controls.pack(fill="x")
        self.visual_review_button = self.button(review_controls, "文字核对 / 草稿", self.visual_open_review, pady=3)
        self.visual_review_button.pack(side="left", pady=(0, 3))
        self.label(review_controls, "人工快照不建基线；草稿需逐条核对及本次上传确认", 9, C["muted"], C["soft"]).pack(side="left", padx=8)
        shadow_controls = tk.Frame(visual_bar, bg=C["soft"])
        shadow_controls.pack(fill="x")
        self.visual_shadow_button = self.button(shadow_controls, "新算法只读对照（实验）", self.visual_open_shadow, pady=3)
        self.visual_shadow_button.pack(side="left")
        self.label(shadow_controls, "默认关闭；每轮确认；不进入草稿或发送", 9, C["muted"], C["soft"]).pack(side="left", padx=8)
        self.visual_status = tk.StringVar(value="已保存校准，本轮未读取；请手动检测。" if self.visual_calibration else "尚未校准；不自动读取、不调用模型、不输入或发送微信。")
        self.label(visual_bar, "", 9, C["muted"], C["soft"], textvariable=self.visual_status, anchor="w", wraplength=790, justify="left").pack(fill="x")

        chat_panel = tk.Frame(main, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        chat_panel.pack(fill="both", expand=True)
        chat_header = tk.Frame(chat_panel, bg=C["panel"], padx=18, pady=14)
        chat_header.pack(fill="x")
        self.contact_title = self.label(chat_header, "先试聊，再接管", 14)
        self.contact_title.pack(side="left")
        self.scope_label = self.label(chat_header, "练习消息不会发送到微信", 9, C["muted"])
        self.scope_label.pack(side="right")
        tk.Frame(chat_panel, bg=C["line"], height=1).pack(fill="x")
        transcript_frame = tk.Frame(chat_panel, bg=C["panel"])
        transcript_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(transcript_frame)
        scrollbar.pack(side="right", fill="y")
        self.transcript = tk.Text(transcript_frame, bg=C["panel"], fg=C["ink"], relief="flat", bd=0,
            wrap="word", height=1, width=1, padx=22, pady=20, spacing1=5, spacing3=10, cursor="arrow", state="disabled",
            yscrollcommand=scrollbar.set)
        self.transcript.pack(fill="both", expand=True)
        scrollbar.config(command=self.transcript.yview)
        self.transcript.tag_configure("user_title", foreground=C["muted"], font=(FONT, 9))
        self.transcript.tag_configure("assistant_title", foreground=C["green"], font=(FONT, 9))
        self.transcript.tag_configure("message", font=(FONT, 12), spacing3=15)
        self.transcript.tag_configure("notice", foreground=C["muted"], font=(FONT, 10), spacing3=15)

        compose = tk.Frame(chat_panel, bg=C["panel"], padx=16, pady=12)
        compose.pack(fill="x")
        self.input = tk.Entry(compose, bg=C["bg"], fg=C["ink"], relief="flat", font=(FONT, 11),
                              highlightbackground=C["line"], highlightthickness=1)
        self.input.pack(side="left", fill="x", expand=True, ipady=12, padx=(0, 12))
        self.input.bind("<Return>", lambda _: self.preview())
        self.preview_button = self.button(compose, "试一句", self.preview, primary=True)
        self.preview_button.pack(side="right")
        self.label(chat_panel, "在这里输入对方一句话，生成练习回复；不会发到微信。", 9, C["muted"]).pack(anchor="w", padx=18, pady=(0, 12))
        usage_row = tk.Frame(chat_panel, bg=C["panel"])
        usage_row.pack(fill="x", padx=18, pady=(0, 8))
        self.billing_hint = self.label(usage_row, "", 9, C["green"])
        self.billing_hint.pack(side="left")
        self.usage_button = self.button(usage_row, "管理 ChatGPT 用量", lambda: webbrowser.open(USAGE_URL), pady=2)

        draft_panel = tk.Frame(main, bg=C["bg"])
        draft_panel.pack(fill="x", pady=(12, 0))
        self.draft_text = tk.StringVar(value="逐条确认模式下，待发送的草稿会显示在这里。")
        self.draft_entry = tk.Entry(draft_panel, textvariable=self.draft_text, bg=C["panel"], fg=C["muted"],
                                    relief="flat", state="readonly", readonlybackground=C["panel"])
        self.draft_entry.pack(side="left", fill="x", expand=True, ipady=13, padx=(0, 10))
        self.approve_button = self.button(draft_panel, "确认发送", self.approve)
        self.approve_button.pack(side="right")
        self.approve_button.configure(state="disabled")
        self.status_text = tk.StringVar(value=skill_text()[1])
        self.label(main, "", 9, C["muted"], C["bg"], textvariable=self.status_text, anchor="w").pack(fill="x", pady=(12, 0))

    def log(self, text):
        self.logs.append(time.strftime("%H:%M:%S  ") + text)
        del self.logs[:-100]
        self.status_text.set(text[:110])

    def refresh_bridge(self):
        provider = self.store.config()["provider"]
        self.provider_hint.set("本机配置 · " + PROVIDER_NAMES[provider])
        self.billing_hint.config(text={"openai": "使用 OpenAI API 项目额度", "chatgpt": "使用 ChatGPT 计划 · 需已授权", "deepseek": "使用 DeepSeek API 余额"}[provider])
        if provider == "chatgpt":
            self.billing_hint.config(text="使用 ChatGPT 计划 · " + ("已授权" if self.ai.plan.status()["sharing"] else "尚未授权"))
            self.usage_button.pack(side="right")
        else:
            self.usage_button.pack_forget()
        self.bridge_info = compatibility(self.store.config()["adapter"])
        if not self.engine.running:
            self.banner.config(text=self.bridge_info["message"], fg=C["green"] if self.bridge_info["ready"] else C["amber"],
                               bg=C["soft"] if self.bridge_info["ready"] else C["sand"])
        self.start_button.config(text="开始接管" if self.bridge_info["ready"] else "查看连接限制")

    def refresh_profiles(self):
        cfg = self.store.config()
        self.names = cfg["profiles"]
        self.contact_list.delete(0, "end")
        for i, p in enumerate(self.names):
            self.contact_list.insert("end", "  " + p["name"])
            if p["id"] == cfg["selected"]:
                self.contact_list.selection_set(i)
        self.current_profile = self.store.selected()
        self.contact_title.config(text=self.current_profile["name"] if self.current_profile else "先试聊，再接管")
        self.render_history()

    def select_contact(self, _=None):
        chosen = self.contact_list.curselection()
        if not chosen:
            return
        profile = self.names[chosen[0]]
        if self.current_profile and self.current_profile["id"] == profile["id"]:
            return
        self.visual_end_review()
        self.visual_stop()
        self.visual_last = None
        self.engine.stop("已切换联系人，自动回复暂停。")
        cfg = self.store.config()
        cfg["selected"] = profile["id"]
        self.store.save(cfg)
        self.current_profile = profile
        self.contact_title.config(text=profile["name"])
        self.render_history()

    def render_history(self):
        if self.view_mode.get() == "视觉只读":
            self.visual_controls_state(True)
            return self.render_visual()
        self.visual_controls_state(self.visual.running or self.visual_review.active)
        self.contact_title.config(text=self.current_profile["name"] if self.current_profile else "先试聊，再接管")
        profile = self.current_profile or self.sandbox
        scope = ("practice:" if self.view_mode.get() == "练习" else "live:") + profile["id"]
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self.scope_label.config(text="练习消息不会发送到微信" if self.view_mode.get() == "练习" else "仅显示启用后的指定私聊")
        history = self.store.history(scope)
        if not history:
            self.append_notice("先添加微信唯一备注名，再选择接管方式。\n\n你也可以先输入一句话，试试狗头军师的回复风格。\n例如：躺着休息，听歌。")
        for item in history:
            self.append_chat(item["role"], item["content"])

    def append_chat(self, role, text):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", ("对方" if role == "user" else "你的回复") + "\n", "user_title" if role == "user" else "assistant_title")
        self.transcript.insert("end", text + "\n\n", "message")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def append_notice(self, text):
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text + "\n\n", "notice")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def edit_profile(self):
        if not self.current_profile:
            return self.profile_dialog()
        self.profile_dialog(self.current_profile)

    def dialog(self, title, width=600, height=680):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry(f"{min(width, self.root.winfo_screenwidth() - 80)}x{min(height, self.root.winfo_screenheight() - 120)}")
        dialog.configure(bg=C["panel"])
        dialog.transient(self.root)
        dialog.grab_set()
        return dialog

    def scroll_form(self, dialog):
        footer = tk.Frame(dialog, bg=C["panel"])
        footer.pack(side="bottom", fill="x", padx=26, pady=(8, 18))
        body = tk.Frame(dialog, bg=C["panel"])
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=C["panel"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        form = tk.Frame(canvas, bg=C["panel"], padx=26, pady=18)
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        return form, footer

    def profile_dialog(self, existing=None):
        self.visual_end_review()
        self.visual_stop()
        dialog = self.dialog("联系人资料", height=750)
        form, footer = self.scroll_form(dialog)
        self.label(form, "只填写你明确知道的事实", 16).pack(anchor="w")
        self.label(form, "未知可留空；每位联系人的资料和聊天各自独立。", 9, C["muted"]).pack(anchor="w", pady=(8, 16))
        values = existing or new_profile("")
        fields = {}
        for name, label in (("name", "微信唯一备注名（与微信显示完全一致）"), ("stage", "当前关系"), ("goal", "聊天目标")):
            self.label(form, label).pack(anchor="w", pady=(8, 4))
            var = tk.StringVar(value=values[name])
            tk.Entry(form, textvariable=var, relief="solid", bd=1,
                     state="readonly" if existing and name == "name" else "normal").pack(fill="x", ipady=5)
            fields[name] = var
        self.label(form, "关系背景 / 对方明确分享的兴趣（可留空）").pack(anchor="w", pady=(10, 4))
        context = tk.Text(form, height=5, wrap="word", relief="solid", bd=1)
        context.pack(fill="x")
        context.insert("1.0", values["context"])
        extras = tk.Frame(form, bg=C["panel"])
        extras.pack(fill="x", pady=10)
        for i, (name, label) in enumerate((("my_mbti", "我的MBTI（选填）"), ("their_mbti", "对方MBTI（选填）"), ("my_score", "我的主观综合评分0—100"), ("their_score", "我给对方的主观综合评分0—100"))):
            cell = tk.Frame(extras, bg=C["panel"])
            cell.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 10), pady=5)
            self.label(cell, label, 9).pack(anchor="w")
            var = tk.StringVar(value=values[name])
            tk.Entry(cell, textvariable=var, width=26).pack(fill="x", ipady=4)
            fields[name] = var
        def save():
            profile = dict(values)
            profile.update({k: v.get().strip() for k, v in fields.items()})
            profile["context"] = context.get("1.0", "end").strip()
            cfg = self.store.config()
            cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != profile["id"]] + [profile]
            cfg["selected"] = profile["id"]
            try:
                self.visual_stop()
                self.engine.stop("联系人资料已更新，接管保持暂停。")
                self.store.save(cfg)
            except ValueError as exc:
                return messagebox.showerror("请检查资料", str(exc), parent=dialog)
            self.refresh_profiles()
            dialog.destroy()
        self.button(footer, "保存并选中", save, primary=True).pack(fill="x")

    def settings_dialog(self):
        self.visual_end_review()
        self.visual_stop()
        cfg = self.store.config()
        dialog = self.dialog("设置与我的口吻", width=680, height=780)
        form, footer = self.scroll_form(dialog)
        self.label(form, "让回复更像你", 17).pack(anchor="w")
        labels = {"OpenAI API Key": "openai", "ChatGPT 会员登录": "chatgpt", "DeepSeek API Key": "deepseek"}
        provider = tk.StringVar(value=next(k for k, v in labels.items() if v == cfg["provider"]))
        self.label(form, "模型接入方式").pack(anchor="w", pady=(10, 4))
        provider_picker = ttk.Combobox(form, textvariable=provider, values=list(labels), state="readonly")
        provider_picker.pack(fill="x")
        key_status = self.label(form, "", 9, C["muted"], wraplength=600, justify="left")
        key_status.pack(anchor="w", pady=(8, 10))
        model = tk.StringVar(value=cfg["model"])
        models_for = {"openai": "gpt-5-mini", "chatgpt": "auto", "deepseek": "deepseek-flash", cfg["provider"]: cfg["model"]}
        last_provider = [cfg["provider"]]
        self.label(form, "模型（会员模式可选 auto，使用账号返回的可用模型）", 9).pack(anchor="w")
        model_picker = ttk.Combobox(form, textvariable=model, values=())
        model_picker.pack(fill="x", ipady=3, pady=(4, 8))
        model_ids = {}
        def model_value():
            return model_ids.get(model.get(), model.get()).strip()
        self.label(form, "新的 API Key（留空保留原配置，只在本机加密保存）").pack(anchor="w")
        key = tk.StringVar()
        key_entry = tk.Entry(form, textvariable=key, show="●")
        key_entry.pack(fill="x", ipady=4, pady=(4, 6))

        plan_box = tk.LabelFrame(form, text="ChatGPT 账号与计划", bg=C["panel"], fg=C["green"], padx=10, pady=8)
        plan_box.pack(fill="x", pady=(8, 10))
        plan_status = self.label(plan_box, self.ai.plan.status()["message"], 9, C["muted"])
        plan_status.pack(anchor="w")
        account_var = tk.StringVar()
        account_picker = ttk.Combobox(plan_box, textvariable=account_var, values=(), state="readonly")
        account_picker.pack(fill="x", pady=(6, 5))
        account_ids = []
        plan_buttons = []
        auth_busy = [False]
        def refresh_accounts(catalog=None):
            status = self.ai.plan.status()
            plan_status.config(text=status["message"])
            account_ids[:] = [a["id"] for a in status["accounts"]]
            account_picker.config(values=[a["label"] for a in status["accounts"]])
            if status["selected"] in account_ids:
                account_picker.current(account_ids.index(status["selected"]))
            else:
                account_var.set("")
            if catalog is not None:
                selected_model = model_value()
                model_ids.clear()
                model_ids.update({m["display_name"] + " [" + m["slug"] + "]": m["slug"] for m in catalog})
                model_picker.config(values=["auto"] + list(model_ids))
                model.set(next((label for label, slug in model_ids.items() if slug == selected_model), "auto"))

        def run_auth(task, function):
            if auth_busy[0]:
                return
            self.engine.stop("模型账号操作中，微信接管暂停。")
            auth_busy[0] = True
            account_picker.config(state="disabled")
            for button in plan_buttons:
                button.config(state="disabled")
            plan_status.config(text="请在系统浏览器完成登录授权……" if task == "login" else "正在处理……")
            def finish(result, error):
                auth_busy[0] = False
                if not dialog.winfo_exists():
                    return
                for button in plan_buttons:
                    button.config(state="normal" if labels[provider.get()] == "chatgpt" else "disabled")
                account_picker.config(state="readonly" if labels[provider.get()] == "chatgpt" else "disabled")
                if error:
                    plan_status.config(text=error, wraplength=570, justify="left")
                    self.log(error)
                else:
                    if task in ("login", "select"):
                        model_ids.clear()
                        model_picker.config(values=["auto"])
                        model.set("auto")
                    refresh_accounts(result if task == "models" else None)
                    if task == "logout":
                        plan_status.config(text=result, wraplength=570, justify="left")
                    self.log("ChatGPT 账号操作完成。")
                    if task == "login" and self.ai.plan.needs_plan_notice():
                        messagebox.showinfo("正在使用你的 ChatGPT 计划", "此 App 的合资格 AI 请求将使用你授权的 ChatGPT 计划或积分额度。\n\n可在“管理 ChatGPT 用量”中查看和调整本 App 的额度与权限。\n登录授权不等于开始微信接管。", parent=dialog)
                        self.ai.plan.acknowledge_plan()
            def worker():
                try:
                    result, error = function(), ""
                except Exception as exc:
                    result, error = None, str(exc) if isinstance(exc, ValueError) else "ChatGPT 授权操作失败，请重新尝试。"
                self.engine.emit("ui_callback", function=finish, result=result, error=error)
            threading.Thread(target=worker, daemon=True).start()

        auth_row = tk.Frame(plan_box, bg=C["panel"])
        auth_row.pack(fill="x")
        for label, command in (("Continue with ChatGPT", lambda: run_auth("login", self.ai.plan.login)),
                               ("添加账号", lambda: run_auth("login", lambda: self.ai.plan.login(new_account=True))),
                               ("退出授权", lambda: run_auth("logout", self.ai.plan.logout))):
            button = self.button(auth_row, label, command)
            button.pack(side="left", padx=(0, 4))
            plan_buttons.append(button)
        actions = tk.Frame(plan_box, bg=C["panel"])
        actions.pack(fill="x")
        refresh_button = self.button(actions, "刷新账号可用模型", lambda: run_auth("models", lambda: self.ai.plan.models(refresh=True)))
        refresh_button.pack(side="left")
        plan_buttons.append(refresh_button)
        self.button(actions, "管理 ChatGPT 用量", lambda: webbrowser.open(USAGE_URL)).pack(side="left")
        self.button(actions, "取消等待登录", self.ai.plan.cancel).pack(side="left")
        def choose_account(_=None):
            idx = account_picker.current()
            if idx >= 0:
                client = account_ids[idx]
                run_auth("select", lambda: self.ai.plan.select(client))
        account_picker.bind("<<ComboboxSelected>>", choose_account)
        refresh_accounts()
        flags = {}
        for name, label in (("use_existing_key", "沿用旧微信助手已保存的 DeepSeek Key（不复制、不显示）"),
                            ("cloud_consent", "允许将所选联系人的消息、资料和上下文发送给上述模型服务"),
                            ("remember_history", "在本机保存每位联系人最近16轮上下文（可单独清除）"),
                            ("autostart", "下次打开 App 自动接管已选联系人（仅在连接检查通过时）")):
            var = tk.BooleanVar(value=cfg[name])
            checkbox = tk.Checkbutton(form, text=label, variable=var, bg=C["panel"], anchor="w", wraplength=610, justify="left")
            checkbox.pack(fill="x", pady=2)
            if name == "use_existing_key":
                legacy_checkbox = checkbox
            flags[name] = var
        billing = self.label(form, "", 9, C["green"], wraplength=610, justify="left")
        billing.pack(anchor="w", pady=(5, 8))
        def provider_changed(_=None):
            selected = labels[provider.get()]
            if selected != last_provider[0]:
                models_for[last_provider[0]] = model_value()
                model.set(models_for[selected])
                last_provider[0] = selected
                key.set("")
                flags["cloud_consent"].set(False)
            key_entry.config(state="disabled" if selected == "chatgpt" else "normal")
            legacy_checkbox.config(state="normal" if selected == "deepseek" else "disabled")
            key_status.config(text="会员模式通过官方浏览器授权，不填写 API Key。" if selected == "chatgpt" else PROVIDER_NAMES[selected] + "：" + self.store.key_status(selected))
            for button in plan_buttons:
                button.config(state="normal" if selected == "chatgpt" and not auth_busy[0] else "disabled")
            account_picker.config(state="readonly" if selected == "chatgpt" else "disabled")
            model_picker.config(values=["auto"] + list(model_ids) if selected == "chatgpt" else ())
            billing.config(text={"openai": "使用 OpenAI API 项目额度，按 API 计费。", "chatgpt": "使用你授权的 ChatGPT 计划额度；账号需具备相应资格与共享权限。", "deepseek": "使用 DeepSeek API 账户余额。"}[selected])
        provider_picker.bind("<<ComboboxSelected>>", provider_changed)
        provider_changed()
        self.label(form, "我的真实信息（作息、爱好等；不填的内容不会被当成已知）").pack(anchor="w", pady=(12, 4))
        facts = tk.Text(form, height=4, wrap="word", relief="solid", bd=1)
        facts.insert("1.0", cfg["my_facts"])
        facts.pack(fill="x")
        self.label(form, "我的口吻 / 可粘贴几句我平时会说的话").pack(anchor="w", pady=(10, 4))
        voice = tk.Text(form, height=3, wrap="word", relief="solid", bd=1)
        voice.insert("1.0", cfg["my_style"])
        voice.pack(fill="x")
        self.label(form, "只有普通文字会自动处理。图片、语音、借款、重大承诺等交给你。", 9, C["muted"]).pack(anchor="w", pady=(12, 5))
        def save():
            if auth_busy[0]:
                return messagebox.showinfo("账号操作尚未完成", "请先完成账号操作，或取消等待登录后再保存。", parent=dialog)
            updated = self.store.config()
            updated.update({name: var.get() for name, var in flags.items()})
            updated.update(provider=labels[provider.get()], model=model_value(), my_facts=facts.get("1.0", "end").strip(), my_style=voice.get("1.0", "end").strip())
            try:
                self.visual_stop()
                self.engine.stop("设置已更新，接管保持暂停。")
                self.store.save(updated, key.get().strip())
            except ValueError as exc:
                return messagebox.showerror("设置未保存", str(exc), parent=dialog)
            dialog.destroy()
            self.log("设置已保存。" + skill_text()[1])
            self.refresh_bridge()
        self.button(footer, "保存设置", save, primary=True).pack(fill="x")
        def close_dialog():
            if auth_busy[0]:
                self.ai.plan.cancel()
            dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

    def open_managed_window(self):
        """Open the separate hosting UI, NEVER start its live loop here."""
        process = getattr(self, "managed_process", None)
        if process is not None and process.poll() is None:
            return messagebox.showinfo("托管窗口已打开", "请切换到「狗头军师 · 托管」窗口。", parent=self.root)
        from pathlib import Path
        script = APP_ROOT / "hosting_window.py"
        python = APP_ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if not python.is_file() and not getattr(sys, "frozen", False):
            python = Path(sys.executable)
        if not python.is_file() or not script.is_file():
            return messagebox.showinfo("托管窗口未打开", "未找到托管源码或项目 Python。请使用项目目录的 start-hosting.cmd。", parent=self.root)
        self.pause()  # configuration App's old connector/visual tools yield
        try:
            self.managed_process = subprocess.Popen(
                [str(python), "-B", str(script)], cwd=str(APP_ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            return messagebox.showinfo("托管窗口未打开", "无法启动托管窗口；没有开启自动回复。", parent=self.root)
        self.log("托管窗口已打开；需在新窗口选择联系人并开启。聊天台不负责收发。")

    def change_mode(self):
        self.visual_stop()
        self.engine.stop("接管方式已改变，等待你重新开始。")
        cfg = self.store.config()
        cfg["mode"] = self.mode.get()
        self.store.save(cfg)

    def start(self):
        if self.visual.running or self.visual_review.active or self.visual_job_busy or self.view_mode.get() == "视觉只读":
            return messagebox.showinfo("视觉连接器尚不能发送", "本地识别与人工核对草稿不等于自动代聊。视觉连接器没有发送能力；原 wxauto4 版本拦截仍保留。", parent=self.root)
        self.refresh_bridge()
        if not self.bridge_info["ready"]:
            return messagebox.showinfo("请使用独立托管窗口",
                self.bridge_info["message"] +
                "\n\n旧连接器（wxauto4）确实发不出去，这条路仍然是断的。\n"
                "独立托管窗口走的是另一条路：本地识别 + 剪贴板回读 + "
                "账本记账 + 一次提交，已经能真实发送并核验。\n\n"
                "请运行 hosting_window.py，在托管窗口选择已标定联系人并开启。\n"
                "在托管窗口停止或确认关闭后，才交还给你自己聊。", parent=self.root)
        try:
            self.engine.start()
            self.view_mode.set("微信")
            self.render_history()
        except ValueError as exc:
            messagebox.showinfo("暂未开始接管", str(exc), parent=self.root)

    def pause(self):
        self.visual_stop()
        self.engine.stop()
        self.refresh_bridge()

    def preview(self):
        if self.visual.running or self.visual_review.active or self.visual_job_busy or self.view_mode.get() == "视觉只读":
            return messagebox.showinfo("请使用文字核对窗口", "视觉候选须在“文字核对 / 草稿”中单独确认。关闭核对会话并切到“练习”，才能使用原有试聊。", parent=self.root)
        text = self.input.get().strip()
        if not text or self.preview_busy:
            return
        if len(text) > 8000:
            return messagebox.showinfo("消息过长", "练习消息最多8000字。", parent=self.root)
        if not self.store.config()["cloud_consent"]:
            messagebox.showinfo("先配置模型", "在设置中配置 OpenAI API、ChatGPT 会员登录或 DeepSeek，并勾选消息传输许可后，就能试聊。", parent=self.root)
            return self.settings_dialog()
        profile = dict(self.current_profile or self.sandbox)
        scope = "practice:" + profile["id"]
        self.view_mode.set("练习")
        self.render_history()
        self.append_chat("user", text)
        self.input.delete(0, "end")
        self.preview_busy = True
        self.preview_button.config(text="正在生成…", state="disabled")
        def run():
            try:
                decision = self.ai.decide(profile, scope, text)
                self.engine.emit("practice_result", profile_id=profile["id"], scope=scope, text=text, decision=decision)
            except Exception as exc:
                self.engine.emit("practice_error", text=str(exc) if isinstance(exc, ValueError) else "模型请求失败，请检查设置。")
        threading.Thread(target=run, daemon=True).start()

    def approve(self):
        if self.visual.running or self.visual_review.active or self.visual_job_busy or self.view_mode.get() == "视觉只读":
            return messagebox.showinfo("视觉连接器未开放发送", "视觉候选与草稿没有发送权限。真实发送需要下一阶段另行开发、核验并由你确认测试对象和内容。", parent=self.root)
        try:
            self.engine.approve(self.draft_token, self.draft_text.get())
            self.approve_button.config(state="disabled")
        except ValueError as exc:
            messagebox.showinfo("草稿未发送", str(exc), parent=self.root)

    def drain(self):
        for _ in range(20):
            try:
                visual_item = self.visual.events.get_nowait()
            except queue.Empty:
                break
            if visual_item["epoch"] != self.visual.epoch:
                continue
            self.visual_status.set(visual_item["message"])
            if visual_item["kind"] == "shadow":
                # Numeric hypotheses ONLY; never feed ReviewSession/Engine.
                self.visual_shadow_report = visual_item["report"]
                self.visual_last = None
                self.visual_map_button.config(state="disabled")
                self.visual_confirm_button.config(state="disabled")
                if self.view_mode.get() == "视觉只读":
                    self.render_visual()
            elif visual_item["kind"] == "shadow_end":
                if self.visual_shadow_report is None:
                    self.visual_shadow_report = {"pending": True}
                if self.view_mode.get() == "视觉只读":
                    self.render_visual()
            elif visual_item["kind"] == "snapshot":
                self.visual_shadow_report = None
                self.visual_close_locations()
                self.visual_last = visual_item
                self.invalidate_visual_draft()
                if self.visual_review.active:
                    try:
                        self.visual_review.observe(self.visual_calibration, self.store.selected(), self.store.config(), visual_item["title"], visual_item["rows"], evidence=visual_item.get("evidence"))
                    except VisualPause:
                        pass  # The session already failed closed, no raw chat in logs.
                    self.visual_refresh_review()
                if self.view_mode.get() == "视觉只读":
                    self.render_visual()
                uncertain = sum(not row.safe for row in visual_item["rows"])
                self.log(f"视觉只读：{len(visual_item['rows'])}条候选，{uncertain}条不确定；未调用模型、未发送。")
                self.visual_confirm_button.config(state="normal" if visual_item["rows"] and not uncertain else "disabled")
                self.visual_report("detected")
            elif visual_item["kind"] == "paused":
                self.visual_shadow_report = None
                self.visual_last = None
                if self.visual_review.active:
                    self.visual_review.pause("本地识别保护暂停：" + visual_item["message"] + "\n旧草稿失效，请重新只读检测。")
                    self.visual_refresh_review()
                self.visual_close_locations()
                self.visual_map_button.config(state="disabled")
                self.invalidate_visual_draft()
                self.log("视觉只读已暂停；未调用模型、未发送。")
                self.visual_confirm_button.config(state="disabled")
                self.visual_report("paused")
            self.visual_controls_state(self.visual.running or self.visual_review.active or self.view_mode.get() == "视觉只读")
        for _ in range(40):
            try:
                item = self.engine.events.get_nowait()
            except queue.Empty:
                break
            kind = item["kind"]
            if kind == "visual_draft_result":
                self.visual_job_busy = False
                session = self.visual_review
                if not session.accepts(item["token"]):
                    self.visual_draft_stats["stale_results_discarded"] += 1
                elif item["error"]:
                    session.invalidate(item["error"])
                elif not self.visual_calibration or not session.matches(self.visual_calibration, self.store.selected(), self.store.config()):
                    session.pause("联系人或设置已改变，在途草稿已丢弃。")
                    self.visual_draft_stats["stale_results_discarded"] += 1
                elif session.complete(item["token"], item["decision"]):
                    self.visual_draft_stats["jobs_completed"] += 1
                self.visual_refresh_review()
                self.visual_draft_report()
                self.log("核对草稿请求已结束；无微信输入/发送，不自动重试。")
            elif kind == "ui_callback":
                item["function"](item["result"], item["error"])
            elif kind == "status":
                self.log(item["text"])
                self.banner.config(text=item["text"], bg=C["soft"] if item["running"] else C["sand"],
                                   fg=C["green"] if item["running"] else C["amber"])
            elif kind == "log":
                self.log(item["text"])
            elif kind == "chat":
                if self.current_profile and item["profile_id"] == self.current_profile["id"] and self.view_mode.get() == "微信":
                    self.append_chat(item["role"], item["text"])
            elif kind == "draft":
                if self.visual_review.active or self.visual_job_busy or self.view_mode.get() == "视觉只读":
                    continue
                self.draft_token = item["token"]
                self.draft_entry.config(state="normal", fg=C["ink"])
                self.draft_text.set(item["text"])
                self.approve_button.config(state="normal")
                self.log("草稿待你确认：" + item["reason"])
            elif kind == "invalidate":
                self.draft_token = ""
                self.draft_text.set(item["text"])
                self.draft_entry.config(state="readonly", fg=C["muted"])
                self.approve_button.config(state="disabled")
            elif kind in ("practice_result", "practice_error"):
                self.preview_busy = False
                self.preview_button.config(text="试一句", state="normal")
                if kind == "practice_error":
                    self.append_notice(item["text"] + "（未发送到微信）")
                    self.log(item["text"])
                else:
                    decision = item["decision"]
                    self.store.append(item["scope"], "user", item["text"])
                    if decision.action == "reply":
                        self.store.append(item["scope"], "assistant", decision.text)
                    selected = self.current_profile or self.sandbox
                    if selected["id"] == item["profile_id"] and self.view_mode.get() == "练习":
                        if decision.action == "reply":
                            self.append_chat("assistant", decision.text)
                        else:
                            self.append_notice({"wait": "本轮留白", "stop": "停止联系", "handoff": "请你亲自处理"}[decision.action] + "：" + decision.reason)
                    self.log("试聊完成，未发送到微信。")
        self.root.after(150, self.drain)

    def clear_context(self):
        self.visual_end_review()
        self.visual_stop()
        if not self.current_profile:
            self.store.clear(self.sandbox["id"])
            return self.render_history()
        if messagebox.askyesno("清除当前上下文", "仅清除“" + self.current_profile["name"] + "”在本 App 中的练习和微信上下文。不会删除微信消息。确定吗？", parent=self.root):
            self.engine.stop("上下文已清除，接管暂停。")
            self.store.clear(self.current_profile["id"])
            self.render_history()

    def show_logs(self):
        dialog = self.dialog("运行记录", width=750, height=450)
        text = tk.Text(dialog, bg=C["panel"], fg=C["ink"], wrap="word", relief="flat", padx=18, pady=18)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(self.logs) or "尚无运行记录。日志不记录密钥或聊天全文。")
        text.configure(state="disabled")

    def close(self):
        self.visual_end_review()
        self.visual.stop()
        self.ai.plan.cancel()
        self.engine.stop("App 已关闭，自动回复停止。")
        self.root.destroy()

    def visual_selected(self, calibration):
        if calibration.target_kind == "filehelper":
            return "test:filehelper", "文件传输助手"
        profile = self.store.selected()
        return (profile["id"], profile["name"]) if profile else ("", "")

    def invalidate_visual_draft(self):
        self.draft_token = ""
        self.draft_text.set("视觉连接器无发送能力；核对草稿在独立窗口编辑。旧发送草稿已作废。")
        self.draft_entry.config(state="readonly", fg=C["muted"])
        self.approve_button.config(state="disabled")

    def visual_controls_state(self, read_only):
        read_only = read_only or self.visual_review.active or self.visual_job_busy
        self.preview_button.config(state="disabled" if read_only or self.preview_busy else "normal")
        self.input.config(state="disabled" if read_only else "normal")
        for button in self.mode_buttons:
            button.config(state="disabled" if read_only else "normal")
        if read_only:
            self.approve_button.config(state="disabled")

    def visual_stop(self):
        self.visual_shadow_report = None
        dialog = getattr(self, "visual_shadow_dialog", None)
        if dialog is not None:
            dialog.close()
            self.visual_shadow_dialog = None
        self.visual.stop()
        if self.visual_review.active:
            self.visual_review.pause()
            self.visual_refresh_review()
        self.visual_close_locations()
        self.visual_map_button.config(state="disabled")
        self.visual_status.set("视觉识别已停止；在途本地结果丢弃，不再截取，不操作微信。")
        self.invalidate_visual_draft()
        self.visual_controls_state(self.view_mode.get() == "视觉只读")
        self.visual_confirm_button.config(state="disabled")

    def visual_calibrate(self):
        if self.preview_busy:
            return messagebox.showinfo("请先等试聊结束", "为保持第一阶段没有模型请求，请等待当前练习请求结束后再校准。", parent=self.root)
        self.engine.stop("区域校准中，原连接器已暂停。")
        self.visual_end_review()
        self.visual_stop()
        from visual_ui import CalibrationWizard
        def saved(calibration):
            save_calibration(self.visual_path, calibration)
            self.visual_calibration = calibration
            self.visual_last = None
            self.view_mode.set("视觉只读")
            self.render_history()
            self.visual_status.set("已校准“" + calibration.expected_title + "”。尚未读取；手动按“只读检测”后切到微信。")
            self.visual_report("calibrated")
        try:
            self.calibration_wizard = CalibrationWizard(self.root, self.current_profile, saved)
        except Exception:
            messagebox.showinfo("校准暂不可用", "未能枚举已安装微信的可见窗口。未截屏、未操作微信。", parent=self.root)

    def visual_start(self, once):
        if self.preview_busy or self.engine.running or (self.engine.thread and self.engine.thread.is_alive()):
            return messagebox.showinfo("请先完成当前任务", "请先暂停原连接器、等待正在进行的试聊结束，再开始纯本地识别。", parent=self.root)
        if not self.visual_calibration:
            return messagebox.showinfo("先进行区域校准", "请点击“区域校准”，由你确认目标并框选完整标题和消息区。", parent=self.root)
        self.engine.stop("视觉第一阶段只读，原连接器保持暂停。")
        self.visual_shadow_report = None
        self.visual_close_locations()
        self.visual_map_button.config(state="disabled")
        self.invalidate_visual_draft()
        if self.visual_review.active:
            self.visual_review.reading()
            self.visual_refresh_review()
        try:
            self.visual_last = None
            self.visual_confirm_button.config(state="disabled")
            self.visual.start(self.visual_calibration, once=once)
            self.view_mode.set("视觉只读")
            self.render_history()
            self.visual_status.set("5秒后仅本地读取，请手动切到已校准的微信窗口；不会调用模型、输入或发送。")
            self.visual_refresh_review()
            self.visual_report("starting")
        except VisualPause as error:
            messagebox.showinfo("视觉识别未开始", str(error), parent=self.root)

    def visual_shadow_ready(self):
        if (self.preview_busy or self.engine.running or self.visual.running or self.visual_job_busy
                or self.visual_review.active or (self.engine.thread and self.engine.thread.is_alive())
                or (self.visual.thread and self.visual.thread.is_alive())):
            return False
        calibration = self.visual_calibration
        return bool(calibration and self.visual_selected(calibration)
                    == (calibration.profile_id, calibration.expected_title))

    def visual_open_shadow(self):
        if not self.visual_shadow_ready():
            return messagebox.showinfo("只读对照尚未开始", "请保留当前草稿，先结束核对/识别或模型任务；并选中已经校准的目标。未开始读取。", parent=self.root)
        dialog = getattr(self, "visual_shadow_dialog", None)
        if dialog is not None and not dialog.closed:
            dialog.window.lift()
            return
        from shadow_ui import ShadowConsentWindow
        self.visual_shadow_dialog = ShadowConsentWindow(self.root, self.visual_calibration, self.visual_begin_shadow)

    def visual_begin_shadow(self, consent):
        self.visual_shadow_dialog = None
        if not self.visual_shadow_ready() or consent.calibration != self.visual_calibration:
            return messagebox.showinfo("本次确认已失效", "目标、校准或任务状态已改变；未开始读取，请重新确认。", parent=self.root)
        try:
            self.visual.start_shadow(self.visual_calibration, consent)
        except VisualPause as error:
            return messagebox.showinfo("只读对照未开始", str(error), parent=self.root)
        self.engine.stop("仅本次新算法只读对照；模型/发送保持暂停。")
        self.visual_close_locations()
        self.visual_last = None
        self.visual_shadow_report = {"pending": True}
        self.visual_confirm_button.config(state="disabled")
        self.invalidate_visual_draft()
        self.view_mode.set("视觉只读")
        self.render_history()
        self.visual_controls_state(True)
        self.visual_status.set("5秒后只读对照；请手动切到校准微信并保持前台，无需发送测试消息。")

    def render_visual(self):
        current = self.visual_last and self.visual_last["epoch"] == self.visual.epoch
        self.visual_map_button.config(state="normal" if current and self.visual_last["rows"] else "disabled")
        self.transcript.config(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.config(state="disabled")
        target = self.visual_calibration.expected_title if self.visual_calibration else "未校准"
        self.contact_title.config(text="视觉目标 · " + target)
        self.scope_label.config(text="本地 OCR 候选 · 不会自动上传/发送")
        shadow_report = getattr(self, "visual_shadow_report", None)
        if shadow_report is not None:
            from shadow_integration import shadow_message
            self.append_notice("新算法只读对照（实验）：仅显示最后一次观察，不进入人工草稿、模型或发送。")
            if shadow_report.get("pending"):
                self.append_notice("本轮尚无可用的只读对照快照；请看上方状态说明。这不是重校准提示。")
            else:
                self.append_notice(shadow_message(shadow_report))
            self.append_notice("仍在采样，请保持微信前台。" if self.visual.running else
                               "本轮已结束，不会继续监控。上述结果不等于实际新消息或自动收发验收。")
            return
        self.append_notice("第一阶段：仅识别，不是自动代聊。OCR 无逐字置信分；双帧/双尺度一致仍需你人工核对。")
        if not self.visual_last:
            return self.append_notice("尚无本轮检测结果。请校准后手动按“只读检测”。")
        result = self.visual_last
        self.append_notice("完整标题核对：" + result["title"] + "（精确匹配）\n" + result["message"])
        if not result["rows"]:
            self.append_notice("未发现可确认的普通文字候选；这不能证明画面里没有非文字消息。")
        for index, row in enumerate(result["rows"], 1):
            direction = {"friend": "对方 → 我", "self": "我 → 对方", "unknown": "方向不明"}[row.direction]
            position = f"消息区内位置：x={row.box.left}–{row.box.right}，y={row.box.top}–{row.box.bottom}"
            if row.safe:
                self.append_notice(f"{index}. {direction} · {row.origin} · 文字候选（待人工核对） · {row.identity}")
                self.append_chat("user" if row.direction == "friend" else "assistant", row.text)
            else:
                evidence = f"；内部疑点行{len(row.evidence_boxes)}处（点“位置核对”看虚线框）" if row.evidence_boxes else ""
                self.append_notice(f"{index}. {direction} · 不确定/非文字 · {row.uncertainty}\n{position}{evidence}\n[未当作普通文字处理，已暂停]")

    def visual_close_locations(self):
        if self.visual_location:
            self.visual_location.close()
            self.visual_location = None

    def visual_show_locations(self):
        result, calibration = self.visual_last, self.visual_calibration
        if not result or not calibration or result["epoch"] != self.visual.epoch or not result["rows"]:
            return
        if self.visual.running:
            return messagebox.showinfo("请用单次只读检测", "持续识别尚未结束；请用单次“只读检测”取得暂停快照后核对位置。", parent=self.root)
        if self.visual_selected(calibration) != (calibration.profile_id, calibration.expected_title):
            return
        self.visual_close_locations()
        from visual_locations import LocationPreview
        self.visual_location = LocationPreview(self.root, result["rows"], calibration.messages.width, calibration.messages.height,
            lambda: self.visual_last is result and self.visual_calibration is calibration
                    and result["epoch"] == self.visual.epoch
                    and self.visual_selected(calibration) == (calibration.profile_id, calibration.expected_title))

    def visual_report(self, state, human_verified=False):
        """Local aggregate diagnostic only; no chat text, image or credentials."""
        result = self.visual_last
        rows = result["rows"] if result else ()
        snapshot_digest = hashlib.sha256("|".join(row.fingerprint for row in rows).encode()).hexdigest() if rows else ""
        data = {"stage": 1, "state": state, "title_ocr_backend": "rapidocr-" + TITLE_OCR_VERSION,
                "message_ocr_backend": "rapidocr-3.10.0-strict-message",
                "layout_localization_version": 3, "localized_evidence_count": sum(len(row.evidence_boxes) for row in rows),
                "target_kind": self.visual_calibration.target_kind if self.visual_calibration else "",
                "title_matched": bool(result), "candidate_count": len(rows), "text_candidate_count": sum(row.safe for row in rows),
                "uncertain_count": sum(not row.safe for row in rows), "self_count": sum(row.direction == "self" for row in rows),
                "friend_count": sum(row.direction == "friend" for row in rows), "human_verified": human_verified,
                "snapshot_digest": snapshot_digest, "model_calls": 0, "wechat_inputs": 0, "wechat_sends": 0}
        path = self.store.data / "visual_runtime_status.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def visual_confirm(self):
        if not self.visual_last or not self.visual_last["rows"] or any(not row.safe for row in self.visual_last["rows"]):
            return
        self.visual.stop()
        if self.visual_review.active:
            self.visual_review.pause("整轮只读核对已结束；第二阶段需重新只读检测，逐条确认。")
            self.visual_refresh_review()
        self.visual_close_locations()
        self.visual_map_button.config(state="disabled")
        self.visual_confirm_button.config(state="disabled")
        self.visual_report("human_verified", True)
        self.visual_status.set("你已核对整轮识别；不会上传或发送。请用“文字核对 / 草稿”逐条核对并指定文字；人工快照不需建立基线。")

    def visual_open_review(self):
        if self.visual_draft_window:
            self.visual_draft_window.window.lift()
            return
        if self.preview_busy or self.visual.running or self.engine.running or self.visual_job_busy or (self.engine.thread and self.engine.thread.is_alive()):
            return messagebox.showinfo("先结束当前任务", "请先停止持续识别或原连接器，并等待正在进行的模型请求结束。", parent=self.root)
        if not self.visual_calibration:
            return messagebox.showinfo("先校准指定单聊", "请先校准唯一的联系人标题与消息区。", parent=self.root)
        try:
            self.visual_review.begin(self.visual_calibration, self.store.selected(), self.store.config())
        except VisualPause as error:
            return messagebox.showinfo("未开始核对", str(error), parent=self.root)
        self.engine.stop("人工核对草稿模式，原连接器保持暂停。")
        self.invalidate_visual_draft()
        self.view_mode.set("视觉只读")
        self.render_history()
        from visual_draft_ui import DraftReviewWindow
        self.visual_draft_window = DraftReviewWindow(self)
        self.visual_draft_report()

    def visual_end_review(self):
        if self.visual_draft_window:
            self.visual_draft_window.close()
        else:
            self.visual_review.close()

    def visual_refresh_review(self):
        if self.visual_draft_window:
            self.visual_draft_window.refresh()
        self.visual_draft_report()

    def visual_draft_report(self):
        # Aggregate pipeline counters, not a claim that a model HTTP call or
        # real WeChat synchronization succeeded. No identities or chat text.
        session = self.visual_review
        data = {"stage": 2, "active": session.active, "snapshot_ready": session.ready,
                "needs_baseline": session.needs_baseline, "observed_count": len(session.rows),
                "pixel_continuity_enabled": session.pixel_tracking,
                "review_mode": "manual_snapshot" if session.manual_mode else "pixel_baseline",
                "manual_target_selected": bool(session.manual_target),
                "baseline_lost": bool(session.rebaseline_reason),
                "alignment": safe_alignment_diagnostic(session.sync_diagnostic),
                "reviewed_count": len(session.reviewed),
                "pending_new_count": 0 if session.manual_mode else len(session.pending()),
                "manual_pending_count": len(session.pending()) if session.manual_mode else 0,
                "draft_present": bool(session.draft), "job_in_flight": self.visual_job_busy,
                **self.visual_draft_stats, "wechat_inputs": 0, "wechat_sends": 0}
        path = self.store.data / "visual_draft_status.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(path)


def main():
    if "--selftest-layout-ui" in sys.argv:
        from layout_ui_smoke_test import run_synthetic_ui
        try:
            report = run_synthetic_ui(App, APP_ROOT)
        except Exception as error:
            report = {"success": False, "error_type": type(error).__name__, "real_user_data_access": False}
        path = APP_ROOT / "artifacts" / "layout_ui_selftest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        raise SystemExit(0 if report["success"] else 1)
    if "--selftest-visual-runtime" in sys.argv:
        # Reproduce the production worker's native initialization and offline
        # title/message OCR without
        # opening Tk, Store, authorization, a screen capture or WeChat.
        from runtime_probe import run_runtime_probe
        report = run_runtime_probe(APP_ROOT / "artifacts" / "visual_runtime_selftest.json")
        raise SystemExit(0 if report["success"] else 1)
    if "--selftest-title" in sys.argv:
        # Explicit diagnostic startup: no Tk, Store, login, WeChat or autostart.
        path = APP_ROOT / "artifacts" / "title_ocr_selftest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from title_smoke_test import run_synthetic
            report = run_synthetic()
        except Exception as error:
            report = {"success": False, "error_type": type(error).__name__, "user_images_read": 0,
                      "wechat_captures": 0, "model_calls": 0, "wechat_inputs": 0, "wechat_sends": 0}
        report["frozen"] = bool(getattr(sys, "frozen", False))
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return
    root = tk.Tk()
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", 18771))
    except OSError:
        root.withdraw()
        messagebox.showinfo("狗头军师", "聊天台已经打开，请切换到现有窗口。")
        root.destroy()
        return
    try:
        App(root)
        root.mainloop()
    except Exception:
        messagebox.showerror("启动失败", "聊天台未能启动。请从源目录运行 app.py 查看诊断；不会启用微信自动回复。")
        raise
    finally:
        guard.close()


if __name__ == "__main__":
    main()

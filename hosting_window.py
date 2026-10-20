"""Hosting lifecycle/display only; sending remains in hosting_run.

--selftest creates our own widgets with a fake runner and temporary ledger.
Import and selftest do not touch WeChat, clipboard or model services.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from app import C, FONT
from hosting_ledger import Ledger


@dataclass
class ChatEntry:
    role: str
    text: str
    status: str = ""


class HostingController:
    """No widgets or native adapters. Only the worker invokes the runner."""

    def __init__(self, *, config, select, calibration, ledger, runner, dpi):
        self.config, self.select = config, select
        self.calibration, self.ledger = calibration, ledger
        self.runner, self.dpi = runner, dpi
        self.thread = None
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.entries = deque(maxlen=1000)
        self.details = deque(maxlen=500)
        self.state = "未开始"
        self.note = "选择已标定联系人后开启；只处理启动后的新消息。"
        self.pending_reply = None
        self.active_contact = None
        self.stop_reason = ""
        self.revision = 0

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def system(self, text, state=None):
        if state:
            self.state = state
            if state in ("已暂停", "待人工确认"):
                self.stop_reason = text
        self.note = text
        self.entries.append(ChatEntry("system", text))
        self.revision += 1

    def blocking_description(self):
        finishes = {e.commit_id: e for e in self.ledger.read() if e.kind == "commit_finish"}
        rows = []
        for e in self.ledger.unfinished():
            phase = "UNKNOWN（提交后未确认）" if e.commit_id in finishes else "COMMIT（无完成记录）"
            rows.append(f"联系人：{e.target}\n时间：{e.at}\n正文：{e.text}\n阶段：{phase}\n编号：{e.commit_id}")
        return "\n\n".join(rows)

    def start(self, contact_id):
        if self.running:
            return False
        self.poll()
        try:
            cfg = self.config()
            profile = next((p for p in cfg["profiles"] if p["id"] == contact_id), None)
            if not profile:
                self.system("未选择联系人，拒绝启动。", "已暂停")
                return False
            if self.ledger.blocks_start():
                self.system("账本拦截：待人工确认，不会自动重发。\n" + self.blocking_description(), "待人工确认")
                return False
            calibration = self.calibration()
            if calibration["title"] != profile["name"]:
                self.system("所选联系人与标定目标不一致，拒绝启动；不切换或扫描其他会话。", "已暂停")
                return False
            self.select(contact_id)  # preserve ChatAI's selected-profile isolation
        except Exception as error:
            self.system("启动检查失败：" + type(error).__name__, "已暂停")
            return False
        self.stop_event = threading.Event()
        if self.active_contact != contact_id:
            self.entries.clear()
            self.details.clear()
            self.active_contact = contact_id
        self.pending_reply = None
        self.stop_reason = ""
        self.system("正在校验实际会话；校验通过前不会输入或发送。", "未开始")
        self.thread = threading.Thread(target=self._work, args=(calibration, self.stop_event),
                                       name="goutou-hosting", daemon=False)
        self.thread.start()
        return True

    def _work(self, calibration, stop_event):
        try:
            self.dpi()
            code = self.runner(calibration, self.ledger, live=True, stage_only=False,
                               dry_run=False, offline_reply=False, seconds=24 * 3600,
                               log=self.log, stop_event=stop_event)
            if code:
                self.events.put({"kind": "system", "text": f"循环拒绝运行（返回码 {code}）", "state": "已暂停"})
        except Exception as error:
            self.events.put({"kind": "system", "text": "循环异常停止：" + type(error).__name__, "state": "已暂停"})
        finally:
            self.events.put({"kind": "finished"})

    def log(self, message, **_kwargs):
        # run_hosting uses print-style log(..., flush=True).
        if message.startswith("@hosting "):
            try:
                self.events.put(json.loads(message[len("@hosting "):]))
            except (ValueError, TypeError):
                self.events.put({"kind": "system", "text": "事件格式异常，已请求停止", "state": "已暂停"})
                self.stop_event.set()
        else:
            self.events.put({"kind": "detail", "text": message})

    def stop(self):
        self.stop_event.set()
        if self.running:
            self.system("正在停止并等待当前操作收尾；完成前请勿操作微信。")

    def reap(self, timeout=0):
        if self.thread:
            self.thread.join(timeout)
        return not self.running

    def request_close(self, confirm):
        if not confirm():
            return False
        self.stop()
        return True

    def acknowledge(self):
        if self.running:
            return False
        for entry in self.ledger.unfinished():
            self.ledger.acknowledge(entry.commit_id, note="用户在托管窗口明确确认；禁止补发")
        self.system("人工确认已记录；未补发。需重新开启，只处理新基线之后的来信。", "已暂停")
        return True

    def poll(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind, text = event.get("kind"), event.get("text", "")
            if kind == "detail":
                self.details.append(text)
                continue
            if kind == "incoming":
                self.entries.append(ChatEntry("incoming", text))
            elif kind in ("reply", "outgoing"):
                entry = ChatEntry("outgoing", text, "待提交，尚未发送" if kind == "reply" else "人工消息")
                self.entries.append(entry)
                if kind == "reply":
                    self.pending_reply = entry
            elif kind == "verification":
                delivered = event.get("delivered") is True
                if self.pending_reply:
                    self.pending_reply.status = "已核验本机气泡" if delivered else "结果不确定，禁止重发"
                    self.pending_reply = None
                self.system("已核验本次新增己方气泡（不代表对方已读）" if delivered else "UNKNOWN：待人工确认，禁止重发",
                            None if delivered else "待人工确认")
                trace = event.get("trace") or {}
                if not delivered and trace:
                    # The structural check is not yet proven live; show which
                    # signal never moved so the cause is not guessed at.
                    self.system(f"核验数据：输入框草稿宽度 回车前 {trace.get('draft_before')}px / 回车后最少 {trace.get('draft_left_min')}px"
                                f"（需 ≤ {trace.get('max_draft_left')} 倍），"
                                f"聊天区变化峰值 {trace.get('peak_message')}（阈值 {trace.get('threshold')}），"
                                f"等待 {trace.get('elapsed')}s。请先在微信里确认这条是否已发出。")
            elif kind == "system":
                self.system(text, event.get("state"))
            # finished is notification only: wait for actual adapter cleanup.
            self.revision += 1
        if self.thread and self.reap():
            self.thread = None
            if self.pending_reply:
                self.pending_reply.status = "本轮停止；未核验发送，勿自动补发"
                self.pending_reply = None
            try:
                blocked = self.ledger.blocks_start()
            except Exception:
                blocked = True
            summary = "已停止，之后由你自己接手。" if not blocked else "已停止；账本冻结，待人工确认。"
            if self.stop_reason:
                summary = self.stop_reason + "\n" + summary
            self.system(summary,
                        "待人工确认" if blocked else "已暂停")


class HostingWindow:
    def __init__(self, root, controller, *, confirm=None, ask=None, save_config=None, read_chat=None):
        self.root, self.controller = root, controller
        self.read_chat = read_chat  # chat_reader.read_visible_chat; None = paste only
        self.ask = ask              # advisor model call; None = not configured
        self.save_config = save_config  # Store.save; None = contacts are read-only here
        self.confirm = confirm or (lambda: messagebox.askokcancel(
            "停止托管并关闭", "确认关闭后停止托管，由你自己接手。\n若正在提交，会先完成核验或保留 UNKNOWN 冻结。确定关闭吗？", parent=root))
        self.closing = False
        self.stop_started = None
        self.rendered = -1
        self.after_id = None
        root.title("狗头军师 · 托管")
        width = min(1180, root.winfo_screenwidth() - 80)
        height = min(800, root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}")
        root.minsize(min(980, width), min(690, height))
        root.configure(bg=C["bg"])
        root.option_add("*Font", (FONT, 10))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self._pump()

    def label(self, parent, text, size=10, color=None, bg=None, **kwargs):
        return tk.Label(parent, text=text, font=(FONT, size), fg=color or C["ink"],
                        bg=bg or C["panel"], **kwargs)

    def action(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, bg=C["green"] if primary else C["panel"],
                         fg="white" if primary else C["ink"], activebackground=C["soft"],
                         relief="flat", bd=0, padx=16, pady=10, cursor="hand2")

    def _build(self):
        # Same sidebar/main-panel structure and visual tokens as the App.
        side = self.sidebar = tk.Frame(self.root, bg=C["panel"], width=255, padx=22, pady=26)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.label(side, "GOUTOU  /  HOST", 10, C["green"]).pack(anchor="w")
        self.label(side, "狗头军师", 24).pack(anchor="w", pady=(10, 0))
        # Wrap to the fixed sidebar width (255 - 2 x 22 padding): unwrapped,
        # these were clipped on both sides (user report 16:3x).
        wrap = dict(wraplength=205, justify="left")
        self.label(side, "托管收发，随时交还给你。", 10, C["muted"], **wrap).pack(anchor="w", pady=(6, 12))
        self.label(side, "指定联系人", 11).pack(anchor="w")
        self.label(side, "一次只托管一位，资料各自独立", 9, C["muted"], **wrap).pack(anchor="w", pady=(4, 8))
        self.contact = self.contact_list = tk.Listbox(
            side, relief="flat", bd=0, bg=C["bg"], fg=C["ink"], selectbackground=C["green"],
            selectforeground="white", activestyle="none", height=5, exportselection=False,
            font=(FONT, 11), highlightthickness=0)
        self.contact.pack(fill="x")
        self.contact.bind("<<ListboxSelect>>", self.select_contact)
        self.refresh_contacts()
        # Two buttons per row: one per row pushed 确认冻结记录 below the bottom
        # edge at the minimum window height once add/edit contact were added.
        row = tk.Frame(side, bg=C["panel"])
        row.pack(fill="x", pady=(10, 0))
        self.add_button = self.action(row, "+ 添加", lambda: self.profile_dialog())
        self.add_button.pack(side="left", fill="x", expand=True)
        self.edit_button = self.action(row, "编辑资料", self.edit_profile)
        self.edit_button.pack(side="left", fill="x", expand=True)
        tk.Frame(side, bg=C["line"], height=1).pack(fill="x", pady=12)
        row = tk.Frame(side, bg=C["panel"])
        row.pack(fill="x")
        self.action(row, "冻结记录…", self.resolve).pack(side="left", fill="x", expand=True)
        self.action(row, "风格卡", lambda: messagebox.showinfo(
            "风格卡（预留）", "尚未开放。本轮不会推断或生成风格卡。", parent=self.root)).pack(side="left", fill="x", expand=True)
        for button in row.winfo_children():
            button.config(padx=6)
        side_foot = tk.Frame(side, bg=C["panel"])
        side_foot.pack(side="bottom", fill="x")
        self.label(side_foot, "停止后，由你自己接手", 9, C["green"], **wrap).pack(anchor="w")

        main = self.main_panel = tk.Frame(self.root, bg=C["bg"], padx=28, pady=23)
        main.pack(side="left", expand=True, fill="both")
        head = tk.Frame(main, bg=C["bg"])
        head.pack(fill="x")
        self.tabs = {}
        for key, title in (("hosting", "托管"), ("advisor", "军师"), ("rhythm", "聊天节奏")):
            tab = tk.Label(head, text=title, bg=C["bg"], font=(FONT, 17), padx=4, cursor="hand2")
            tab.pack(side="left", padx=(0, 18))
            tab.bind("<Button-1>", lambda _e, k=key: self.show_view(k))
            self.tabs[key] = tab
        self.lamp = tk.Label(head, text="● 未开始", bg=C["bg"], fg=C["muted"], font=(FONT, 11, "bold"))
        self.lamp.pack(side="right")

        # The hosting view is the original workbench, unchanged inside its frame.
        hosting = self.hosting_view = tk.Frame(main, bg=C["bg"])
        self.banner = self.label(hosting, "", 10, C["amber"], C["sand"],
                                 anchor="w", justify="left", wraplength=750, padx=14, pady=13)
        self.banner.pack(fill="x", pady=(18, 15))

        # Reserve controls FIRST, preserving the user's footer clipping fix.
        foot = tk.Frame(hosting, bg=C["bg"], pady=14)
        foot.pack(fill="x", side="bottom")
        self.button = self.action(foot, "开启托管", self.toggle, primary=True)
        self.button.pack(side="right", padx=(16, 0))
        self.note = self.label(foot, "", 9, C["muted"], C["bg"], justify="left", wraplength=500)
        self.note.pack(side="left", fill="x", expand=True)

        panel = self.chat_panel = tk.Frame(hosting, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        panel.pack(fill="both", expand=True)
        chat_header = tk.Frame(panel, bg=C["panel"], padx=18, pady=14)
        chat_header.pack(fill="x")
        self.contact_title = self.label(chat_header, "请选择联系人", 14)
        self.contact_title.pack(side="left")
        self.scope_label = self.label(chat_header, "实际托管记录 · 不是练习", 9, C["muted"])
        self.scope_label.pack(side="right")
        tk.Frame(panel, bg=C["line"], height=1).pack(fill="x")
        transcript = tk.Frame(panel, bg=C["panel"])
        transcript.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(transcript)
        scroll.pack(side="right", fill="y")
        self.history = tk.Text(transcript, bg=C["panel"], fg=C["ink"], wrap="word", state="disabled",
                               relief="flat", bd=0, height=1, width=1, cursor="arrow", font=(FONT, 12),
                               padx=22, pady=20, yscrollcommand=scroll.set)
        self.history.pack(fill="both", expand=True)
        scroll.config(command=self.history.yview)
        self.history.tag_configure("incoming", justify="left", lmargin1=8, lmargin2=8, rmargin=160, spacing1=12, spacing3=8)
        self.history.tag_configure("outgoing", justify="right", lmargin1=160, lmargin2=160, rmargin=8,
                                   foreground=C["green"], spacing1=12, spacing3=8)
        self.history.tag_configure("system", justify="center", foreground=C["muted"], spacing1=8, spacing3=8)

        from advisor_ui import AdvisorPanel
        from rhythm_ui import RhythmPanel
        self.advisor = AdvisorPanel(main, self.root, config=self.controller.config,
                                    entries=lambda: list(self.controller.entries),
                                    running=lambda: self.controller.running, ask=self.ask,
                                    read_chat=self.read_chat, save_config=self.save_config)
        self.rhythm = RhythmPanel(main, self.root)
        self.views = {"hosting": hosting, "advisor": self.advisor.frame, "rhythm": self.rhythm.frame}
        self.view = None
        self.show_view("hosting")
        self.select_contact()

    def show_view(self, key):
        if key == self.view:
            return
        for name, frame in self.views.items():
            if name != key:
                frame.pack_forget()
        self.views[key].pack(fill="both", expand=True)
        for name, tab in self.tabs.items():
            tab.config(fg=C["ink"] if name == key else C["muted"],
                       font=(FONT, 17, "bold") if name == key else (FONT, 17))
        if key == "advisor":
            self.advisor.refresh_background()
        self.view = key

    def refresh_contacts(self, select_id=None):
        cfg = self.controller.config()
        self.profiles = cfg["profiles"]
        self.contact.delete(0, "end")
        for profile in self.profiles:
            self.contact.insert("end", profile["name"])
        wanted = select_id or cfg["selected"]
        selected = next((i for i, p in enumerate(self.profiles) if p["id"] == wanted), -1)
        if selected >= 0:
            self.contact.selection_set(selected)
            self.contact.activate(selected)

    def edit_profile(self):
        profile = self.selected_profile()
        if not profile:
            return messagebox.showinfo("请先选择联系人", "在上面的列表里点一位联系人，再编辑资料。", parent=self.root)
        self.profile_dialog(profile)

    def profile_dialog(self, existing=None):
        """Add or edit a contact (same fields and Store.save checks as the 聊天台)."""
        if self.save_config is None:
            messagebox.showinfo("联系人", "这个窗口没有接入资料保存；请在聊天台修改联系人。", parent=self.root)
            return None
        if self.controller.running:
            messagebox.showinfo("请先停止托管", "托管运行中不改联系人资料。", parent=self.root)
            return None
        from chat_core import new_profile
        values = dict(existing) if existing else new_profile("")
        dialog = tk.Toplevel(self.root)
        dialog.title("编辑联系人资料" if existing else "添加联系人")
        dialog.configure(bg=C["panel"])
        dialog.transient(self.root)
        dialog.resizable(False, False)
        form = tk.Frame(dialog, bg=C["panel"], padx=26, pady=20)
        form.pack(fill="both", expand=True)
        self.label(form, "只填写你明确知道的事实", 15).pack(anchor="w")
        self.label(form, "未知可留空。只有已标定的联系人能开启托管；其他联系人可在“军师”里分析。",
                   9, C["muted"], wraplength=440, justify="left").pack(anchor="w", pady=(6, 12))
        fields = {}
        for name, label in (("name", "微信备注名（与微信显示完全一致）"), ("stage", "当前关系（如：刚认识 / 暧昧 / 在一起）"),
                            ("goal", "聊天目标（如：自然聊天，互相了解）")):
            self.label(form, label).pack(anchor="w", pady=(8, 4))
            var = tk.StringVar(value=values.get(name, ""))
            entry = tk.Entry(form, textvariable=var, relief="solid", bd=1, width=52,
                             state="readonly" if existing and name == "name" else "normal")
            entry.pack(fill="x", ipady=5)
            fields[name] = var
        self.label(form, "关系背景 / 对方明确分享过的兴趣（可留空）").pack(anchor="w", pady=(10, 4))
        context = tk.Text(form, height=5, width=52, wrap="word", relief="solid", bd=1)
        context.pack(fill="x")
        context.insert("1.0", values.get("context", ""))
        traits = list(values.get("traits", []))
        portrait = None
        if existing:
            self.label(form, "对象画像（军师整理、你勾选过的；聊天里看得到的特点）").pack(anchor="w", pady=(12, 4))
            box = tk.Frame(form, bg=C["panel"])
            box.pack(fill="x")
            portrait = tk.Listbox(box, height=5, width=52, relief="solid", bd=1, activestyle="none",
                                  exportselection=False, font=(FONT, 10))
            portrait.pack(side="left", fill="x", expand=True)

            def show_traits():
                portrait.delete(0, "end")
                for t in traits:
                    portrait.insert("end", (f"[{t.get('kind')}] " if t.get("kind") else "") + t["text"]
                                    + (f" —— “{t['evidence']}”" if t.get("evidence") else ""))
                if not traits:
                    portrait.insert("end", "（还没有：在“军师”里点“总结对象画像”）")

            def delete_trait():
                picked = portrait.curselection()
                if picked and traits and picked[0] < len(traits):
                    del traits[picked[0]]
                    show_traits()
            show_traits()
            self.action(box, "删除选中", delete_trait).pack(side="left", padx=(8, 0))

        def save():
            profile = dict(values)
            profile.update({k: v.get().strip() for k, v in fields.items()})
            profile["context"] = context.get("1.0", "end").strip()
            if existing:
                profile["traits"] = traits
            cfg = self.controller.config()
            cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != profile["id"]] + [profile]
            cfg["selected"] = profile["id"]
            try:
                if self.controller.running:
                    raise ValueError("托管运行中不改联系人资料。")
                self.save_config(cfg)
            except ValueError as error:
                return messagebox.showerror("请检查资料", str(error), parent=dialog)
            self.refresh_contacts(profile["id"])
            self.select_contact()
            self.advisor.refresh_background()
            dialog.destroy()

        self.action(form, "保存并选中", save, primary=True).pack(fill="x", pady=(16, 0))
        self.dialog_window = dialog
        dialog.grab_set()
        return dialog

    def selected_profile(self):
        selection = self.contact.curselection()
        return self.profiles[selection[0]] if selection else None

    def select_contact(self, _event=None):
        profile = self.selected_profile()
        name = profile["name"] if profile else "请选择联系人"
        previous = next((p["name"] for p in self.profiles if p["id"] == self.controller.active_contact), None)
        # Browsing must not relabel an existing transcript as another contact.
        if previous and profile and self.controller.active_contact != profile["id"]:
            self.contact_title.config(text=previous + " · 上一轮记录")
        else:
            self.contact_title.config(text=name)

    def toggle(self):
        if self.controller.running:
            self.controller.stop()
            self.stop_started = time.monotonic()
        else:
            profile = self.selected_profile()
            self.controller.start(profile["id"] if profile else "")
            self.select_contact()

    def resolve(self):
        if self.controller.running:
            return messagebox.showinfo("请先停止", "线程收尾完成后才能人工确认账本。", parent=self.root)
        try:
            description = self.controller.blocking_description()
            if not description:
                return messagebox.showinfo("账本", "没有待确认记录。", parent=self.root)
            if messagebox.askyesno("人工确认，不重发", description +
                                  "\n\n请先自行核实实际发送情况。确认解除冻结？\n这不会补发；之后需手动开启并重建基线。", parent=self.root):
                self.controller.acknowledge()
        except Exception as error:
            self.controller.system("账本处理失败：" + type(error).__name__, "待人工确认")

    def on_close(self):
        if self.closing:
            return
        if self.controller.request_close(self.confirm):
            self.closing = True
            self.stop_started = time.monotonic()

    def _pump(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.controller.poll()
        running = self.controller.running
        if self.closing and not running:
            if self.after_id:
                self.root.after_cancel(self.after_id)
            self.root.destroy()
            return
        colors = {"未开始": C["muted"], "托管中": C["green"], "已暂停": C["amber"], "待人工确认": C["amber"]}
        self.lamp.config(text="● " + self.controller.state, fg=colors[self.controller.state])
        stopping = running and self.controller.stop_event.is_set()
        self.button.config(text="正在停止…" if stopping else "停止托管" if running else "开启托管",
                           state="disabled" if stopping or self.closing else "normal")
        self.contact.config(state="disabled" if running or self.closing else "normal")
        for button in (self.add_button, self.edit_button):
            button.config(state="disabled" if running or self.closing else "normal")
        note = self.controller.note
        if stopping and self.stop_started and time.monotonic() - self.stop_started > 10:
            note = "停止等待超过10秒：保留窗口直到线程收尾；不会强杀或自动重试。"
        self.note.config(text=note)
        self.banner.config(text=note, bg=C["soft"] if self.controller.state == "托管中" else C["sand"],
                           fg=C["green"] if self.controller.state == "托管中" else C["amber"])
        if self.rendered != self.controller.revision:
            self.history.config(state="normal")
            self.history.delete("1.0", "end")
            labels = {"incoming": "对方", "outgoing": "我", "system": "—"}
            if not self.controller.entries:
                self.history.insert("end", "选择左侧联系人，点击“开启托管”。\n这里显示本轮来信、回复与核验状态；启动前不会自动读取微信。\n", "system")
            for entry in self.controller.entries:
                text = labels[entry.role] + "  " + entry.text
                if entry.status:
                    text += "\n    — " + entry.status
                self.history.insert("end", text + "\n", entry.role)
            self.history.see("end")
            self.history.config(state="disabled")
            self.rendered = self.controller.revision
        self.after_id = self.root.after(100, self._pump)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            # Even an unexpected Tk/mainloop exception must not orphan a
            # live worker. Normal close uses the responsive after() poll.
            self.controller.stop()
            self.controller.reap(timeout=10)
            if self.controller.running:
                self.controller.reap(timeout=None)


def create_controller(store):
    import hosting_run

    def select(contact_id):
        cfg = store.config()
        if cfg["selected"] != contact_id:
            cfg["selected"] = contact_id
            store.save(cfg)

    return HostingController(config=store.config, select=select, calibration=hosting_run.load_calibration,
                             ledger=Ledger(hosting_run.LEDGER), runner=hosting_run.run_hosting,
                             dpi=hosting_run.enable_pmv2)


def selftest():
    """Six machine-readable lines; real widgets, fake loop, temporary data."""
    results = dict(window_created=False, ledger_check="blocked", start_refused_without_contact=False,
                   stop_event_set=False, thread_reaped=False, close_confirm_cancels=False)
    root = None
    controller = None
    try:
        with tempfile.TemporaryDirectory(prefix="hosting-selftest-", dir=Path(__file__).resolve().parent) as folder:
            cfg = {"profiles": [{"id": "synthetic", "name": "离线夹具"}], "selected": ""}
            ready = threading.Event()

            def fake_runner(*_, log, stop_event, **__):
                log('@hosting {"kind":"system","text":"离线夹具就绪","state":"托管中"}', flush=True)
                ready.set()
                stop_event.wait(3)
                return 0

            controller = HostingController(config=lambda: cfg, select=lambda i: cfg.update(selected=i),
                                           calibration=lambda: {"title": "离线夹具"},
                                           ledger=Ledger(str(Path(folder) / "ledger.jsonl")),
                                           runner=fake_runner, dpi=lambda: None)
            root = tk.Tk()
            root.withdraw()
            window = HostingWindow(root, controller, confirm=lambda: False)
            root.update_idletasks()
            results["window_created"] = bool(root.winfo_exists() and window.history.winfo_exists())
            results["start_refused_without_contact"] = not controller.start("") and controller.thread is None
            blocked = controller.ledger.begin_commit("离线夹具", "仅合成文本")
            ledger_refused = not controller.start("synthetic") and controller.thread is None
            controller.ledger.acknowledge(blocked, note="selftest only")
            results["ledger_check"] = "ok" if ledger_refused and not controller.ledger.blocks_start() else "blocked"
            controller.start("synthetic")
            if not ready.wait(2):
                raise RuntimeError("fake_runner_not_started")
            window.on_close()
            results["close_confirm_cancels"] = (not window.closing and controller.running
                                                 and not controller.stop_event.is_set() and bool(root.winfo_exists()))
            window.toggle()
            results["stop_event_set"] = controller.stop_event.is_set()
            results["thread_reaped"] = controller.reap(2)
            controller.poll()
    finally:
        if controller:
            controller.stop()
            controller.reap(4)
        if root:
            if 'window' in locals() and window.after_id:
                root.after_cancel(window.after_id)
            root.destroy()
        for key, value in results.items():
            print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
    return 0 if all(value is True or value == "ok" for value in results.values()) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="offline widgets + fake runner only")
    args = parser.parse_args(argv)
    from hosting_run import enable_pmv2
    enable_pmv2()  # before Tk creates any HWND; no WeChat access
    if args.selftest:
        return selftest()
    # Window-level single instance only: no server, no public port, no runner.
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", 18772))
    except OSError:
        guard.close()
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("托管窗口已打开", "请切换到现有「狗头军师 · 托管」窗口。", parent=root)
        root.destroy()
        return 1
    from chat_core import ChatAI, Store
    from chat_reader import read_visible_chat
    store = None
    try:
        store = Store()
        HostingWindow(tk.Tk(), create_controller(store), ask=ChatAI(store).ask_json,
                      save_config=store.save, read_chat=read_visible_chat).run()
    finally:
        if store:
            store.db.close()
        guard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

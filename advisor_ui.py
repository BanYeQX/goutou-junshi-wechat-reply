"""军师 panel inside the hosting window. Display and copy only.

It never types into WeChat and never sends. Copy is refused while hosting is
running: staging uses the clipboard too, and the user is meant to take over
first. Model calls run on a worker thread; results come back through a queue
and are dropped if the input changed meanwhile.
"""
from __future__ import annotations

from dataclasses import replace
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import advisor
from app import C, FONT

HINT = "把对方说的话粘到这里，一行一条。\n以“我：”开头的行算你说的，其余都算对方说的。"


class AdvisorPanel:
    def __init__(self, parent, root, *, config, entries, running, ask=None, read_chat=None,
                 save_config=None):
        self.root = root
        self.config, self.entries, self.running = config, entries, running
        self.ask = ask
        self.read_chat = read_chat   # chat_reader.read_visible_chat; None = not available
        self.save_config = save_config  # Store.save; None = portrait cannot be kept
        self.results = queue.Queue()
        self.request = 0
        self.turns = []
        self.analysis = None
        self.busy = False
        self.pending_kind = None
        self.after_id = None
        self.frame = tk.Frame(parent, bg=C["bg"])
        self.frame.bind("<Destroy>", self._destroyed)
        self._build()
        self.refresh_background()
        self._poll()

    # ------------------------------------------------------------------ layout

    def label(self, parent, text, size=10, color=None, bg=None, **kwargs):
        return tk.Label(parent, text=text, font=(FONT, size), fg=color or C["ink"],
                        bg=bg or C["panel"], **kwargs)

    def action(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, bg=C["green"] if primary else C["panel"],
                         fg="white" if primary else C["ink"], activebackground=C["soft"],
                         relief="flat", bd=0, padx=14, pady=8, cursor="hand2")

    def _build(self):
        top = tk.Frame(self.frame, bg=C["bg"])
        top.pack(fill="x", pady=(14, 8))
        self.background = self.label(top, "", 10, C["muted"], C["bg"], anchor="w", justify="left")
        self.background.pack(side="left", fill="x", expand=True)
        # Wraps with the window: a long goal note used to run under a right-hand hint.
        self.background.bind("<Configure>", lambda e: self.background.config(wraplength=max(e.width - 4, 200)))

        box = tk.Frame(self.frame, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        box.pack(fill="x")
        self.input = tk.Text(box, height=4, wrap="word", relief="flat", bd=0, bg=C["panel"],
                             fg=C["ink"], font=(FONT, 11), padx=14, pady=10, undo=True)
        self.input.pack(fill="x")
        self.input.insert("1.0", HINT)
        self.input.config(fg=C["muted"])
        self.placeholder = True
        self.input.bind("<FocusIn>", self._clear_hint)
        self.input.bind("<<Modified>>", self._edited)
        # Tk text has no right-click menu of its own. Every copy goes through
        # copy(), which refuses while hosting runs (staging uses the clipboard).
        self.input_menu = self._popup(self.input, [("剪切", lambda: self.copy_input(cut=True)),
                                                   ("复制", self.copy_input), ("粘贴", self.paste_input),
                                                   ("全选", lambda: self.select_all(self.input))])
        self.input.bind("<<Copy>>", lambda _e: self.copy_input())
        self.input.bind("<<Cut>>", lambda _e: self.copy_input(cut=True))
        self.input.bind("<Control-a>", lambda _e: self.select_all(self.input))

        controls = tk.Frame(self.frame, bg=C["bg"])
        controls.pack(fill="x", pady=8)
        self.analyze_button = self.action(controls, "分析并给回复", self.start_analysis, primary=True)
        self.analyze_button.pack(side="right")
        self.portrait_button = self.action(controls, "总结画像", self.start_portrait)
        self.portrait_button.pack(side="right", padx=(0, 8))
        self.read_button = self.action(controls, "从微信读取", self.start_read)
        self.read_button.pack(side="left")
        self.action(controls, "带入托管", self.fill_from_hosting).pack(side="left", padx=(8, 0))
        self.action(controls, "清空", self.clear).pack(side="left", padx=8)
        self.status = self.label(self.frame, "", 9, C["muted"], C["bg"], anchor="w", justify="left")
        self.status.pack(fill="x", pady=(0, 6))

        # Analysis and candidate cards share ONE scrolling text: at the minimum
        # window size a separate card strip squeezed the analysis to nothing
        # and still clipped the third card. Cards are embedded windows.
        body = tk.Frame(self.frame, bg=C["panel"], highlightbackground=C["line"], highlightthickness=1)
        body.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(body)
        scroll.pack(side="right", fill="y")
        self.output = tk.Text(body, wrap="word", state="disabled", relief="flat", bd=0, height=8,
                              bg=C["panel"], fg=C["ink"], font=(FONT, 11), padx=16, pady=12,
                              cursor="xterm", yscrollcommand=scroll.set)
        self.output.pack(fill="both", expand=True)
        scroll.config(command=self.output.yview)
        self.output_menu = self._popup(self.output, [("复制", lambda: self.copy_output(all_if_empty=True)),
                                                     ("全选", lambda: self.select_all(self.output))])
        self.output.bind("<<Copy>>", lambda _e: self.copy_output())
        self.output.bind("<Control-a>", lambda _e: self.select_all(self.output))
        self.output.tag_configure("head", foreground=C["green"], font=(FONT, 11, "bold"), spacing1=8)
        self.output.tag_configure("muted", foreground=C["muted"], font=(FONT, 9))
        self.output.tag_configure("warn", foreground=C["amber"], font=(FONT, 11, "bold"))
        self.card_widgets = []
        self.render(None)

    # ------------------------------------------------------------------ menus

    def _popup(self, widgets, entries):
        widgets = widgets if isinstance(widgets, (list, tuple)) else [widgets]
        menu = tk.Menu(widgets[0], tearoff=0)
        for label, command in entries:
            menu.add_command(label=label, command=command)

        def show(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        for widget in widgets:
            widget.bind("<Button-3>", show)
        return menu

    @staticmethod
    def _selected(widget):
        try:
            return widget.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def select_all(self, widget):
        widget.tag_add("sel", "1.0", "end-1c")
        widget.focus_set()
        return "break"

    def copy_input(self, cut=False):
        text = "" if self.placeholder else self._selected(self.input)
        if text and self.copy(text) and cut:
            self.input.delete("sel.first", "sel.last")
        return "break"

    def paste_input(self):
        self._clear_hint()
        self.input.focus_set()
        self.input.event_generate("<<Paste>>")

    def copy_output(self, all_if_empty=False):
        text = self._selected(self.output)
        if not text and all_if_empty:
            # The candidate cards are embedded widgets, invisible to Text.get():
            # copying "everything" used to drop exactly the replies.
            text = self.output.get("1.0", "end-1c")
            if self.analysis and self.analysis.candidates:
                text += "\n" + "\n".join(
                    f"{i}. {c.text}" + (f"（为什么：{c.why}；代价：{c.cost}）" if c.why or c.cost else "")
                    for i, c in enumerate(self.analysis.candidates, 1))
        if text.strip():
            self.copy(text.strip())
        return "break"

    # ------------------------------------------------------------------ input

    def _clear_hint(self, _event=None):
        if self.placeholder:
            self.input.delete("1.0", "end")
            self.input.config(fg=C["ink"])
            self.placeholder = False

    def _edited(self, _event=None):
        if self.input.edit_modified():
            self.input.edit_modified(False)
            if not self.placeholder and self.analysis is not None:
                self.status.config(text="输入已改动：下面的建议对应的是上一次的内容")

    def text(self):
        return "" if self.placeholder else self.input.get("1.0", "end").strip()

    def set_text(self, text):
        self._clear_hint()
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)

    def clear(self):
        self.request += 1          # anything in flight is now stale
        self.busy = False
        self.set_text("")
        self.analysis = None
        self.render(None)
        self.status.config(text="")
        self.analyze_button.config(state="normal")

    def fill_from_hosting(self):
        turns = advisor.turns_from_entries(list(self.entries()))
        if not turns:
            return self.status.config(text="本轮托管还没有文字记录；直接粘贴对方的话即可")
        self.set_text(advisor.format_transcript(turns))
        self.status.config(text=f"已带入 {len(turns)} 条托管记录，可修改后再分析")

    def refresh_background(self):
        cfg = self.config() or {}
        profile = next((p for p in cfg.get("profiles", []) if p.get("id") == cfg.get("selected")), None)
        if not profile:
            text = "当前对象：未选择"
        else:
            parts = [profile.get("name", ""), profile.get("stage", "") or "阶段未填",
                     profile.get("goal", "") or "目标未填"]
            text = "当前对象：" + " · ".join(p for p in parts if p)
            if profile.get("traits"):
                text += f" · 画像 {len(profile['traits'])} 条"
        self.background.config(text=text + "（资料在左侧“编辑资料”改，口吻在聊天台改）")
        return profile, cfg

    # ------------------------------------------------------------------ model

    @staticmethod
    def _worker(results, request, job):
        """Runs off the Tk thread and holds NO Tk object: if the last reference
        to a widget were dropped here, Tcl would abort ("Tcl_AsyncDelete:
        async handler deleted by the wrong thread") - e.g. on closing the window
        mid-analysis. Only the queue, plain data and the model call."""
        try:
            results.put((request, "ok", job()))
        except Exception as error:   # shown to the user; never retried silently
            message = str(error) if isinstance(error, ValueError) else type(error).__name__
            results.put((request, "error", message))

    def _start(self, request, job):
        threading.Thread(target=AdvisorPanel._worker, args=(self.results, request, job),
                         daemon=True).start()

    def start_analysis(self):
        if self.busy:
            return
        profile, cfg = self.refresh_background()
        turns = advisor.parse_transcript(self.text())
        try:
            if not turns:
                raise ValueError("先粘贴对方说的话。")
            if not any(t.speaker == "other" for t in turns):
                raise ValueError("没有对方的消息：不加“我：”的行会算作对方说的。")
        except ValueError as error:
            return self.status.config(text=str(error))
        guarded = advisor.guard(turns)
        if guarded is None and self.ask is None:
            return self.status.config(text="模型未配置：请先在聊天台设置模型并允许发送上下文")
        self.turns = turns
        self.request += 1
        request = self.request
        self.busy = True
        self.pending_kind = "analysis"
        self.analyze_button.config(state="disabled")
        ask = self.ask
        if guarded:
            self.status.config(text="这条触发了安全规则：不调用模型")
            job = lambda: guarded
        else:
            self.status.config(text="正在分析…（会把这段对话和背景发给已配置的模型）")
            job = lambda: advisor.analyze(turns, profile, cfg, ask)
        self._start(request, job)

    def start_read(self):
        """One read-only capture of the chat open in WeChat -> the input box."""
        if self.busy:
            return
        if self.read_chat is None:
            return self.status.config(text="这个窗口没有接入微信读取；请直接粘贴")
        if self.running():
            return self.status.config(text="托管运行中不另外读取：用“带入托管”，或先停止托管")
        profile, _cfg = self.refresh_background()
        if not profile:
            return self.status.config(text="先在左侧选中联系人")
        self.request += 1
        request = self.request
        self.busy = True
        self.pending_kind = "read"
        self.analyze_button.config(state="disabled")
        self.read_button.config(state="disabled")
        self.status.config(text=f"正在读取微信里和「{profile['name']}」的聊天（只截屏和识别，不点击、不输入）…")
        read, name = self.read_chat, profile["name"]
        self._start(request, lambda: read(name))

    def start_portrait(self):
        """Ask the model which visible traits of the contact this chat shows."""
        if self.busy:
            return
        if self.ask is None:
            return self.status.config(text="模型未配置：请先在聊天台设置模型并允许发送上下文")
        if self.save_config is None:
            return self.status.config(text="这个窗口没有接入资料保存，无法记录画像")
        profile, _cfg = self.refresh_background()
        if not profile:
            return self.status.config(text="先在左侧选中联系人")
        turns = advisor.parse_transcript(self.text())
        if not any(t.speaker == "other" for t in turns):
            return self.status.config(text="先读取或粘贴一段有对方消息的对话")
        self.request += 1
        request = self.request
        self.busy = True
        self.pending_kind = ("portrait", profile["id"], turns)
        self.analyze_button.config(state="disabled")
        self.status.config(text=f"正在从这段对话整理「{profile['name']}」的画像…（只记聊天里看得到的）")
        ask = self.ask
        self._start(request, lambda: advisor.suggest_traits(turns, profile, ask))

    def portrait_dialog(self, traits, profile_id):
        """Every suggestion starts UNticked: nothing is kept unless chosen."""
        cfg = self.config() or {}
        profile = next((p for p in cfg.get("profiles", []) if p.get("id") == profile_id), None)
        if profile is None:
            self.status.config(text="联系人已不存在，画像未保存")
            return None
        dialog = tk.Toplevel(self.root)
        dialog.title("对象画像 · " + profile["name"])
        dialog.configure(bg=C["panel"])
        dialog.transient(self.root)
        form = tk.Frame(dialog, bg=C["panel"], padx=24, pady=18)
        form.pack(fill="both", expand=True)
        self.label(form, f"从这段对话里看到的「{profile['name']}」", 14).pack(anchor="w")
        self.label(form, "只勾选你认可的；没勾的不保存。保存后可在左侧“编辑资料”里查看和删除。",
                   9, C["muted"], wraplength=520, justify="left").pack(anchor="w", pady=(4, 12))
        chosen = []
        for trait in traits:
            var = tk.BooleanVar(value=False)
            row = tk.Frame(form, bg=C["panel"])
            row.pack(fill="x", pady=4)
            tk.Checkbutton(row, variable=var, bg=C["panel"], activebackground=C["panel"],
                           text=(f"[{trait.kind}] " if trait.kind else "") + trait.text,
                           font=(FONT, 11), anchor="w", justify="left", wraplength=500).pack(anchor="w")
            self.label(row, "依据：对方说“" + trait.evidence + "”", 9, C["muted"], anchor="w",
                       justify="left", wraplength=480).pack(anchor="w", padx=(26, 0))
            chosen.append((var, trait))

        def save():
            picked = [t for v, t in chosen if v.get()]
            if not picked:
                dialog.destroy()
                return self.status.config(text="没有勾选，画像未改动")
            if self.running():
                return messagebox.showinfo("请先停止托管", "托管运行中不改联系人资料。", parent=dialog)
            from datetime import date
            from chat_core import MAX_TRAITS
            fresh = self.config()
            target = next((p for p in fresh["profiles"] if p["id"] == profile_id), None)
            if target is None:
                dialog.destroy()
                return self.status.config(text="联系人已不存在，画像未保存")
            traits_now = list(target.get("traits", []))
            if len(traits_now) + len(picked) > MAX_TRAITS:
                return messagebox.showinfo("画像已满", f"最多保存 {MAX_TRAITS} 条。请先在“编辑资料”里删掉一些。",
                                           parent=dialog)
            today = date.today().isoformat()
            target["traits"] = traits_now + [t.stored(today) for t in picked]
            try:
                self.save_config(fresh)
            except ValueError as error:
                return messagebox.showerror("没有保存", str(error), parent=dialog)
            dialog.destroy()
            self.refresh_background()
            self.status.config(text=f"已把 {len(picked)} 条加入「{target['name']}」的画像；之后分析和托管回复都会参考")

        buttons = tk.Frame(form, bg=C["panel"])
        buttons.pack(fill="x", pady=(14, 0))
        self.action(buttons, "保存勾选", save, primary=True).pack(side="right")
        self.action(buttons, "取消", dialog.destroy).pack(side="right", padx=(0, 8))
        self.portrait_window = dialog
        self.portrait_choices = chosen
        return dialog

    def start_rewrite(self, index):
        if self.busy or not self.analysis or index >= len(self.analysis.candidates):
            return
        if self.ask is None:
            return self.status.config(text="模型未配置")
        profile, cfg = self.refresh_background()
        original = self.analysis.candidates[index]
        turns = self.turns
        self.request += 1
        request = self.request
        self.busy = True
        self.pending_kind = ("rewrite", index)
        self.analyze_button.config(state="disabled")
        self.status.config(text="正在改得更像你…")
        ask, text = self.ask, original.text
        self._start(request, lambda: advisor.rewrite(text, turns, profile, cfg, ask))

    def _poll(self):
        while True:
            try:
                request, kind, value = self.results.get_nowait()
            except queue.Empty:
                break
            if request != self.request:
                continue                       # stale: input was cleared or replaced
            self.busy = False
            self.analyze_button.config(state="normal")
            self.read_button.config(state="normal")
            if kind == "error":
                self.status.config(text=value)
                continue
            if isinstance(self.pending_kind, tuple) and self.pending_kind[0] == "portrait":
                if not value:
                    self.status.config(text="这段对话里没有能用原话证明的新特点（已记录的不重复）")
                else:
                    self.status.config(text=f"找到 {len(value)} 条，勾选你认可的再保存")
                    self.portrait_dialog(value, self.pending_kind[1])
            elif self.pending_kind == "read":
                self.set_text(advisor.format_transcript(value))
                self.status.config(text=f"已读取屏幕上可见的 {len(value)} 条（图片、表情不读）。"
                                        "请核对错字和“我 / 对方”，再点“分析并给回复”")
            elif self.pending_kind == "analysis":
                self.analysis = value
                self.render(value)
                self.status.config(text="仅供参考：判断把握是模型估计，不是对方真实想法的概率；发不发由你决定")
            else:
                _, index = self.pending_kind
                candidates = list(self.analysis.candidates)
                old = candidates[index]
                candidates[index] = advisor.Candidate(value, old.why, "已按你的口吻改写；" + old.cost)
                self.analysis = replace(self.analysis, candidates=tuple(candidates))
                self.render(self.analysis)
                self.status.config(text="已改写第 %d 条" % (index + 1))
        try:
            self.after_id = self.root.after(120, self._poll)
        except tk.TclError:
            self.after_id = None

    def _destroyed(self, event):
        if event.widget is self.frame and self.after_id:
            try:
                self.root.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    # ------------------------------------------------------------------ output

    def render(self, analysis):
        for widget in self.card_widgets:
            widget.destroy()
        self.output.config(state="normal")
        self.output.delete("1.0", "end")
        self.card_widgets = []
        if analysis is None:
            self.output.insert("end", "粘贴对方的话后点“分析并给回复”。\n", "muted")
            self.output.insert("end", "这里会显示：对方可能的意图（附模型估计的把握）、依据、已知 / 未知、"
                                      "军师建议和什么时候该停；下方给 3 条候选，可复制或改得更像你。\n", "muted")
            self.output.config(state="disabled")
            return
        if analysis.guarded:
            self.output.insert("end", "⚠ " + analysis.advice + "\n", "warn")
        self.output.insert("end", "对方可能的意图\n", "head")
        self.output.insert("end", analysis.intent + "\n")
        if analysis.confidence is not None:
            self.output.insert("end", "判断把握 · %d%%（模型估计）\n" % round(analysis.confidence * 100), "muted")
        if analysis.basis:
            self.output.insert("end", "依据：" + analysis.basis + "\n", "muted")
        self.output.insert("end", "军师建议 · " + analysis.strategy + "\n", "head")
        self.output.insert("end", (analysis.advice or "—") + "\n")
        if analysis.facts:
            self.output.insert("end", "已知\n", "head")
            self.output.insert("end", "".join("· " + f + "\n" for f in analysis.facts))
        if analysis.unknowns:
            self.output.insert("end", "还不知道\n", "head")
            self.output.insert("end", "".join("· " + f + "\n" for f in analysis.unknowns))
        if analysis.stop_when:
            self.output.insert("end", "什么时候该停\n", "head")
            self.output.insert("end", analysis.stop_when + "\n")
        if analysis.candidates:
            self.output.insert("end", "候选回复\n", "head")
        elif not analysis.guarded:
            dropped = analysis.offered
            self.output.insert("end", "候选回复\n", "head")
            self.output.insert("end", ("模型给了 %d 条，都因格式或安全规则（链接、转账、过长、照抄对方原话）被过滤。" % dropped
                                       if dropped else "模型这次没有给出候选。")
                               + "可以再点一次“分析并给回复”。\n", "warn")
        for index, candidate in enumerate(analysis.candidates):
            card = tk.Frame(self.output, bg=C["bg"], highlightbackground=C["line"], highlightthickness=1,
                            padx=12, pady=8)
            buttons = tk.Frame(card, bg=C["bg"])
            buttons.pack(side="right", padx=(12, 0))
            self.action(buttons, "复制", lambda t=candidate.text: self.copy(t)).pack(side="left")
            self.action(buttons, "更像我一点", lambda i=index: self.start_rewrite(i)).pack(side="left", padx=(6, 0))
            parts = [card, self.label(card, candidate.text, 11, bg=C["bg"], anchor="w", justify="left",
                                      wraplength=440)]
            parts[-1].pack(fill="x")
            note = " · ".join(x for x in ("为什么：" + candidate.why if candidate.why else "",
                                          "代价：" + candidate.cost if candidate.cost else "") if x)
            if note:
                parts.append(self.label(card, note, 9, C["muted"], C["bg"], anchor="w", justify="left",
                                        wraplength=440))
                parts[-1].pack(fill="x")
            self._popup(parts, [("复制这条", lambda t=candidate.text: self.copy(t))])
            self.output.window_create("end", window=card, pady=4)
            self.output.insert("end", "\n")
            self.card_widgets.append(card)
        self.output.config(state="disabled")

    def copy(self, text) -> bool:
        if self.running():
            messagebox.showinfo("请先停止托管", "托管运行中会用到剪贴板。\n请先停止托管、由你接手，再复制。",
                                parent=self.root)
            return False
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.config(text="已复制到剪贴板：在微信里粘贴，自己决定发不发")
        return True

"""Explicit per-occurrence review + editable drafts inside the existing App.

No WeChat input/send API. Only the user can approve a specific text-only model
request. All review state is volatile; unknown blocks never enter the request.
"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from chat_core import PROVIDER_NAMES, incoming_guard
from visual_types import VisualPause, exact_title
from visual_sync import safe_alignment_diagnostic


class DraftReviewWindow:
    def __init__(self, app):
        self.app = app
        self.session = app.visual_review
        self.closed = False
        self.edits = {}
        self.ids = []
        self.editing_id = ""
        self.suppress = False
        self.consent_window = None
        self.window = tk.Toplevel(app.root)
        self.window.title("狗头军师 · 文字核对 / 草稿（不发送）")
        self.window.geometry(f"{min(850, app.root.winfo_screenwidth()-80)}x{min(760, app.root.winfo_screenheight()-100)}")
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _: app.pause())
        outer = tk.Frame(self.window)
        outer.pack(fill="both", expand=True)
        self.form_canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.form_canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.form_canvas.pack(side="left", fill="both", expand=True)
        self.form_canvas.configure(yscrollcommand=scrollbar.set)
        frame = self.form_frame = tk.Frame(self.form_canvas, padx=16, pady=12)
        form = self.form_canvas.create_window((0, 0), window=frame, anchor="nw")
        self.form_canvas.bind("<Configure>", lambda event: self.form_canvas.itemconfigure(form, width=event.width))
        frame.bind("<Configure>", lambda _: self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all")))
        self.status = tk.StringVar()
        tk.Label(frame, text="第二阶段：逐条核对文字 → 明确授权本次模型请求 → 编辑草稿", anchor="w").pack(fill="x")
        tk.Label(frame, text="只处理当前指定单聊；核对仅在本机。不读其他会话、不输入或发送微信。", anchor="w").pack(fill="x", pady=(4, 6))
        actions = tk.Frame(frame)
        actions.pack(fill="x")
        self.baseline_button = tk.Button(actions, text="用本轮快照重建基线", command=self.baseline)
        self.baseline_button.pack(side="left")
        tk.Button(actions, text="只读同步一次（5秒后）", command=lambda: app.visual_start(True)).pack(side="left", padx=6)
        tk.Button(actions, text="暂停", command=app.visual_stop).pack(side="left")
        tk.Button(actions, text="我已在微信回复", command=self.manual_reply).pack(side="left", padx=6)
        tk.Button(actions, text="同步诊断", command=self.show_diagnostic).pack(side="left")
        self.manual_snapshot_button = tk.Button(frame, text="人工核对当前快照（不判新旧）", command=self.manual_snapshot)
        self.manual_snapshot_button.pack(anchor="w", pady=(4, 0))
        self.bottom_confirmed = tk.BooleanVar(value=False)
        self.bottom_check = tk.Checkbutton(frame, variable=self.bottom_confirmed, command=self.bottom_changed,
            text="我已手动确认当前在此单聊最新底部；之后不手动翻历史，只让新消息推动画面", anchor="w",
            wraplength=780, justify="left")
        self.bottom_check.pack(fill="x", pady=(6, 0))
        tk.Label(frame, textvariable=self.status, justify="left", anchor="w", wraplength=780).pack(fill="x", pady=(8, 6))
        tk.Label(frame, text="选择一条文字；可修正识字错误后单独确认。方向不明、非文字项不能确认。", anchor="w").pack(fill="x")
        self.list = tk.Listbox(frame, height=5, exportselection=False)
        self.list.pack(fill="x", pady=(4, 6))
        self.list.bind("<<ListboxSelect>>", self.select)
        self.row_status = tk.StringVar(value="尚未选择文字")
        tk.Label(frame, textvariable=self.row_status, anchor="w").pack(fill="x")
        self.editor = tk.Text(frame, height=2, wrap="word")
        self.editor.pack(fill="x", pady=4)
        self.editor.bind("<<Modified>>", self.text_modified)
        row_actions = tk.Frame(frame)
        row_actions.pack(fill="x")
        self.confirm_button = tk.Button(row_actions, text="核对这条文字与方向（仅本机）", command=self.confirm)
        self.confirm_button.pack(side="left")
        self.manual_target_button = tk.Button(row_actions, text="指定本次回复对象", command=self.choose_manual_target)
        self.manual_target_button.pack(side="left", padx=6)
        tk.Label(frame, text="可编辑草稿（不会进入已发送上下文；此窗口没有发送能力）", anchor="w").pack(fill="x", pady=(10, 4))
        self.draft = tk.Text(frame, height=3, wrap="word")
        self.draft.pack(fill="x")
        self.draft.bind("<<Modified>>", self.draft_modified)
        self.generate_button = tk.Button(frame, text="预览本次上传文字 / 生成草稿", command=self.generate)
        self.generate_button.pack(anchor="w", pady=6)
        self.safety_notice = tk.Label(frame, text="像素模式仅处理基线后新文字；人工快照不判新旧，只处理你明确指定的一条。\n仅已核对文字可提交，图片/文件等省略；快照非持续监控，草稿绝不可自动发送。\n暂停丢弃在途结果，但不能撤回已经授权发出的模型请求。", justify="left", anchor="w", wraplength=780)
        self.safety_notice.pack(fill="x")
        self.window.bind("<MouseWheel>", self.scroll_form)
        self.refresh()
        self.window.after(250, self.poll_availability)

    def scroll_form(self, event):
        # Window-local only; text/list widgets retain their own wheel scroll.
        if event.widget not in (self.editor, self.draft, self.list) and event.delta:
            self.form_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

    def snapshot_problem(self, *, require_bottom=True):
        """One gate for both the button and the action; never authorize a
        stale snapshot just because the last explanatory label looked ready.
        Only reads existing local state. No capture, focus, model or input.
        """
        app = self.app
        result = app.visual_last
        cal = app.visual_calibration
        if not cal:
            return "没有已保存的区域校准，请先校准指定单聊。"
        if not self.session.matches(cal, app.store.selected(), app.store.config()):
            return "联系人、校准或设置已改变，请重新打开核对会话，再做单次只读检测。"
        if app.visual.running:
            return "识别尚未结束：5秒只是开始前的倒计时，不是完成时间。请保持微信前台，等待单次检测完成。"
        if not result:
            return "当前没有成功的单次快照。检测状态：" + app.visual_status.get() + "\n请点“只读同步一次”，切到微信并保持前台，完成后再返回。"
        if result.get("epoch") != app.visual.epoch:
            return "本轮快照在停止、暂停或重测后已作废。请重新“只读同步一次”；不用重新校准，除非窗口布局改变。"
        if not exact_title(result.get("title", ""), cal.expected_title):
            return "本轮完整标题未与指定联系人精确匹配，不能使用此快照。请核对目标后重新检测。"
        if require_bottom and not self.bottom_confirmed.get():
            return "快照已取得；建立基线前，请由你确认当前是会话最新底部，并勾选上方确认。不会替你滚动微信。"
        if result.get("evidence") is None:
            return "本轮缺少像素衔接摘要，请重新“只读同步一次”；不要用旧版快照建立新基线。"
        return ""

    def bottom_changed(self):
        if self.session.rows:
            self.app.visual_stop()
            self.session.require_rebaseline("最新底部确认已改变；旧基线/草稿失效，请重新单次检测。")
            self.refresh()
        else:
            self.update_availability()

    def current_snapshot(self, *, require_bottom=True):
        problem = self.snapshot_problem(require_bottom=require_bottom)
        if problem:
            raise VisualPause(problem)
        return self.app.visual_calibration, self.app.visual_last

    def show_diagnostic(self):
        stats = safe_alignment_diagnostic(self.session.sync_diagnostic)
        body = ("仅最近一次衔接的尺寸、行数和固定状态码；无截图、正文、哈希或身份信息。\n"
                "最接近位移仅供排查，绝不作为放行依据。重新检测不会恢复已失效基线。\n\n" +
                (json.dumps(stats, ensure_ascii=False, indent=2) if stats else "尚无衔接诊断；未主动读取微信。"))
        messagebox.showinfo("同步诊断（仅本机）", body, parent=self.window)

    def update_availability(self):
        baseline_problem = self.snapshot_problem()
        problem = self.snapshot_problem(require_bottom=not self.session.manual_mode)
        manual_problem = self.snapshot_problem(require_bottom=False)
        self.baseline_button.config(state="disabled" if baseline_problem or self.session.manual_mode else "normal")
        self.bottom_check.config(state="disabled" if self.session.manual_mode else "normal")
        can_change_mode = not self.app.visual.running and not self.app.visual_job_busy
        self.manual_snapshot_button.config(
            text="返回像素基线模式（重新检测）" if self.session.manual_mode else "人工核对当前快照（不判新旧）",
            state="normal" if can_change_mode and (self.session.manual_mode or not manual_problem) else "disabled")
        can_generate = (not problem and self.session.ready and (self.session.manual_mode or not self.session.needs_baseline)
                        and (not self.session.manual_mode or bool(self.session.pending()))
                        and not self.app.visual_job_busy)
        self.generate_button.config(state="normal" if can_generate else "disabled")
        notice = (manual_problem if self.session.manual_mode else problem) or self.session.notice
        self.status.set(notice + "\n当前仅显示最近一次快照，非连续监控。")

    def poll_availability(self):
        if self.closed:
            return
        # The monitor marks itself finished after queueing a snapshot. Recheck
        # that transition without rebuilding editors or losing user input.
        self.update_availability()
        self.window.after(250, self.poll_availability)

    def baseline(self):
        try:
            cal, result = self.current_snapshot()
            self.session.set_baseline(cal, self.app.store.selected(), self.app.store.config(), result["title"], result["rows"],
                                      evidence=result.get("evidence"), bottom_confirmed=self.bottom_confirmed.get())
            self.edits.clear()
            self.refresh()
            self.app.visual_draft_report()
        except VisualPause as error:
            messagebox.showinfo("基线未建立", str(error), parent=self.window)

    def manual_snapshot(self):
        if self.app.visual.running or self.app.visual_job_busy:
            return
        try:
            if self.session.manual_mode:
                self.session.leave_manual_mode()
                self.bottom_confirmed.set(False)
            else:
                cal, result = self.current_snapshot(require_bottom=False)
                self.session.set_manual_snapshot(cal, self.app.store.selected(), self.app.store.config(),
                                                 result["title"], result["rows"], evidence=result.get("evidence"))
            self.edits.clear()
            self.refresh()
            self.app.visual_draft_report()
        except VisualPause as error:
            messagebox.showinfo("人工快照不可用", str(error), parent=self.window)

    def choose_manual_target(self):
        try:
            self.current_snapshot(require_bottom=False)
            self.session.choose_manual_target(self.editing_id)
            self.refresh()
            self.app.visual_draft_report()
        except VisualPause as error:
            messagebox.showinfo("本次回复对象未指定", str(error), parent=self.window)

    def refresh(self):
        if self.closed:
            return
        self.close_consent()
        session = self.session
        self.status.set(session.notice + "\n当前仅显示最近一次快照，非连续监控。")
        old_id = self.editing_id
        self.list.delete(0, "end")
        self.ids = []
        for number, row in enumerate(session.rows, 1):
            self.ids.append(row.identity)
            origin = ("人工快照" if session.manual_mode else "基线" if row.identity in session.baseline else "新增")
            if session.manual_mode and row.identity == session.manual_target:
                origin = "本次指定（非新消息判定）"
            direction = {"friend": "对方→我", "self": "我→对方"}.get(row.direction, "方向不明")
            checked = "已核对" if row.identity in session.reviewed else "未核对"
            self.list.insert("end", f"{number}. {origin} · {direction} · {checked if row.safe else '不确定/非文字，排除'}")
        if old_id in self.ids:
            self.list.selection_set(self.ids.index(old_id))
        elif self.ids:
            self.list.selection_set(len(self.ids)-1)
        self.select()
        self.suppress = True
        self.draft.config(state="normal")
        self.draft.delete("1.0", "end")
        self.draft.insert("1.0", session.draft)
        self.draft.edit_modified(False)
        self.draft.config(state="normal" if session.ready and (session.manual_mode or not session.needs_baseline) else "disabled")
        self.suppress = False
        self.update_availability()

    def select(self, _=None):
        chosen = self.list.curselection()
        self.editing_id = self.ids[chosen[0]] if chosen else ""
        row = next((row for row in self.session.rows if row.identity == self.editing_id), None)
        self.suppress = True
        self.editor.config(state="normal")
        self.editor.delete("1.0", "end")
        if row:
            self.editor.insert("1.0", self.edits.get(row.identity, self.session.reviewed.get(row.identity, row.text)) if row.safe else "不确定/非文字，不能转为普通文字或提交模型。")
        self.editor.edit_modified(False)
        allowed = bool(row and row.safe and self.session.ready and (self.session.manual_mode or not self.session.needs_baseline))
        self.editor.config(state="normal" if allowed else "disabled")
        self.confirm_button.config(state="normal" if allowed else "disabled")
        target_allowed = bool(allowed and self.session.manual_mode and row.direction == "friend"
                              and row.identity in self.session.reviewed)
        self.manual_target_button.config(state="normal" if target_allowed else "disabled")
        self.row_status.set(("已在本机核对" if row and row.identity in self.session.reviewed else "待你核对完整文字与方向") if allowed else "不可核对；请完成基线或重新检测")
        self.suppress = False

    def text_modified(self, _=None):
        changed = self.editor.edit_modified()
        self.editor.edit_modified(False)
        if self.suppress or not changed or not self.editing_id:
            return
        self.edits[self.editing_id] = self.editor.get("1.0", "end-1c")
        self.session.unconfirm(self.editing_id)
        self.manual_target_button.config(state="disabled")
        self.close_consent()
        self.row_status.set("已编辑，须重新核对")
        self.clear_draft_widget()
        self.update_availability()

    def clear_draft_widget(self):
        self.suppress = True
        state = str(self.draft["state"])
        self.draft.config(state="normal")
        self.draft.delete("1.0", "end")
        self.draft.edit_modified(False)
        self.draft.config(state=state)
        self.suppress = False
        self.status.set(self.session.notice)

    def draft_modified(self, _=None):
        changed = self.draft.edit_modified()
        self.draft.edit_modified(False)
        if not self.suppress and changed:
            self.session.edit_draft(self.draft.get("1.0", "end-1c"))
            self.close_consent()
            self.status.set(self.session.notice)

    def confirm(self):
        try:
            self.session.confirm(self.editing_id, self.editor.get("1.0", "end-1c"))
            self.refresh()
        except VisualPause as error:
            messagebox.showinfo("文字未确认", str(error), parent=self.window)

    def manual_reply(self):
        self.app.visual_stop()
        self.session.manual_reply()
        self.refresh()

    def generate(self):
        if self.app.visual.running or self.app.visual_job_busy:
            return messagebox.showinfo("当前不可生成", "先停止持续识别并完成单次快照；一次只允许一个草稿请求。", parent=self.window)
        try:
            self.current_snapshot(require_bottom=not self.session.manual_mode)
            request = self.session.prepare()
            # Deterministic sensitive/stop handling remains local; do not send
            # such material to the model merely to ask whether to hand it off.
            guarded = next((hit for item in (*request.context, {"role": "user", "content": request.text})
                            if item["role"] == "user" and (hit := incoming_guard(item["content"]))), None)
            if guarded:
                self.session.authorize(request)
                self.session.complete(request.token, guarded)
                self.refresh()
                return
            account = self.app.ai.plan.status() if request.config["provider"] == "chatgpt" else None
        except ValueError as error:
            return messagebox.showinfo("草稿尚不能生成", str(error), parent=self.window)
        self.close_consent()
        consent = self.consent_window = tk.Toplevel(self.window)
        consent.title("仅本次文字请求 · 由你确认后才提交模型")
        consent.geometry(f"{min(760, self.window.winfo_screenwidth()-80)}x{min(650, self.window.winfo_screenheight()-100)}")
        consent.transient(self.window)
        consent.bind("<Escape>", lambda _: self.app.pause())
        provider = PROVIDER_NAMES[request.config["provider"]]
        info = f"目标：{request.profile['name']}\n服务：{provider} · 模型：{request.config['model']}"
        if request.mode == "manual_snapshot":
            info += "\n确认方式：人工指定当前快照；未判断新旧，不代表最新或尚未回复，请自行核实。"
        if account:
            info += "\nChatGPT 账号：" + account["label"]
        tk.Label(consent, text=info + "\n只上传下方核对文字、所选联系人资料、你的事实/口吻及现有狗头军师规则。\n未核对文字、其他联系人、非文字块及截图均不提交；可能有上下文缺失。", justify="left", anchor="w", wraplength=700).pack(fill="x", padx=16, pady=12)
        body = tk.Text(consent, wrap="word")
        body.pack(fill="both", expand=True, padx=16)
        # Match prompt_for's allowlist exactly; no IDs, keys or auth fields.
        profile = {key: request.profile.get(key, "") for key in ("name", "stage", "goal", "context", "my_mbti", "their_mbti", "my_score", "their_score")}
        reply_label = "本次人工指定文字（未判新旧）" if request.mode == "manual_snapshot" else "最新待回复文字"
        preview = {"所选联系人资料": profile, "我的真实信息": request.config["my_facts"], "我的口吻": request.config["my_style"],
                   "已核对上下文": list(request.context), reply_label: request.text}
        body.insert("1.0", json.dumps(preview, ensure_ascii=False, indent=2))
        body.config(state="disabled")
        def authorize():
            if self.app.visual_job_busy:
                return
            try:
                self.current_snapshot(require_bottom=not self.session.manual_mode)
                self.session.authorize(request)
            except VisualPause as error:
                self.close_consent()
                return messagebox.showinfo("本次请求已失效", str(error), parent=self.window)
            self.close_consent()
            self.run_request(request, account["selected"] if account else None)
        tk.Button(consent, text="同意仅本次文字请求，生成草稿（不发微信）", command=authorize).pack(side="right", padx=16, pady=12)
        tk.Button(consent, text="取消，不上传", command=self.close_consent).pack(side="left", padx=16, pady=12)
        consent.protocol("WM_DELETE_WINDOW", self.close_consent)

    def run_request(self, request, account):
        app, session = self.app, self.session
        app.visual_job_busy = True
        app.visual_draft_stats["jobs_started"] += 1
        self.refresh()
        def work():
            try:
                decision = app.ai.decide(request.profile, "visual:" + session.session_id, request.text,
                    confirmed_context=request.context, expected_config=request.config,
                    request_valid=lambda: session.accepts(request.token), expected_account=account)
                error = ""
            except Exception:
                # Do not expose arbitrary provider exception text/chat/tokens.
                decision, error = None, "草稿请求失败或已失效，请检查现有模型设置；本次未发送微信。不会自动重试。"
            app.engine.emit("visual_draft_result", token=request.token, decision=decision, error=error)
        threading.Thread(target=work, daemon=True).start()

    def close_consent(self):
        if self.consent_window is not None:
            try:
                self.consent_window.destroy()
            except tk.TclError:
                pass
            self.consent_window = None

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.close_consent()
        self.session.close()
        self.edits.clear()
        self.app.visual_stop()
        self.app.visual_draft_window = None
        self.app.visual_draft_report()
        self.window.destroy()
        self.app.visual_controls_state(self.app.view_mode.get() == "视觉只读")

"""Phase-two, memory-only review state. No capture, OCR, network or sending.

An occurrence is not a string: fixed-position appends retain individual IDs.
Lost anchors/reflow fail closed and require a new, explicit baseline. Drafts
are bound to a reviewed snapshot, never evidence that WeChat is still current.
"""
from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, replace

from visual_types import ObservationTracker, VisualPause, exact_title
from visual_sync import PixelEvidence, PixelSequenceTracker, safe_alignment_diagnostic


@dataclass(frozen=True)
class DraftRequest:
    token: tuple
    profile: dict
    config: dict
    context: tuple
    text: str
    mode: str = "new_messages"


class ReviewSession:
    def __init__(self):
        self.active = False
        self.revision = 0
        self.rows = ()
        self.reviewed = {}
        self.baseline = set()
        self.answered = set()
        self.tracker = ObservationTracker()
        self.pixel_tracking = False
        self.manual_mode = False
        self.manual_target = ""
        self.binding = ""
        self.profile = None
        self.config = None
        self.session_id = ""
        self.ready = False
        self.needs_baseline = True
        self.rebaseline_reason = ""
        self.sync_diagnostic = {}
        self.draft = ""
        self.request_token = None
        self.notice = "第二阶段尚未启用；不会自动读取或请求模型。"

    def invalidate(self, reason):
        self.revision += 1
        self.draft = ""
        self.request_token = None
        self.notice = reason

    def close(self):
        self.invalidate("核对会话已关闭，本机核对文字及草稿已清空。")
        self.active = self.ready = False
        self.rows = ()
        self.reviewed.clear()
        self.baseline.clear()
        self.answered.clear()
        self.profile = self.config = None
        self.binding = ""
        self.tracker = ObservationTracker()
        self.pixel_tracking = False
        self.manual_mode = False
        self.manual_target = ""
        self.needs_baseline = True
        self.rebaseline_reason = ""
        self.sync_diagnostic = {}

    @staticmethod
    def binding_key(calibration):
        return json.dumps(calibration.to_dict(), sort_keys=True, ensure_ascii=False)

    def begin(self, calibration, profile, config):
        calibration.validate()
        if calibration.target_kind != "contact" or not profile:
            raise VisualPause("第二阶段需选择已建资料的指定单聊；文件传输助手仍可用于第一阶段。")
        if (profile["id"], profile["name"]) != (calibration.profile_id, calibration.expected_title) or config["selected"] != profile["id"]:
            raise VisualPause("当前资料与校准目标不一致，请重新校准。")
        self.close()
        self.active = True
        self.session_id = uuid.uuid4().hex
        self.profile, self.config = copy.deepcopy(profile), copy.deepcopy(config)
        self.binding = self.binding_key(calibration)
        self.notice = "先手动只读检测；人工快照需核对并指定文字、不建基线；像素模式另需确认基线。不会自动请求草稿。"

    def matches(self, calibration, profile, config):
        return (self.active and self.binding == self.binding_key(calibration)
                and profile == self.profile and config == self.config
                and config["selected"] == self.profile["id"])

    def reading(self):
        self.ready = False
        if self.manual_mode:
            self.rows = ()
            self.reviewed.clear()
            self.manual_target = ""
        self.invalidate("正在重新核对微信画面；旧草稿已失效。")

    def pause(self, reason="已暂停；旧草稿失效。请重新只读检测。"):
        self.ready = False
        if self.manual_mode:
            self.rows = ()
            self.reviewed.clear()
            self.manual_target = ""
        self.invalidate(reason)

    def require_rebaseline(self, reason):
        """Do not keep stale rows/reviews under a newly captured snapshot."""
        self.pause(reason)
        self.needs_baseline = True
        self.rebaseline_reason = reason
        self.rows = ()
        self.reviewed.clear()
        self.baseline.clear()
        self.answered.clear()

    def observe(self, calibration, profile, config, title, rows, *, evidence=None):
        if not self.matches(calibration, profile, config) or not exact_title(title, self.profile["name"]):
            self.require_rebaseline("目标、标题或设置已改变；请关闭核对会话并重新建立基线。")
            raise VisualPause(self.notice)
        if self.manual_mode:
            self.set_manual_snapshot(calibration, profile, config, title, rows, evidence=evidence)
            return
        if self.needs_baseline:
            self.ready = True
            self.notice = (("旧基线已失效：" + self.rebaseline_reason +
                            "\n本轮仅取得待重建快照，未同步新消息；旧列表已清空。重建会将当前来信全部记为已有消息。")
                           if self.rebaseline_reason else "本轮快照可设为基线；尚未同步任何新消息。")
            return
        try:
            tracked, added = self.tracker.update(rows, evidence) if self.pixel_tracking else self.tracker.update(rows)
        except VisualPause as error:
            self.sync_diagnostic = safe_alignment_diagnostic(getattr(self.tracker, "diagnostic", {}))
            self.require_rebaseline(str(error) if self.pixel_tracking else "画面锚点变化或滚动/换行重排，无法可靠区分新旧；请明确重建基线，不会把旧消息当新消息。")
            raise VisualPause(self.notice) from None
        self.sync_diagnostic = safe_alignment_diagnostic(getattr(self.tracker, "diagnostic", {}))
        if added:
            self.invalidate("收到新增候选，旧草稿失效；请逐条核对新文字。")
        self.rows, self.ready = tracked, True
        self.reviewed = {key: text for key, text in self.reviewed.items() if any(row.identity == key and row.safe for row in tracked)}
        if self.pixel_tracking and (self.tracker.last_shift or self.tracker.last_shift_x):
            displacement = f"上移{-self.tracker.last_shift}像素"
            if self.tracker.last_shift_x:
                displacement += f"，横向{self.tracker.last_shift_x:+d}像素"
            self.notice = f"旧画面按唯一像素重叠衔接（{displacement}），新增{len(added)}条；仍须逐条核对，不自动上传或发送。"
        elif not added:
            self.notice = "本轮快照核对完成：没有新增候选，基线保持有效。旧不确定项仍隔离；不代表此后微信没有变化。"
        if any(not row.safe for row in added):
            self.pause("存在新增的不确定/非文字消息；未交给模型，请处理后重建基线。")
        elif any(row.direction == "self" for row in added):
            self.invalidate("观察到本人回复，旧草稿失效；此前对方消息不再作为待回复。")

    def set_baseline(self, calibration, profile, config, title, rows, *, evidence=None, bottom_confirmed=False):
        if not self.matches(calibration, profile, config) or not exact_title(title, self.profile["name"]):
            raise VisualPause("目标、完整标题或设置不匹配，不能建立基线。")
        tracker = PixelSequenceTracker() if bottom_confirmed else ObservationTracker()
        try:
            tracked, _ = tracker.update(rows, evidence) if bottom_confirmed else tracker.update(rows)
        except VisualPause:
            self.sync_diagnostic = safe_alignment_diagnostic(getattr(tracker, "diagnostic", {}))
            self.require_rebaseline("基线摘要校验未通过；旧草稿已作废，需重新检测。")
            raise
        self.invalidate("基线已建立：只记录已有消息，不生成回复。等待此后可可靠识别的新文字。")
        self.tracker, self.rows, self.pixel_tracking = tracker, tracked, bottom_confirmed
        self.manual_mode, self.manual_target = False, ""
        self.rebaseline_reason = ""
        self.sync_diagnostic = safe_alignment_diagnostic(getattr(tracker, "diagnostic", {}))
        self.baseline = {row.identity for row in self.rows}
        self.reviewed.clear()
        self.answered.clear()
        self.ready, self.needs_baseline = True, False

    def set_manual_snapshot(self, calibration, profile, config, title, rows, *, evidence=None):
        """Explicit snapshot review, NOT an alternative new-message matcher.

        No baseline is established or restored. Every refresh drops reviews
        and the reply designation, even if the pixels/text happen to match.
        """
        if not self.matches(calibration, profile, config) or not exact_title(title, self.profile["name"]):
            self.require_rebaseline("目标、完整标题或设置不匹配，人工快照不可用。")
            raise VisualPause(self.notice)
        try:
            if not isinstance(evidence, PixelEvidence):
                raise VisualPause("缺少本轮像素摘要，请重新只读检测，不能使用旧快照。")
            evidence.validate()
            if (evidence.width, evidence.height) != (calibration.messages.width, calibration.messages.height):
                raise VisualPause("快照尺寸与当前校准不一致，不能人工核对。")
            if any(not row.box.inside(evidence.width, evidence.height) for row in rows):
                raise VisualPause("快照候选越界，不能人工核对。")
        except VisualPause:
            self.require_rebaseline("人工快照校验未通过；旧核对文字和草稿已清空，请重新检测。")
            raise
        self.invalidate("人工快照：未判断新旧、未恢复像素同步。核对文字后，请明确指定本次回复对象；不会自动上传或发送。")
        # Independent IDs cannot carry review/designation across captures.
        prefix = uuid.uuid4().hex
        self.rows = tuple(replace(row, identity=f"snapshot:{prefix}:{i}", origin="人工快照（未判新旧）")
                          for i, row in enumerate(rows))
        self.reviewed.clear()
        self.baseline.clear()
        self.answered.clear()
        self.manual_mode, self.manual_target = True, ""
        self.pixel_tracking = False
        self.tracker = ObservationTracker()
        self.ready, self.needs_baseline = True, True

    def leave_manual_mode(self):
        self.require_rebaseline("已回到像素模式；请重新检测并明确建立基线，不会继承人工快照的待回复或核对结果。")
        self.manual_mode, self.manual_target = False, ""
        self.pixel_tracking = False
        self.tracker = ObservationTracker()

    def choose_manual_target(self, identity):
        row = next((row for row in self.rows if row.identity == identity), None)
        if (not self.manual_mode or not self.active or not self.ready or not row or not row.safe
                or row.direction != "friend" or identity not in self.reviewed or identity in self.answered):
            raise VisualPause("只能人工指定本轮已核对的对方普通文字；不确定、本人消息及旧快照均不可选。")
        self.invalidate("已人工指定本次回复对象，未判定其为新消息；请预览并单独授权本次模型请求。")
        self.manual_target = identity

    def confirm(self, identity, text):
        row = next((row for row in self.rows if row.identity == identity), None)
        if not self.active or not self.ready or (self.needs_baseline and not self.manual_mode) or not row or not row.safe:
            raise VisualPause("这条不是当前可核对的普通文字。不能把不确定/非文字改成普通文字。")
        text = text.strip()
        if not text or len(text) > 8000:
            raise VisualPause("请核对完整文字；每条须为1—8000字。")
        self.invalidate("这条文字已在本机核对；尚未上传、未生成或发送。")
        if identity == self.manual_target:
            self.manual_target = ""
        self.reviewed[identity] = text

    def unconfirm(self, identity):
        self.reviewed.pop(identity, None)
        if identity == self.manual_target:
            self.manual_target = ""
        self.invalidate("核对文字正在编辑，须重新确认；旧草稿失效。")

    def pending(self):
        if self.manual_mode:
            return tuple(row for row in self.rows if row.identity == self.manual_target and row.safe
                         and row.direction == "friend" and row.identity in self.reviewed
                         and row.identity not in self.answered)
        new = [row for row in self.rows if row.identity not in self.baseline]
        last_self = max((i for i, row in enumerate(new) if row.direction == "self"), default=-1)
        return tuple(row for row in new[last_self + 1:] if row.safe and row.direction == "friend" and row.identity not in self.answered)

    def manual_reply(self):
        self.answered.update(row.identity for row in self.pending())
        self.pause("你已接手回复：此前待回复项已关闭，旧草稿失效。请重新只读同步。")

    def prepare(self):
        if not self.active or not self.ready or (self.needs_baseline and not self.manual_mode):
            raise VisualPause("请先完成只读检测和明确的基线核对；当前没有可用的新消息快照。")
        if not self.manual_mode and any(not row.safe for row in self.rows if row.identity not in self.baseline):
            raise VisualPause("新增消息中有不确定项，不能请求模型。")
        pending = self.pending()
        if not pending:
            if self.manual_mode:
                raise VisualPause("人工快照不自动选择新消息；请先核对并明确指定一条本次要回复的对方文字。")
            raise VisualPause("没有基线之后待回复的新文字；不会为已有消息自动生成回复。")
        if any(row.identity not in self.reviewed for row in pending):
            raise VisualPause("请先逐条核对所有待回复的新文字及其方向。")
        latest = pending[-1]
        upto = self.rows[:self.rows.index(latest)]
        context = tuple({"role": "user" if row.direction == "friend" else "assistant", "content": self.reviewed[row.identity]}
                        for row in upto if row.safe and row.identity in self.reviewed)[-31:]
        text = self.reviewed[latest.identity]
        if sum(len(item["content"]) for item in context) + len(text) > 32000:
            raise VisualPause("已核对的上下文过长；请缩短本轮核对范围或重建基线。")
        if not self.config.get("cloud_consent") or self.config.get("cloud_provider") != self.config.get("provider"):
            raise VisualPause("现有模型消息传输许可未开启；请由你在原有设置中处理，核对不会扩大授权。")
        token = (self.session_id, self.revision, uuid.uuid4().hex)
        return DraftRequest(token, copy.deepcopy(self.profile), copy.deepcopy(self.config), copy.deepcopy(context), text,
                            "manual_snapshot" if self.manual_mode else "new_messages")

    def authorize(self, request):
        if (not self.active or not self.ready or request.token[:2] != (self.session_id, self.revision)
                or request.mode != ("manual_snapshot" if self.manual_mode else "new_messages")):
            raise VisualPause("核对快照已经变化，本次请求作废。")
        self.request_token = request.token
        self.draft = ""
        self.notice = "已明确授权本次文字请求；生成的是可编辑草稿，不是已发送消息。"

    def accepts(self, token):
        return self.active and self.ready and self.request_token == token and token[:2] == (self.session_id, self.revision)

    def complete(self, token, decision):
        if not self.accepts(token):
            return False
        self.request_token = None
        self.draft = decision.text if decision.action == "reply" else ""
        self.notice = "草稿已生成，未发送；检测到变化或暂停后会失效。" if self.draft else {
            "wait": "本轮留白", "stop": "停止联系，请你处理", "handoff": "敏感事项，请你亲自处理"}.get(decision.action, "未产生草稿")
        return True

    def edit_draft(self, text):
        self.invalidate("你编辑的本机草稿，未发送；不是微信当前状态的证明。")
        self.draft = text[:8000] if self.active and self.ready else ""

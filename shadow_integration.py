"""Opt-in, single-run coordination for image-required SHADOW observations.

No capture, model, input, persistence or UI APIs. A UI consent is a caller
assertion, not proof of bottom position or continuous visual identity.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from bubble_shadow import BubbleShadowTracker, Snapshot, TrackingScope
from visual_types import Calibration, VisualPause


@dataclass(frozen=True, repr=False)
class ShadowConsent:
    calibration: Calibration
    confirmed: bool
    latest_bottom_confirmed: bool
    no_manual_operations_confirmed: bool
    max_seconds: int = 30
    max_samples: int = 12
    created_at: float = field(default_factory=time.monotonic)
    _used: threading.Event = field(default_factory=threading.Event, compare=False)
    _lock: object = field(default_factory=threading.Lock, compare=False)

    def claim(self, calibration, now):
        with self._lock:
            if (self._used.is_set() or self.confirmed is not True
                    or self.latest_bottom_confirmed is not True
                    or self.no_manual_operations_confirmed is not True
                    or self.calibration != calibration
                    or type(self.max_seconds) is not int or not 1 <= self.max_seconds <= 30
                    or type(self.max_samples) is not int or not 1 <= self.max_samples <= 12
                    or type(self.created_at) not in (int, float)
                    or not 0 <= now - self.created_at <= 30):
                raise VisualPause("缺少有效的本轮只读对照确认；未开始读取。")
            calibration.validate()
            self._used.set()
            return now + self.max_seconds


class ShadowObservationRun:
    def __init__(self, calibration, *, epoch, deadline, max_samples):
        self.scope = TrackingScope(calibration.profile_id, calibration.expected_title,
            calibration.binding, calibration.messages, calibration.version, f"shadow-run:{epoch}")
        self.deadline = deadline
        self.max_samples = max_samples
        self.samples = 0
        self.new_total = 0
        self.tracker = BubbleShadowTracker()  # actual appearance REQUIRED

    def context(self, now):
        if self.tracker is None or now >= self.deadline or self.samples >= self.max_samples:
            raise VisualPause("本轮只读对照采样已结束；不再截取，在途结果作废。")
        return Snapshot(self.scope, self.samples + 1, int(now * 1000),
                        self.scope.expected_title, (), True, True, True, "none")

    def observe(self, snapshot, now):
        self.context(now)
        if (snapshot.scope != self.scope or snapshot.sequence != self.samples + 1):
            raise VisualPause("只读对照的观察周期或目标已变化，结果作废。")
        result = (self.tracker.establish_baseline(snapshot) if self.samples == 0
                  else self.tracker.update(snapshot))
        self.samples += 1
        self.new_total += len(result.new_ids)
        report = {**result.numeric_report(), "samples": self.samples,
                  "new_hypotheses_total": self.new_total, "model_requests": 0,
                  "wechat_inputs": 0, "wechat_sends": 0, "screenshots_saved_or_uploaded": 0}
        return report

    def close(self):
        self.tracker = None  # drop all text/occurrence/private feature references


def shadow_message(report):
    verdict = {"old": "旧快照", "new": "新增假设", "uncertain": "不确定"}.get(report["verdict"], "不确定")
    prefix = "起始快照仅记作旧消息。" if report.get("baseline") else ""
    total = report.get("new_hypotheses_total", report["new_hypotheses"])
    return (f"只读对照第{report['samples']}次：{verdict}；本次新增假设{report['new_hypotheses']}条，"
            f"本轮累计{total}条假设。"
            + prefix + "仅实验判断，不进入草稿/模型/发送；遇到不确定即结束。")

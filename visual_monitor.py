"""First-stage read-only coordinator: no ChatAI, Engine, send, input or history."""
from __future__ import annotations

import queue
import threading
import time

from visual_types import ObservationTracker, RevisionGate, VisualPause


class ReadOnlyMonitor:
    def __init__(self, selected, capture_factory=None, ocr_factory=None, title_ocr_factory=None):
        self.selected = selected
        self.capture_factory = capture_factory
        self.ocr_factory = ocr_factory
        self.title_ocr_factory = title_ocr_factory
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.signal = threading.Event()
        self.thread = None
        self.epoch = 0
        self.running = False
        self.gate = RevisionGate()

    def stop(self):
        self.signal.set()
        with self.lock:
            self.running = False
            self.epoch += 1
            self.gate.stop()

    def _current(self, epoch, signal):
        return self.epoch == epoch and not signal.is_set() and self.running

    def start(self, calibration, *, once=False, delay=5):
        self._start(calibration, once=once, delay=delay)

    def start_shadow(self, calibration, consent, *, delay=5):
        from shadow_integration import ShadowConsent
        if not isinstance(consent, ShadowConsent):
            raise VisualPause("请先明确确认本次只读对照；未开始读取。")
        self._start(calibration, once=False, delay=delay, shadow_consent=consent)

    def _start(self, calibration, *, once, delay, shadow_consent=None):
        calibration.validate()
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise VisualPause("上一轮本地识别尚在结束，请稍后再开始。")
            if self.selected(calibration) != (calibration.profile_id, calibration.expected_title):
                raise VisualPause("当前联系人不再是校准目标，请重新校准。")
            shadow_deadline = shadow_consent.claim(calibration, time.monotonic()) if shadow_consent else None
            self.epoch += 1
            epoch = self.epoch
            self.signal = signal = threading.Event()
            self.running = True
            self.gate.start()
            self.thread = threading.Thread(target=self._run,
                args=(calibration, once, delay, epoch, signal, shadow_consent, shadow_deadline), daemon=True)
            self.thread.start()

    def _event(self, epoch, signal, **event):
        with self.lock:
            if self._current(epoch, signal):
                self.events.put(dict(epoch=epoch, **event))

    def _run(self, calibration, once, delay, epoch, signal, shadow_consent=None, shadow_deadline=None):
        initialized = False
        reader = shadow_run = shadow_snapshot = None
        try:
            def alive():
                return (self._current(epoch, signal)
                        and (shadow_deadline is None or time.monotonic() < shadow_deadline))
            def wait(seconds):
                remaining = max(0, shadow_deadline - time.monotonic()) if shadow_deadline is not None else seconds
                return signal.wait(min(seconds, remaining)) or not alive()
            for seconds in range(delay, 0, -1):
                self._event(epoch, signal, kind="status", message=f"{seconds}秒后只读检测：请手动切到文件传输助手/已校准的微信窗口。")
                if wait(1):
                    return
            if not alive():
                return
            from ocr_runtime import ensure_ocr_runtime
            ensure_ocr_runtime()
            from winrt.runtime import ApartmentType, init_apartment
            init_apartment(ApartmentType.MULTI_THREADED)
            initialized = True
            from message_ocr import LocalMessageOCR
            from visual_capture import WindowsCapture
            from visual_recognition import VisualReader
            from title_ocr import LocalTitleOCR
            backend = self.capture_factory() if self.capture_factory else WindowsCapture()
            title_ocr = self.title_ocr_factory() if self.title_ocr_factory else LocalTitleOCR()
            ocr = self.ocr_factory() if self.ocr_factory else LocalMessageOCR(engine=title_ocr.engine)
            def capture(regions):
                with self.lock:
                    if not alive():
                        raise VisualPause("识别已停止。")
                    if self.selected(calibration) != (calibration.profile_id, calibration.expected_title):
                        raise VisualPause("联系人已切换，已停止读取并作废旧状态。")
                    return backend.capture(calibration.binding, regions)
            reader, tracker = VisualReader(capture, ocr, title_ocr=title_ocr), ObservationTracker()
            if shadow_consent:
                from shadow_integration import ShadowObservationRun, shadow_message
                shadow_run = ShadowObservationRun(calibration, epoch=epoch, deadline=shadow_deadline,
                                                   max_samples=shadow_consent.max_samples)
            while alive():
                if shadow_run is not None:
                    context = shadow_run.context(time.monotonic())
                    reader.poll(calibration, wait, shadow_context=context)
                    shadow_snapshot = reader.last_shadow_snapshot
                    reader.last_shadow_snapshot = None
                    if not alive():
                        return
                    if self.selected(calibration) != (calibration.profile_id, calibration.expected_title):
                        raise VisualPause("当前联系人已变化，本轮只读对照结果作废。")
                    report = shadow_run.observe(shadow_snapshot, time.monotonic())
                    shadow_snapshot = None
                    self._event(epoch, signal, kind="shadow", report=report, message=shadow_message(report))
                    if report["verdict"] == "uncertain" or shadow_run.samples >= shadow_run.max_samples:
                        break
                    if wait(0.5):
                        break
                    continue
                title, candidates = reader.poll(calibration, signal.wait)
                safe = all(row.safe for row in candidates)
                added, rows = (), candidates
                message = "双帧/双尺度一致的本地候选，仍需人工核对；未调用模型、未输入微信。"
                if safe:
                    try:
                        rows, added = tracker.update(candidates)
                    except VisualPause as error:
                        safe, message = False, str(error)
                else:
                    message = f"共{len(candidates)}条候选：文字{sum(row.safe for row in candidates)}条，不确定{sum(not row.safe for row in candidates)}条；已暂停，不猜测，不调用对话模型或发送。"
                if added:
                    self.gate.invalidate()
                    if any(row.direction == "self" for row in added):
                        safe, message = False, "检测到新增的本人回复候选，已暂停，旧状态/草稿作废；需你核对。"
                paused = once or not safe
                self._event(epoch, signal, kind="snapshot", title=title, rows=rows, evidence=reader.last_evidence,
                            added=len(added), paused=paused, message=message)
                if paused:
                    break
                if signal.wait(2):
                    return
        except VisualPause as error:
            self._event(epoch, signal, kind="paused", message=str(error))
        except Exception:
            # No raw exception/event data or chat content in diagnostics.
            self._event(epoch, signal, kind="paused", message="本地视觉连接器未通过运行校验，已暂停；未调用模型、未输入微信。")
        finally:
            shadow_snapshot = None
            if reader is not None:
                reader.last_shadow_snapshot = None
            if shadow_run is not None:
                shadow_run.close()
            if shadow_consent is not None:
                self._event(epoch, signal, kind="shadow_end",
                    message="本轮只读对照已结束；未调用模型、未输入或发送。它不等于自动收发已验收。")
            with self.lock:
                if epoch == self.epoch:
                    self.running = False
                    self.gate.stop()
            signal.set()
            if initialized:
                from winrt.runtime import uninit_apartment
                uninit_apartment()

    def send(self, *_args, **_kwargs):
        raise PermissionError("第一阶段视觉连接器没有发送能力。")

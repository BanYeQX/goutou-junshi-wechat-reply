"""Bounded numeric capture statistics, no images/log files/native/model calls.

CPU is single-core kernel+user delta. 3% is a STOP reference pending a new
measurement/decision, NOT a newly accepted CPU limit or a reason to slow down.
"""
from __future__ import annotations

import math
import os
import time

from wechat_capture import CaptureError, Coverage


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def cpu_percent(before, after, elapsed):
    """Tuples are (creation FILETIME, kernel ticks, user ticks), 100ns units."""
    if (not _finite(elapsed) or elapsed <= 0 or not isinstance(before, tuple)
            or not isinstance(after, tuple) or len(before) != 3 or len(after) != 3
            or any(type(n) is not int or n < 0 for n in before+after)
            or before[0] != after[0] or before[0] <= 0
            or after[1] < before[1] or after[2] < before[2]):
        raise CaptureError("invalid_cpu_interval_or_process_replaced")
    ticks = (after[1]-before[1])+(after[2]-before[2])
    return ticks/10_000_000/elapsed*100  # Never divide by logical CPU count.


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered)*fraction)-1)]


class CaptureMetrics:
    def __init__(self, max_samples=50_000):
        if type(max_samples) is not int or not 1 <= max_samples <= 100_000:
            raise CaptureError("invalid_metric_limit")
        self.limit = max_samples
        self.times, self.frame_ms = [], []
        self.center = self.grid = self.incomplete = 0
        self.first_incomplete = self.last_incomplete = None
        self._run_first = None
        self._run_count = 0
        self.longest_incomplete_samples = 0
        self.longest_incomplete_span = 0.0
        self.max_msg_change = None
        self.cpu_py = self.cpu_wechat = self.cpu_wechat_incremental = None
        self.cpu_elapsed = None
        self._cpu_windows = []
        self.failed_samples = 0
        self.post_capture_stops = 0
        self.stop_required = False

    def add(self, timestamp, frame_ms, coverage, changes=None):
        if len(self.times) >= self.limit:
            raise CaptureError("metric_budget_reached")
        if (not _finite(timestamp) or not _finite(frame_ms) or frame_ms < 0
                or not isinstance(coverage, Coverage)
                or (self.times and timestamp <= self.times[-1])):
            raise CaptureError("invalid_capture_metric")
        if changes is not None and (not isinstance(changes, dict)
                or set(changes) != {"left", "head", "msg", "input"}
                or any(not _finite(v) or not 0 <= v <= 1 for v in changes.values())):
            raise CaptureError("invalid_change_metric")
        self.times.append(timestamp)
        self.frame_ms.append(frame_ms)
        self.center += int(coverage.center_hit)
        self.grid += int(coverage.grid_covered)
        if not coverage.grid_covered:
            self.incomplete += 1
            self.first_incomplete = timestamp if self.first_incomplete is None else self.first_incomplete
            self.last_incomplete = timestamp
            if self._run_first is None:
                self._run_first = timestamp
            self._run_count += 1
            self.longest_incomplete_samples = max(self.longest_incomplete_samples, self._run_count)
            self.longest_incomplete_span = max(self.longest_incomplete_span, timestamp-self._run_first)
        else:
            self._run_first, self._run_count = None, 0
        if changes is not None:
            self.max_msg_change = max(self.max_msg_change or 0, changes["msg"])

    def note_cpu(self, py, wechat, elapsed, *, wechat_idle_baseline=None):
        if (any(not _finite(v) or v < 0 for v in (py, wechat))
                or not _finite(elapsed) or elapsed <= 0
                or (wechat_idle_baseline is not None and
                    (not _finite(wechat_idle_baseline) or wechat_idle_baseline < 0))):
            raise CaptureError("invalid_cpu_metric")
        if len(self._cpu_windows) >= self.limit:
            raise CaptureError("cpu_metric_budget_reached")
        self.cpu_py, self.cpu_wechat, self.cpu_elapsed = py, wechat, elapsed
        self.cpu_wechat_incremental = (None if wechat_idle_baseline is None else wechat-wechat_idle_baseline)
        self._cpu_windows.append((py, wechat, elapsed, self.cpu_wechat_incremental))
        # Latched for the whole run. No policy/interval object is reachable here.
        self.stop_required = self.stop_required or py > 3.0

    def require_continue(self):
        if self.stop_required:
            raise CaptureError("cpu_above_3_percent_stop_and_report")

    def summary(self):
        n = len(self.times)
        buckets = {}
        if n:
            for timestamp, value in zip(self.times, self.frame_ms):
                index = int((timestamp-self.times[0])//600)
                buckets.setdefault(index, []).append(value)
        cpu_span = sum(v[2] for v in self._cpu_windows)
        py_mean = sum(v[0]*v[2] for v in self._cpu_windows)/cpu_span if cpu_span else None
        wx_mean = sum(v[1]*v[2] for v in self._cpu_windows)/cpu_span if cpu_span else None
        wx_increment = (sum(v[3]*v[2] for v in self._cpu_windows)/cpu_span
                        if cpu_span and all(v[3] is not None for v in self._cpu_windows) else None)
        return {"samples": n, "failed_samples": self.failed_samples,
                "post_capture_stops": self.post_capture_stops,
                "duration_seconds": self.times[-1]-self.times[0] if n else None,
                "center_hit_ratio": self.center/n if n else None,
                "grid_covered_ratio": self.grid/n if n else None,
                "ever_not_fully_covered": self.incomplete > 0 if n else None,
                "not_fully_covered_samples": self.incomplete,
                "first_incomplete_at": self.first_incomplete, "last_incomplete_at": self.last_incomplete,
                "longest_incomplete_run_samples": self.longest_incomplete_samples,
                "longest_incomplete_observation_span_seconds": self.longest_incomplete_span,
                "coverage_meaning": "nine_point_hit_test_proxy_not_complete_opaque_coverage",
                "frame_mean_ms": sum(self.frame_ms)/n if n else None,
                "frame_p95_ms": percentile(self.frame_ms, .95),
                "frame_max_ms": max(self.frame_ms) if n else None,
                "slow_frames_over_400_ms": sum(v > 400 for v in self.frame_ms),
                "ten_minute_buckets": [{"index": i, "samples": len(v), "mean_ms": sum(v)/len(v),
                                         "p95_ms": percentile(v, .95)} for i, v in sorted(buckets.items())],
                "cpu_accounting": "delta_kernel_plus_user_single_core_percent",
                "capture_process_cpu_percent": py_mean, "wechat_cpu_percent": wx_mean,
                "capture_process_cpu_last_window_percent": self.cpu_py,
                "capture_process_cpu_peak_window_percent": max((v[0] for v in self._cpu_windows), default=None),
                "wechat_incremental_cpu_percent": wx_increment,
                "wechat_last_window_incremental_cpu_percent": self.cpu_wechat_incremental,
                "cpu_observation_seconds": cpu_span if cpu_span else None,
                "cpu_acceptance": "pending_remeasurement_then_user_decision",
                "cpu_above_stop_reference": self.stop_required,
                "msg_max_sampled_byte_change_ratio": self.max_msg_change,
                "new_incoming_visibility": "not_verified",
                "degradation_acceptance": "requires_predeclared_separate_complete_10_minute_buckets",
                "real_capture_acceptance": "not_established_by_numeric_summary"}


class CaptureSampler:
    """One scheduled sample per explicit call; no background thread or autostart.

    Enforces pacing, records failures, and stops at the first CPU window >3%.
    Live callers still need an authorized, bounded worker. No sample is dropped
    to conceal a slow frame or incomplete cover. No CPU-based rate adjustment.
    """
    def __init__(self, adapter, *, metrics=None, clock=time.monotonic, cpu_window=30,
                 process_id=None, wechat_idle_baseline=None):
        if not _finite(cpu_window) or not 1 <= cpu_window <= 600:
            raise CaptureError("invalid_cpu_window")
        self.adapter = adapter
        self.metrics = metrics if metrics is not None else CaptureMetrics()
        self.clock, self.cpu_window = clock, cpu_window
        self.process_id = os.getpid() if process_id is None else process_id
        self.wechat_idle_baseline = wechat_idle_baseline
        self._cpu_anchor = None
        self.stopped = False

    def _cpu_sample(self):
        self.adapter._guard()
        api = self.adapter._native()
        py = api.process_times(self.process_id)
        wx = api.process_times(self.adapter.scope.pid)
        if wx[0] != self.adapter.scope.created:
            raise CaptureError("cpu_target_process_replaced")
        return self.clock(), py, wx

    def _update_cpu(self, *, final=False):
        current = self._cpu_sample()
        if self._cpu_anchor is None:
            self._cpu_anchor = current
            return
        when, py, wx = self._cpu_anchor
        elapsed = current[0]-when
        if elapsed > 0 and (final or elapsed >= self.cpu_window):
            self.metrics.note_cpu(cpu_percent(py, current[1], elapsed),
                                  cpu_percent(wx, current[2], elapsed), elapsed,
                                  wechat_idle_baseline=self.wechat_idle_baseline)
            self._cpu_anchor = current
            self.metrics.require_continue()

    def sample(self):
        if self.stopped:
            raise CaptureError("sampler_stopped_no_retry")
        try:
            self.metrics.require_continue()
            delay = self.adapter.next_delay()
        except Exception as error:
            self._abort(error, recorded=False)
        if delay > 0:
            raise CaptureError("sample_not_due")
        recorded = False
        try:
            if self._cpu_anchor is None:
                self._update_cpu()
            result = self.adapter.capture()
            self.metrics.add(result[1], self.adapter.last_frame_ms, self.adapter.last_coverage,
                             self.adapter.last_changes)
            recorded = True
            self._update_cpu()
            self.adapter._guard()
            return result
        except Exception as error:
            self._abort(error, recorded=recorded)

    def _abort(self, error, *, recorded):
        if recorded:
            self.metrics.post_capture_stops += 1
        else:
            self.metrics.failed_samples += 1
        self.stopped = True
        self.adapter.close()
        if isinstance(error, CaptureError):
            raise error from None
        raise CaptureError("sampling_failed") from None

    def finish(self):
        try:
            if not self.stopped and self._cpu_anchor is not None:
                self._update_cpu(final=True)
        except Exception as error:
            self.metrics.post_capture_stops += 1
            if isinstance(error, CaptureError):
                raise
            raise CaptureError("cpu_final_sample_failed") from None
        finally:
            self.stopped = True
            self.adapter.close()
        return self.metrics.summary()

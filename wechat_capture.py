"""Default-off, standard-library-only PrintWindow capture candidate.

Scope: D-003 plus the user's 2026-10-19 CPU/grid corrections. No sender,
OCR, model, desktop fallback, file output, window lookup or automatic loop.
Native PrintWindow is synchronous: live callers MUST use a bounded worker
process, with separate human authorization. A permission record is not consent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import threading
import time


FAILURE_STAGES = frozenset(("unclassified", "scope_validation", "backend_init", "thread_dpi",
    "window_metadata", "binding_validation", "capture", "cpu_finalize", "worker_cleanup",
    "thread_dpi_restore", "process_handle_release", "supervisor_setup", "supervisor_spawn",
    "supervisor_job_assign", "supervisor_monitor", "supervisor_cleanup"))
FAILURE_REASONS = frozenset(("unexpected_exception", "backend_library_load_failed",
    "backend_api_declaration_failed", "windows_only", "incomplete_native_fixture",
    "thread_dpi_query_failed", "physical_dpi_context_required", "thread_dpi_restore_failed",
    "window_handle_invalid", "window_not_visible", "window_minimized", "window_rect_query_failed",
    "window_pid_query_failed", "window_class_query_failed", "window_title_query_failed",
    "window_dpi_query_failed", "window_metadata_unavailable", "window_unavailable_or_minimized",
    "invalid_process_id", "process_query_denied", "process_times_failed", "process_handle_release_failed",
    "version_process_query_failed", "process_path_query_failed", "version_info_unavailable",
    "version_info_invalid", "version_signature_invalid", "invalid_window_scope", "invalid_window_geometry",
    "window_class_mismatch", "window_title_mismatch", "client_version_mismatch", "window_binding_changed",
    "capture_not_authorized_or_expired", "capture_not_authorized_or_closed", "capture_failed",
    "cpu_above_3_percent_stop_and_report", "cpu_final_sample_failed", "grid_hit_test_unknown",
    "grid_point_off_desktop", "windows_job_required", "job_creation_failed", "job_configuration_failed",
    "invalid_owned_child", "job_assignment_failed", "job_release_failed", "invalid_worker_protocol",
    "start_not_armed", "initialization_budget_exceeded", "capture_deadline_overrun",
    "run_cancelled", "capture_deadline_exceeded", "total_deadline_exceeded", "worker_abnormal_exit"))


class CaptureError(RuntimeError):
    """Compatibility message stays local; only allowlisted records cross IPC."""
    def __init__(self, code, *, stage="unclassified", win32_error=None, pid_matches_saved=None):
        super().__init__(code)
        self.stage = stage if stage in FAILURE_STAGES else "unclassified"
        self.reason = code if type(code) is str and code in FAILURE_REASONS else "unexpected_exception"
        self.cleanup_errors = []
        self.win32_error = (win32_error if type(win32_error) is int and 1 <= win32_error <= 0xffffffff else None)
        self.pid_matches_saved = pid_matches_saved if type(pid_matches_saved) is bool else None

    def at(self, stage):
        if self.stage == "unclassified":
            self.stage = stage if stage in FAILURE_STAGES else "unclassified"
        return self

    def record(self):
        return {"stage": self.stage, "reason": self.reason}

    def add_cleanup(self, error, stage):
        cleanup = capture_failure(error, stage)
        for record in [cleanup.record(), *cleanup.cleanup_errors]:
            if len(self.cleanup_errors) < 8:
                self.cleanup_errors.append(record.copy())
        return self


def capture_failure(error, stage):
    """Never exports str(error), exception attributes, paths or credentials."""
    return (error.at(stage) if isinstance(error, CaptureError)
            else CaptureError("unexpected_exception", stage=stage))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class WindowScope:
    hwnd: int = field(repr=False)
    pid: int = field(repr=False)
    created: int = field(repr=False)  # OS creation FILETIME: protects PID reuse.
    rect: tuple[int, int, int, int] = field(repr=False)
    dpi: int
    version: str = "4.1.15.13"
    class_name: str = "Qt51514QWindowIcon"
    title: str = "微信"

    def validate(self):
        if (any(type(n) is not int or n <= 0 for n in (self.hwnd, self.pid, self.created, self.dpi))
                or not isinstance(self.rect, tuple) or len(self.rect) != 4
                or any(type(n) is not int for n in self.rect)):
            raise CaptureError("invalid_window_scope", stage="binding_validation")
        for matches, reason in ((self.class_name == "Qt51514QWindowIcon", "window_class_mismatch"),
                                (self.title == "微信", "window_title_mismatch"),
                                (self.version == "4.1.15.13", "client_version_mismatch")):
            if not matches:
                raise CaptureError(reason, stage="binding_validation")
        l, t, r, b = self.rect
        if not (100 <= r-l <= 6000 and 100 <= b-t <= 4000 and (r-l)*(b-t) <= 16_000_000):
            raise CaptureError("invalid_window_geometry", stage="binding_validation")


@dataclass(frozen=True)
class CapturePermission:
    scope: WindowScope
    expires_at: float
    whole_window_memory: bool = False
    revoked: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def check(self, scope, now):
        if (self.scope != scope or self.whole_window_memory is not True
                or not _number(self.expires_at) or not _number(now)
                or now >= self.expires_at or self.revoked.is_set()):
            raise CaptureError("capture_not_authorized_or_expired")


@dataclass(frozen=True, repr=False)
class PixelImage:
    """Owned top-down BGRA bytes, not a PIL dependency. No save/encode API."""
    width: int
    height: int
    data: bytes

    def __post_init__(self):
        if (type(self.width) is not int or type(self.height) is not int
                or not 0 < self.width*self.height <= 16_000_000
                or self.width <= 0 or self.height <= 0 or type(self.data) is not bytes
                or len(self.data) != self.width*self.height*4):
            raise CaptureError("invalid_bgra_frame")

    @property
    def size(self):
        return self.width, self.height

    @property
    def mode(self):
        return "BGRA"

    def tobytes(self):
        return self.data

    def crop(self, box):
        l, t, r, b = box
        if (any(type(n) is not int for n in box)
                or not 0 <= l < r <= self.width or not 0 <= t < b <= self.height):
            raise CaptureError("invalid_region")
        stride = self.width*4
        return PixelImage(r-l, b-t, b"".join(
            self.data[y*stride+l*4:y*stride+r*4] for y in range(t, b)))


REGIONS = {"left": (0, 28, 0, 100), "head": (28, 100, 0, 10),
           "msg": (28, 100, 10, 80), "input": (28, 100, 80, 100)}

# A conversation moved only when pixels in the message region changed by more
# than repaint noise. Measured on this client (2026-10-19, 60 s, 39 intervals):
# msg noise peaked at 0.0006 while a real left-list update was 0.10. Chat-list
# timestamps, unread badges and caret blink must not raise the sample rate.
CHANGE_NOISE_FLOOR = 0.001
CHANGE_REGION = "msg"


def region_box(name, width, height):
    if name not in REGIONS:
        raise CaptureError("unknown_region")
    x0, x1, y0, y1 = REGIONS[name]
    return width*x0//100, height*y0//100, width*x1//100, height*y1//100


def sampled_region(image, name):
    """Match reference sampling: every fourth row, every third pixel's B byte.

    This is a region change metric, NOT full-pixel equality or message identity.
    It can miss a change between sample points. Never a draft-empty proof.
    """
    l, t, r, b = region_box(name, image.width, image.height)
    stride = image.width*4
    return b"".join(image.data[y*stride+l*4:y*stride+r*4:12] for y in range(t, b, 4))


def difference(first, second):
    if type(first) is not bytes or type(second) is not bytes or len(first) != len(second) or not first:
        raise CaptureError("incomparable_region_samples")
    if first == second:
        return 0.0
    return sum(a != b for a, b in zip(first, second)) / len(first)


@dataclass(frozen=True)
class Coverage:
    """A nine-point hit-test proxy, never proof of continuous opaque coverage."""
    covered: tuple[bool, ...]

    def __post_init__(self):
        if len(self.covered) != 9 or any(type(v) is not bool for v in self.covered):
            raise CaptureError("invalid_grid_result")

    @property
    def center_hit(self):
        return self.covered[4]

    @property
    def grid_covered(self):
        return all(self.covered)


def grid_points(rect):
    l, t, r, b = rect
    # 10%, 50%, 90%, kept inside even with negative monitor coordinates.
    return tuple((l+min(r-l-1, (r-l)*x//10), t+min(b-t-1, (b-t)*y//10))
                 for y in (1, 5, 9) for x in (1, 5, 9))


class AdaptiveInterval:
    def __init__(self, idle=1.5, active=0.4, active_seconds=3.0):
        if (not all(_number(v) for v in (idle, active, active_seconds))
                or not 1.5 <= idle <= 2 or not 0.3 <= active <= 0.5 or not 0 < active_seconds <= 30):
            raise CaptureError("invalid_sampling_policy")
        self.idle, self.active, self.active_seconds = idle, active, active_seconds
        self.active_until = float("-inf")

    def observe(self, now, changed):
        if changed:
            self.active_until = now+self.active_seconds

    def interval(self, now):
        return self.active if now < self.active_until else self.idle


class CaptureAdapter:
    """The three required interfaces operate only on an explicitly bound HWND.

    Construction/import is inert. region() returns this capture's owned pixels,
    never initiates another capture. A failed/revoked capture invalidates cache.
    No seq/hash/callback is promoted into new-message or freshness evidence.
    """
    def __init__(self, scope, permission=None, *, backend=None, clock=time.monotonic, policy=None):
        scope.validate()
        self.scope, self.permission, self.backend, self.clock = scope, permission, backend, clock
        self.policy = policy if policy is not None else AdaptiveInterval()
        self._frame = None
        self._samples = None
        self._seq = 0
        self._closed = False
        self._started = None
        self._lock = threading.RLock()
        self.last_coverage = None
        self.last_changes = None
        self.last_frame_ms = None
        self.last_coverage_changed = False

    def _guard(self):
        if self._closed or not isinstance(self.permission, CapturePermission):
            raise CaptureError("capture_not_authorized_or_closed")
        self.permission.check(self.scope, self.clock())

    def _native(self):
        self._guard()
        if self.backend is None:
            from capture_gdi import NativeGDI
            self.backend = NativeGDI()
        return self.backend

    def _check_window(self, api):
        self._guard()
        if api.describe(self.scope.hwnd) != self.scope:
            raise CaptureError("window_binding_changed")

    def _coverage(self, api):
        values = []
        for point in grid_points(self.scope.rect):
            self._guard()
            root = api.root_from_point(point)
            # NULL/desktop/outside-screen/unreadable is unknown, not occluded.
            if type(root) is not int or root <= 0:
                raise CaptureError("grid_hit_test_unknown")
            values.append(root != self.scope.hwnd)
        return Coverage(tuple(values))

    def capture(self):
        with self._lock:
            self._frame = self.last_coverage = self.last_changes = self.last_frame_ms = None
            self.last_coverage_changed = False
            started = self.clock()
            self._started = started
            try:
                api = self._native()
                self._check_window(api)
                before = self._coverage(api)
                l, t, r, b = self.scope.rect
                frame = api.grab(self.scope.hwnd, r-l, b-t)
                captured_at = self.clock()  # Acquisition time, not later OCR/analysis end.
                if not isinstance(frame, PixelImage) or frame.size != (r-l, b-t):
                    raise CaptureError("invalid_native_frame")
                self._check_window(api)
                after = self._coverage(api)
                # This is capture-only: a cover transition is measured, not
                # omitted from the denominator. Require covered at BOTH ends.
                coverage = Coverage(tuple(a and b for a, b in zip(before.covered, after.covered)))
                samples = {name: sampled_region(frame, name) for name in REGIONS}
                changes = ({name: difference(self._samples[name], value) for name, value in samples.items()}
                           if self._samples is not None else None)
                self._guard()
                ended = self.clock()
                if not started <= captured_at <= ended:
                    raise CaptureError("capture_clock_invalid")
                # Only a message-region change above the noise floor means the
                # bound conversation moved. Everything else is background UI.
                self.policy.observe(ended, changes is not None
                                    and changes.get(CHANGE_REGION, 0.0) > CHANGE_NOISE_FLOOR)
                self._seq += 1
                self._frame, self._samples = frame, samples
                self.last_coverage, self.last_changes = coverage, changes
                self.last_coverage_changed = before != after
                self.last_frame_ms = (ended-started)*1000  # Entire public capture(), not only PrintWindow.
                return frame, captured_at, self._seq
            except Exception as error:
                self._samples = None
                self._frame = None
                if isinstance(error, CaptureError):
                    raise
                raise CaptureError("capture_failed") from None

    def is_window_occluded(self):
        with self._lock:
            try:
                api = self._native()
                self._check_window(api)
                coverage = self._coverage(api)
                self._check_window(api)
                self.last_coverage = coverage
                return coverage.grid_covered
            except Exception as error:
                self._frame = self._samples = None
                if isinstance(error, CaptureError):
                    raise
                raise CaptureError("coverage_failed") from None

    def region(self, name):
        with self._lock:
            try:
                self._guard()
            except CaptureError:
                self._frame = self._samples = None
                raise
            if self._frame is None:
                raise CaptureError("no_successful_current_frame")
            return self._frame.crop(region_box(name, self._frame.width, self._frame.height))

    def next_delay(self):
        """Start-to-start pacing. No CPU input, load throttling or auto-retry."""
        self._guard()
        now = self.clock()
        if self._started is None:
            return 0.0
        return max(0.0, self.policy.interval(now)-(now-self._started))

    def close(self):
        # Do not wait on an uninterruptible PrintWindow call to revoke permission.
        self._closed = True
        if isinstance(self.permission, CapturePermission):
            self.permission.revoked.set()
        self._frame = self._samples = None


def main():
    import json
    print(json.dumps({"status": "not_started", "backend": "PrintWindow_flag_2",
                      "real_capture_runs": 0, "native_loaded": False, "model_calls": 0,
                      "inputs": 0, "sends": 0, "frame_files_written": 0}))


if __name__ == "__main__":
    main()

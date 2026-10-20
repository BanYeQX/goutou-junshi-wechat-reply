"""Generated pixels/fake GDI only. Never queries or captures the real desktop."""
from dataclasses import replace
import ast
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wechat_capture import (CaptureAdapter, CaptureError, CapturePermission, WindowScope,
                            PixelImage, Coverage, AdaptiveInterval, difference, grid_points,
                            region_box, sampled_region, CHANGE_NOISE_FLOOR)
from capture_gdi import NativeGDI, FixedVersion
from capture_metrics import CaptureMetrics, CaptureSampler, cpu_percent


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    clock = [100.0]
    scope = WindowScope(123, 456, 999, (0, 0, 400, 300), 96)
    frame = PixelImage(400, 300, bytes((10, 20, 30, 0))*120000)
    backend = SimpleNamespace(describe=Mock(return_value=scope), grab=Mock(return_value=frame),
                              root_from_point=Mock(return_value=777))
    permission = CapturePermission(scope, 1000, True)
    adapter = CaptureAdapter(scope, permission, backend=backend, clock=lambda: clock[0])
    return adapter, backend, clock, permission, frame


class AdapterTests(unittest.TestCase):
    def test_inert_constructor_and_missing_permission_never_construct_native(self):
        adapter, backend, clock, permission, frame = fixture()
        backend.describe.assert_not_called()
        with patch('capture_gdi.NativeGDI') as factory:
            adapter = CaptureAdapter(adapter.scope, clock=lambda: clock[0])
            with self.assertRaises(CaptureError):
                adapter.capture()
            factory.assert_not_called()

    def test_capture_required_tuple_and_region_not_new_capture(self):
        adapter, backend, clock, permission, frame = fixture()
        self.assertEqual(adapter.capture(), (frame, 100.0, 1))
        self.assertEqual(adapter.region('left').size, (112, 300))
        self.assertEqual(adapter.region('head').size, (288, 30))
        self.assertEqual(adapter.region('msg').size, (288, 210))
        self.assertEqual(adapter.region('input').size, (288, 60))
        backend.grab.assert_called_once_with(123, 400, 300)
        self.assertEqual(backend.root_from_point.call_count, 18)
        self.assertEqual(adapter.region('msg').data[:4], bytes((10, 20, 30, 0)))
        self.assertNotIn('data=', repr(frame))

    def test_negative_monitor_coordinates_and_nine_distinct_interior_points(self):
        points = grid_points((-800, -600, -400, -300))
        self.assertEqual(len(set(points)), 9)
        self.assertTrue(all(-800 <= x < -400 and -600 <= y < -300 for x, y in points))
        self.assertEqual(points[4], (-600, -450))

    def test_bool_means_all_nine_not_just_center(self):
        adapter, backend, *_ = fixture()
        backend.root_from_point.side_effect = [123]+[777]*8
        self.assertFalse(adapter.is_window_occluded())
        self.assertTrue(adapter.last_coverage.center_hit)
        self.assertFalse(adapter.last_coverage.grid_covered)

    def test_same_root_child_is_not_occluder(self):
        # root normalization belongs to NativeGDI; adapter receives that root.
        adapter, backend, *_ = fixture()
        backend.root_from_point.return_value = adapter.scope.hwnd
        self.assertFalse(adapter.is_window_occluded())

    def test_null_unknown_coverage_does_not_return_frame(self):
        for hits in ([0]*9, [None]*9):
            adapter, backend, *_ = fixture()
            backend.root_from_point.side_effect = hits
            with self.assertRaises(CaptureError):
                adapter.capture()
            self.assertIsNone(adapter._frame)

    def test_cover_transition_is_not_silently_discarded_from_statistics(self):
        adapter, backend, *_ = fixture()
        backend.root_from_point.side_effect = [777]*9+[123]+[777]*8
        adapter.capture()
        self.assertTrue(adapter.last_coverage_changed)
        metrics = CaptureMetrics()
        metrics.add(0, 20, adapter.last_coverage)
        self.assertEqual(metrics.summary()['center_hit_ratio'], 1)
        self.assertEqual(metrics.summary()['grid_covered_ratio'], 0)
        self.assertTrue(metrics.summary()['ever_not_fully_covered'])

    def test_binding_geometry_pid_creation_dpi_version_title_changes_fail(self):
        adapter, backend, *_ = fixture()
        for field, value in (('rect', (0, 0, 401, 300)), ('pid', 789), ('created', 888),
                             ('dpi', 144), ('version', '4.1.15.53'), ('title', '其他')):
            backend.describe.return_value = replace(adapter.scope, **{field: value})
            with self.assertRaisesRegex(CaptureError, 'binding_changed'):
                adapter.capture()
        backend.grab.assert_not_called()

    def test_no_stale_cached_region_after_failure_and_no_sequence_increment(self):
        adapter, backend, clock, *_ = fixture()
        self.assertEqual(adapter.capture()[2], 1)
        backend.grab.side_effect = OSError('PRIVATE_NATIVE_PATH')
        with self.assertRaisesRegex(CaptureError, '^capture_failed$'):
            adapter.capture()
        with self.assertRaisesRegex(CaptureError, 'no_successful'):
            adapter.region('msg')
        backend.grab.side_effect = None
        clock[0] += 1.5
        self.assertEqual(adapter.capture()[2], 2)
        self.assertIsNone(adapter.last_changes)  # Observation gap is not ignored.

    def test_revocation_expiry_and_close_discard_cached_pixels(self):
        for action in ('revoke', 'expire', 'close'):
            adapter, backend, clock, permission, _ = fixture()
            adapter.capture()
            if action == 'revoke':
                permission.revoked.set()
            elif action == 'expire':
                clock[0] = 1000
            else:
                adapter.close()
            with self.assertRaises(CaptureError):
                adapter.region('input')
            with self.assertRaises(CaptureError):
                adapter.capture()
            self.assertEqual(backend.grab.call_count, 1)

    def test_revocation_during_native_call_drops_result(self):
        adapter, backend, _, permission, frame = fixture()
        def grab(*_):
            permission.revoked.set()
            return frame
        backend.grab.side_effect = grab
        with self.assertRaises(CaptureError):
            adapter.capture()
        self.assertIsNone(adapter._frame)
        self.assertEqual(adapter._seq, 0)

    def test_region_before_capture_or_unknown_name_refuses(self):
        adapter, *_ = fixture()
        with self.assertRaises(CaptureError):
            adapter.region('msg')
        adapter.capture()
        with self.assertRaisesRegex(CaptureError, 'unknown_region'):
            adapter.region('desktop')

    def test_sampling_matches_reference_and_not_a_draft_or_identity_proof(self):
        _, _, _, _, frame = fixture()
        l, t, r, b = region_box('msg', frame.width, frame.height)
        expected = b''.join(frame.data[y*frame.width*4+l*4:y*frame.width*4+r*4:12]
                            for y in range(t, b, 4))
        self.assertEqual(sampled_region(frame, 'msg'), expected)
        self.assertEqual(difference(b'abcd', b'abxy'), .5)
        for a, b in ((b'', b''), (b'a', b'ab')):
            with self.assertRaises(CaptureError):
                difference(a, b)

    def test_adaptive_capture_uses_only_functional_changes_and_fixed_intervals(self):
        adapter, backend, clock, _, frame = fixture()
        adapter.capture()
        self.assertEqual(adapter.next_delay(), 1.5)
        clock[0] += 1.5
        adapter.capture()
        self.assertEqual(adapter.next_delay(), 1.5)
        backend.grab.return_value = PixelImage(frame.width, frame.height, bytes((11, 20, 30, 0))*120000)
        clock[0] += 1.5
        adapter.capture()
        self.assertEqual(adapter.next_delay(), .4)
        self.assertEqual(adapter.last_changes['msg'], 1)
        clock[0] += 4
        self.assertEqual(adapter.policy.interval(clock[0]), 1.5)

    def test_only_message_region_above_noise_floor_raises_the_rate(self):
        adapter, backend, clock, _, frame = fixture()
        adapter.capture()
        base = bytearray(frame.data)

        def painted(x0, x1, y0, y1):
            data = bytearray(base)
            for y in range(y0, y1):
                row = y*frame.width*4
                for x in range(x0, x1):
                    data[row + x*4] ^= 0xFF
            return PixelImage(frame.width, frame.height, bytes(data))

        # A chat-list row changed (unread badge / timestamp): background UI only.
        backend.grab.return_value = painted(0, 112, 0, 300)
        clock[0] += 1.5
        adapter.capture()
        self.assertGreater(adapter.last_changes['left'], .05)
        self.assertEqual(adapter.last_changes['msg'], 0)
        self.assertEqual(adapter.next_delay(), 1.5)

        # One repaint pixel inside the message region is noise, not a message.
        backend.grab.return_value = painted(115, 118, 102, 106)
        clock[0] += 1.5
        adapter.capture()
        self.assertGreater(adapter.last_changes['msg'], 0)
        self.assertLess(adapter.last_changes['msg'], CHANGE_NOISE_FLOOR)
        self.assertEqual(adapter.next_delay(), 1.5)

        # A bubble-sized change in the message region raises the rate.
        backend.grab.return_value = painted(150, 270, 60, 120)
        clock[0] += 1.5
        adapter.capture()
        self.assertGreater(adapter.last_changes['msg'], CHANGE_NOISE_FLOOR)
        self.assertEqual(adapter.next_delay(), .4)

    def test_invalid_sampling_parameters_not_silently_clamped(self):
        for kw in ({'idle': 3}, {'active': 1}, {'idle': float('nan')}, {'active_seconds': 0}):
            with self.assertRaises(CaptureError):
                AdaptiveInterval(**kw)

    def test_invalid_frames_rejected_without_caching(self):
        for args in ((0, 1, b''), (1, 1, b'bad'), (1, 1, bytearray(4)), (True, 1, bytes(4))):
            with self.assertRaises(CaptureError):
                PixelImage(*args)

    def test_default_command_no_config_dlls_frames_or_vendor_runtime(self):
        code = ("import runpy,sys;runpy.run_module('wechat_capture',run_name='__main__');"
                "print([n for n in ('capture_gdi','ctypes','PIL','numpy','win32gui','chat_core','app',"
                "'exclusive_sender','socket') if n in sys.modules])")
        run = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                             capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(run.stdout.splitlines()[0])['real_capture_runs'], 0)
        self.assertEqual(run.stdout.splitlines()[1], '[]')

    def test_new_modules_have_only_stdlib_or_local_capture_imports_and_no_mutators(self):
        allowed = {'__future__', 'dataclasses', 'math', 'threading', 'time', 'ctypes', 'os', 'json',
                   'contextlib', 'functools',
                   'wechat_capture', 'capture_gdi'}
        banned = {'SetWindowPos', 'SetForegroundWindow', 'ShowWindow', 'SendInput', 'keybd_event',
                  'ReadProcessMemory', 'WriteProcessMemory', 'CreateRemoteThread', 'SetProcessDpiAwareness',
                  'SetProcessDpiAwarenessContext', 'BitBlt', 'GetDC', 'open', 'write_text', 'write_bytes', 'save'}
        for filename in ('wechat_capture.py', 'capture_gdi.py', 'capture_metrics.py'):
            tree = ast.parse((ROOT/filename).read_text(encoding='utf-8'))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add(node.module.split('.')[0])
            self.assertFalse(imports-allowed, filename)
            calls = {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
                     for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and isinstance(n.func, (ast.Name, ast.Attribute))}
            self.assertFalse(calls & banned, filename)


def native_fixture():
    user, gdi, kernel, version = Mock(), Mock(), Mock(), Mock()
    user.GetThreadDpiAwarenessContext.return_value = 20
    user.GetAwarenessFromDpiAwarenessContext.return_value = 2
    user.AreDpiAwarenessContextsEqual.return_value = 1
    user.GetWindowDC.return_value = 10
    user.PrintWindow.return_value = 1
    user.ReleaseDC.return_value = 1
    gdi.CreateCompatibleDC.return_value = 20
    gdi.CreateCompatibleBitmap.return_value = 30
    gdi.SelectObject.return_value = 40
    gdi.PatBlt.return_value = 1
    gdi.DeleteObject.return_value = gdi.DeleteDC.return_value = 1
    def pixels(dc, bitmap, start, height, target, info, mode):
        header = info._obj.header
        C.memmove(target, bytes((1, 2, 3, 0))*header.width*height, header.width*height*4)
        return height
    gdi.GetDIBits.side_effect = pixels
    kernel.OpenProcess.return_value = 50
    kernel.CloseHandle.return_value = 1
    return NativeGDI(user=user, gdi=gdi, kernel=kernel, version=version), user, gdi, kernel, version


class NativeTests(unittest.TestCase):
    def test_native_signatures_pointer_sized_handles_and_cpu_argument_order(self):
        user, gdi, kernel, version = Mock(), Mock(), Mock(), Mock()
        NativeGDI._declare(user, gdi, kernel, version)
        self.assertIs(kernel.OpenProcess.restype, W.HANDLE)
        self.assertEqual(kernel.GetProcessTimes.argtypes, [W.HANDLE]+[C.POINTER(W.FILETIME)]*4)
        self.assertEqual(user.PrintWindow.argtypes, [W.HWND, W.HDC, W.UINT])
        self.assertEqual(gdi.GetDIBits.argtypes[0:4], [W.HDC, W.HBITMAP, W.UINT, W.UINT])

    def test_gdi_pixel_orientation_flag_only_and_all_handles_released(self):
        native, user, gdi, *_ = native_fixture()
        frame = native.grab(123, 10, 4)
        self.assertEqual(frame.size, (10, 4))
        self.assertEqual(frame.data, bytes((1, 2, 3, 0))*40)
        user.PrintWindow.assert_called_once_with(123, 20, 2)
        header = gdi.GetDIBits.call_args.args[5]._obj.header
        self.assertEqual(header.height, -4)
        self.assertEqual(gdi.SelectObject.call_args_list[1].args, (20, 40))
        gdi.DeleteObject.assert_called_once_with(30)
        gdi.DeleteDC.assert_called_once_with(20)
        user.ReleaseDC.assert_called_once_with(123, 10)
        user.GetDC.assert_not_called()

    def test_failure_at_each_gdi_step_no_retry_and_all_possible_cleanup(self):
        for stage in ('source', 'memory', 'bitmap', 'select', 'clear', 'print', 'restore', 'pixels'):
            native, user, gdi, *_ = native_fixture()
            if stage == 'source': user.GetWindowDC.return_value = 0
            if stage == 'memory': gdi.CreateCompatibleDC.return_value = 0
            if stage == 'bitmap': gdi.CreateCompatibleBitmap.return_value = 0
            if stage == 'select': gdi.SelectObject.return_value = 0
            if stage == 'clear': gdi.PatBlt.return_value = 0
            if stage == 'print': user.PrintWindow.return_value = 0
            if stage == 'restore': gdi.SelectObject.side_effect = [40, 0, 40]
            if stage == 'pixels': gdi.GetDIBits.side_effect = lambda *_: 3
            with self.subTest(stage=stage), self.assertRaises(CaptureError):
                native.grab(123, 10, 4)
            self.assertLessEqual(user.PrintWindow.call_count, 1)
            if stage != 'source': user.ReleaseDC.assert_called_once()
            if stage not in ('source', 'memory'): gdi.DeleteDC.assert_called_once()
            if stage not in ('source', 'memory', 'bitmap'): gdi.DeleteObject.assert_called_once()

    def test_cleanup_exception_does_not_skip_remaining_releases(self):
        native, user, gdi, *_ = native_fixture()
        gdi.DeleteObject.side_effect = OSError('private path')
        with self.assertRaisesRegex(CaptureError, '^native_cleanup_failed$'):
            native.grab(123, 10, 4)
        gdi.DeleteDC.assert_called_once()
        user.ReleaseDC.assert_called_once()

    def test_cpu_getprocesstimes_order_counts_kernel_plus_user_not_creation(self):
        native, _, _, kernel, _ = native_fixture()
        def times(handle, creation, exit_time, kernel_time, user_time):
            for pointer, value in ((creation, 1 << 40), (exit_time, 0),
                                   (kernel_time, 20_000_000), (user_time, 30_000_000)):
                pointer._obj.dwLowDateTime = value & 0xffffffff
                pointer._obj.dwHighDateTime = value >> 32
            return 1
        kernel.GetProcessTimes.side_effect = times
        self.assertEqual(native.process_times(456), (1 << 40, 20_000_000, 30_000_000))
        self.assertEqual(native.cpu_seconds(456), 5)
        self.assertEqual(kernel.OpenProcess.call_args.args, (0x1000, False, 456))
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_cpu_query_failure_is_unknown_not_zero_and_handle_closed(self):
        native, _, _, kernel, _ = native_fixture()
        kernel.GetProcessTimes.return_value = 0
        with self.assertRaisesRegex(CaptureError, 'process_times_failed'):
            native.cpu_seconds(456)
        kernel.CloseHandle.assert_called_once()

    def test_child_hit_is_normalized_to_root_and_desktop_or_offscreen_rejected(self):
        native, user, *_ = native_fixture()
        user.GetSystemMetrics.side_effect = lambda n: {76: -100, 77: 0, 78: 500, 79: 400}[n]
        user.WindowFromPoint.return_value = 456
        user.GetAncestor.return_value = 123
        user.GetDesktopWindow.return_value = 1
        self.assertEqual(native.root_from_point((0, 0)), 123)
        user.GetAncestor.assert_called_once_with(456, 2)
        with self.assertRaisesRegex(CaptureError, 'off_desktop'):
            native.root_from_point((1000, 1000))
        user.GetAncestor.return_value = 1
        with self.assertRaisesRegex(CaptureError, 'unknown'):
            native.root_from_point((0, 0))

    def test_describe_checks_physical_geometry_version_and_original_process(self):
        native, user, _, kernel, _ = native_fixture()
        user.GetThreadDpiAwarenessContext.return_value = 20
        user.GetAwarenessFromDpiAwarenessContext.return_value = 2
        user.IsWindow.return_value = user.IsWindowVisible.return_value = 1
        user.IsIconic.return_value = 0
        def rect(_hwnd, ptr):
            ptr._obj.left, ptr._obj.top, ptr._obj.right, ptr._obj.bottom = 0, 0, 400, 300
            return 1
        def pid(_hwnd, ptr):
            ptr._obj.value = 456
            return 999
        def text(_hwnd, buffer, size):
            buffer.value = '微信'
            return 2
        def name(_hwnd, buffer, size):
            buffer.value = 'Qt51514QWindowIcon'
            return len(buffer.value)
        user.GetWindowRect.side_effect = rect
        user.GetWindowThreadProcessId.side_effect = pid
        user.GetWindowTextW.side_effect = text
        user.GetClassNameW.side_effect = name
        user.GetDpiForWindow.return_value = 96
        native.process_times = Mock(return_value=(999, 0, 0))
        native._file_version = Mock(return_value='4.1.15.13')
        self.assertEqual(native.describe(123), WindowScope(123, 456, 999, (0, 0, 400, 300), 96))
        user.IsIconic.return_value = 1
        with self.assertRaisesRegex(CaptureError, 'minimized'):
            native.describe(123)
        user.IsIconic.return_value = 0
        user.GetAwarenessFromDpiAwarenessContext.return_value = 0
        with self.assertRaisesRegex(CaptureError, 'physical_dpi_context_required'):
            native.describe(123)
        user.SetProcessDpiAwareness.assert_not_called()

    def test_file_version_read_and_metadata_cache_not_a_frame_cache(self):
        native, _, _, kernel, version = native_fixture()
        def path(handle, flags, buffer, length):
            buffer.value = 'C:\\fixture\\Weixin.exe'
            return 1
        kernel.QueryFullProcessImageNameW.side_effect = path
        version.GetFileVersionInfoSizeW.return_value = 128
        version.GetFileVersionInfoW.return_value = 1
        info = FixedVersion()
        info.signature, info.file_ms, info.file_ls = 0xFEEF04BD, (4 << 16) | 1, (15 << 16) | 13
        def query(buffer, key, pointer, length):
            self.assertEqual(key, '\\')
            pointer._obj.value, length._obj.value = C.addressof(info), C.sizeof(info)
            return 1
        version.VerQueryValueW.side_effect = query
        self.assertEqual(native._file_version(456, 999), '4.1.15.13')
        self.assertEqual(native._file_version(456, 999), '4.1.15.13')
        self.assertEqual(version.GetFileVersionInfoW.call_count, 1)
        self.assertEqual(native._file_version(456, 1000), '4.1.15.13')
        self.assertEqual(version.GetFileVersionInfoW.call_count, 2)
        kernel.CloseHandle.assert_called()

    def test_bad_version_pointer_or_signature_is_not_accepted(self):
        native, _, _, kernel, version = native_fixture()
        def path(handle, flags, buffer, length):
            buffer.value = 'C:\\fixture\\Weixin.exe'
            return 1
        kernel.QueryFullProcessImageNameW.side_effect = path
        version.GetFileVersionInfoSizeW.return_value = 128
        version.GetFileVersionInfoW.return_value = 1
        version.VerQueryValueW.return_value = 0
        with self.assertRaisesRegex(CaptureError, 'version_info_invalid'):
            native._file_version(456, 999)

    def test_thread_local_physical_context_restored_on_exception_without_system_changes(self):
        native, user, *_ = native_fixture()
        user.GetAwarenessFromDpiAwarenessContext.side_effect = [0, 2]
        user.SetThreadDpiAwarenessContext.side_effect = [101, 202]
        with self.assertRaisesRegex(CaptureError, 'synthetic'):
            with native.physical_pixels():
                raise CaptureError('synthetic')
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_count, 2)
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_args.args, (101,))
        user.SetProcessDpiAwareness.assert_not_called()
        user.SetWindowPos.assert_not_called()

    def test_thread_context_restore_failure_is_not_success(self):
        native, user, *_ = native_fixture()
        user.GetAwarenessFromDpiAwarenessContext.side_effect = [0, 2]
        user.SetThreadDpiAwarenessContext.side_effect = [101, 0]
        with self.assertRaisesRegex(CaptureError, 'thread_dpi_restore_failed'):
            with native.physical_pixels():
                pass



class MetricTests(unittest.TestCase):
    def test_cpu_delta_both_modes_and_single_core_not_divided_by_machine_cores(self):
        self.assertEqual(cpu_percent((999, 0, 0), (999, 20_000_000, 30_000_000), 10), 50)
        self.assertEqual(cpu_percent((999, 0, 0), (999, 20_000_000, 30_000_000), 2), 250)
        for before, after, elapsed in (((1, 0, 0), (2, 0, 0), 10),
                                       ((1, 5, 0), (1, 4, 0), 10), ((1, 0, 0), (1, 0, 0), 0)):
            with self.assertRaises(CaptureError): cpu_percent(before, after, elapsed)

    def test_grid_center_separate_and_any_incomplete_latched_even_if_recovered(self):
        metrics = CaptureMetrics()
        bad_corner = Coverage((False,)+(True,)*8)
        good = Coverage((True,)*9)
        for t, grid in ((0, good), (2, bad_corner), (4, bad_corner), (6, good), (8, bad_corner)):
            metrics.add(t, 20, grid)
        report = metrics.summary()
        self.assertEqual(report['center_hit_ratio'], 1)
        self.assertEqual(report['grid_covered_ratio'], .4)
        self.assertTrue(report['ever_not_fully_covered'])
        self.assertEqual(report['not_fully_covered_samples'], 3)
        self.assertEqual(report['first_incomplete_at'], 2)
        self.assertEqual(report['last_incomplete_at'], 8)
        self.assertEqual(report['longest_incomplete_run_samples'], 2)
        self.assertEqual(report['longest_incomplete_observation_span_seconds'], 2)

    def test_cpu_above_reference_latches_stop_not_new_limit_or_rate_reduction(self):
        metrics, policy = CaptureMetrics(), AdaptiveInterval()
        metrics.note_cpu(12, 7, 60)
        self.assertIsNone(metrics.summary()['wechat_incremental_cpu_percent'])
        with self.assertRaisesRegex(CaptureError, 'stop_and_report'):
            metrics.require_continue()
        metrics.note_cpu(1, 2, 120, wechat_idle_baseline=1)
        self.assertTrue(metrics.summary()['cpu_above_stop_reference'])
        self.assertIsNone(metrics.summary()['wechat_incremental_cpu_percent'])
        self.assertEqual(metrics.summary()['wechat_last_window_incremental_cpu_percent'], 1)
        self.assertIn('pending', metrics.summary()['cpu_acceptance'])
        self.assertEqual((policy.idle, policy.active), (1.5, .4))

    def test_numeric_report_preserves_p95_slow_frames_and_no_incoming_claim(self):
        metrics = CaptureMetrics()
        for i in range(100):
            metrics.add(i*1.5, i+1, Coverage((True,)*9),
                        {'left': 0, 'head': 0, 'msg': .0947 if i else 0, 'input': 0})
        report = metrics.summary()
        self.assertEqual(report['frame_p95_ms'], 95)
        self.assertEqual(report['msg_max_sampled_byte_change_ratio'], .0947)
        self.assertEqual(report['new_incoming_visibility'], 'not_verified')
        self.assertEqual(report['slow_frames_over_400_ms'], 0)
        self.assertEqual(CaptureMetrics().summary()['grid_covered_ratio'], None)

    def test_metric_bounds_unknown_values_and_no_image_payloads(self):
        metrics = CaptureMetrics(max_samples=1)
        metrics.add(0, 400.01, Coverage((True,)*9))
        self.assertEqual(metrics.summary()['slow_frames_over_400_ms'], 1)
        with self.assertRaisesRegex(CaptureError, 'budget'):
            metrics.add(1, 1, Coverage((True,)*9))
        with self.assertRaises(CaptureError):
            CaptureMetrics().add(0, float('nan'), Coverage((True,)*9))


class SamplerTests(unittest.TestCase):
    def sampler(self):
        adapter, backend, clock, permission, frame = fixture()
        cpu = {'own': 0, 'wx': 0}
        backend.process_times = Mock(side_effect=lambda pid:
            (111 if pid == 888 else 999, 0, cpu['own'] if pid == 888 else cpu['wx']))
        sampler = CaptureSampler(adapter, clock=lambda: clock[0], cpu_window=30, process_id=888)
        return sampler, adapter, backend, clock, cpu

    def test_cpu_high_stops_sampler_before_next_frame_without_rate_reduction(self):
        sampler, adapter, backend, clock, cpu = self.sampler()
        sampler.sample()
        clock[0] += 30
        cpu['own'] = 60_000_000  # 6 user-mode CPU seconds / 30 seconds -> 20%.
        with self.assertRaisesRegex(CaptureError, 'stop_and_report'):
            sampler.sample()
        report = sampler.metrics.summary()
        self.assertEqual(report['capture_process_cpu_percent'], 20)
        self.assertEqual(report['samples'], 2)
        self.assertEqual(report['failed_samples'], 0)
        self.assertEqual(report['post_capture_stops'], 1)
        self.assertTrue(sampler.stopped)
        self.assertEqual((adapter.policy.idle, adapter.policy.active), (1.5, .4))
        with self.assertRaisesRegex(CaptureError, 'no_retry'):
            sampler.sample()
        self.assertEqual(backend.grab.call_count, 2)

    def test_scheduler_does_not_capture_early_or_retry_after_native_failure(self):
        sampler, adapter, backend, clock, cpu = self.sampler()
        sampler.sample()
        with self.assertRaisesRegex(CaptureError, 'not_due'):
            sampler.sample()
        self.assertEqual(backend.grab.call_count, 1)
        clock[0] += 1.5
        backend.grab.side_effect = OSError('private error')
        with self.assertRaises(CaptureError): sampler.sample()
        self.assertTrue(sampler.stopped)
        with self.assertRaisesRegex(CaptureError, 'no_retry'): sampler.sample()
        self.assertEqual(sampler.metrics.failed_samples, 1)

    def test_final_partial_cpu_window_is_included_without_inventing_wechat_increment(self):
        sampler, adapter, backend, clock, cpu = self.sampler()
        sampler.sample()
        clock[0] += 10
        cpu.update(own=1_000_000, wx=2_000_000)
        report = sampler.finish()
        self.assertEqual(report['capture_process_cpu_percent'], 1)
        self.assertEqual(report['wechat_cpu_percent'], 2)
        self.assertIsNone(report['wechat_incremental_cpu_percent'])
        self.assertEqual(report['cpu_observation_seconds'], 10)
        self.assertTrue(adapter._closed)

    def test_cpu_failure_does_not_leak_details_or_continue_capturing(self):
        sampler, adapter, backend, clock, cpu = self.sampler()
        backend.process_times.side_effect = OSError('PRIVATE_PATH_OR_TEXT')
        with self.assertRaisesRegex(CaptureError, '^sampling_failed$'):
            sampler.sample()
        backend.grab.assert_not_called()
        self.assertTrue(adapter._closed)

    def test_incomplete_coverage_kept_in_sampler_denominator(self):
        sampler, adapter, backend, clock, cpu = self.sampler()
        backend.root_from_point.side_effect = [777]*9+[123]+[777]*8
        sampler.sample()
        report = sampler.metrics.summary()
        self.assertEqual(report['samples'], 1)
        self.assertEqual(report['grid_covered_ratio'], 0)
        self.assertTrue(report['ever_not_fully_covered'])

    def test_expired_between_samples_stops_and_clears_pixels_before_cpu_or_capture(self):
        sampler, adapter, backend, clock, cpu = self.sampler()
        sampler.sample()
        cpu_calls = backend.process_times.call_count
        clock[0] = 1001
        with self.assertRaisesRegex(CaptureError, 'expired'):
            sampler.sample()
        self.assertTrue(sampler.stopped)
        self.assertIsNone(adapter._frame)
        self.assertEqual(backend.process_times.call_count, cpu_calls)
        self.assertEqual(backend.grab.call_count, 1)


if __name__ == '__main__':
    unittest.main()

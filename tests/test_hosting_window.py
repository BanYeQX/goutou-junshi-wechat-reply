"""Offline only: fake runner/native adapters, temporary data, own Tk widgets."""
import ast
import copy
import ctypes
import gc
import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import app
from chat_core import DEFAULT_SETTINGS, DIRECT_WRITER, Store, new_profile, prompt_for
from hosting_ledger import Ledger
from hosting_loop import Decision
import hosting_run
from hosting_session import Reading
from hosting_window import HostingController, HostingWindow


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.cfg = {"profiles": [{"id": "a", "name": "合成甲"}, {"id": "b", "name": "合成乙"}], "selected": "a"}
        self.ready = threading.Event()
        self.calls = []

        def runner(*args, log, stop_event, **kwargs):
            self.calls.append((args, kwargs))
            log('@hosting {"kind":"system","text":"合成就绪","state":"托管中"}', flush=True)
            self.ready.set()
            stop_event.wait(3)
            return 0

        self.c = HostingController(config=lambda: self.cfg, select=lambda i: self.cfg.update(selected=i),
                                   calibration=lambda: {"title": "合成甲"},
                                   ledger=Ledger(str(Path(self.temp.name) / "ledger.jsonl")),
                                   runner=runner, dpi=lambda: None)

    def tearDown(self):
        self.c.stop()
        self.assertTrue(self.c.reap(4))
        self.temp.cleanup()

    def test_no_contact_refuses_without_thread(self):
        self.assertFalse(self.c.start(""))
        self.assertIsNone(self.c.thread)
        self.assertEqual(self.calls, [])

    def test_wrong_calibrated_contact_refuses(self):
        self.assertFalse(self.c.start("b"))
        self.assertEqual(self.cfg["selected"], "a")
        self.assertEqual(self.calls, [])

    def test_blocked_ledger_displays_full_record_and_refuses(self):
        text = "完整正文" * 30
        self.c.ledger.begin_commit("合成甲", text)
        self.assertFalse(self.c.start("a"))
        self.assertEqual(self.c.state, "待人工确认")
        for value in (text, "合成甲", "COMMIT", "时间："):
            self.assertIn(value, self.c.note)
        self.assertEqual(self.calls, [])

    def test_unknown_also_blocks_and_explicit_ack_does_not_run(self):
        commit = self.c.ledger.begin_commit("合成甲", "合成回复")
        self.c.ledger.finish_commit(commit, False)
        self.assertFalse(self.c.start("a"))
        self.assertIn("UNKNOWN", self.c.note)
        self.assertTrue(self.c.acknowledge())
        self.assertFalse(self.c.ledger.blocks_start())
        self.assertEqual(self.calls, [])

    def test_stop_sets_event_and_reaps_non_daemon_worker(self):
        self.assertTrue(self.c.start("a"))
        self.assertTrue(self.ready.wait(2))
        self.assertFalse(self.c.thread.daemon)
        self.assertFalse(self.c.start("a"))
        self.c.stop()
        self.assertTrue(self.c.stop_event.is_set())
        self.assertTrue(self.c.reap(2))
        self.c.poll()
        self.assertIsNone(self.c.thread)
        self.assertEqual(self.c.state, "已暂停")
        self.assertTrue(self.calls[0][1]["live"])
        self.assertFalse(self.calls[0][1]["dry_run"])

    def test_close_cancel_does_not_request_stop(self):
        self.c.start("a")
        self.assertTrue(self.ready.wait(2))
        self.assertFalse(self.c.request_close(lambda: False))
        self.assertFalse(self.c.stop_event.is_set())
        self.assertTrue(self.c.running)
        self.assertTrue(self.c.request_close(lambda: True))
        self.assertTrue(self.c.stop_event.is_set())
        self.assertTrue(self.c.reap(2))

    def test_three_chat_categories_and_verification_not_invented(self):
        for item in [dict(kind="incoming", text="合成问题"), dict(kind="reply", text="合成回复")]:
            self.c.log("@hosting " + json.dumps(item), flush=True)
        self.c.poll()
        self.assertEqual([e.role for e in self.c.entries], ["incoming", "outgoing"])
        self.assertIn("尚未发送", self.c.entries[-1].status)
        self.c.log('@hosting {"kind":"verification","delivered":true}', flush=True)
        self.c.poll()
        self.assertEqual([e.role for e in self.c.entries], ["incoming", "outgoing", "system"])
        self.assertEqual(self.c.entries[1].status, "已核验本机气泡")

    def test_exception_reaped_and_freeze_retained(self):
        def crash(*_, **__):
            self.c.ledger.begin_commit("合成甲", "合成文本")
            raise RuntimeError("synthetic")
        self.c.runner = crash
        self.c.start("a")
        self.assertTrue(self.c.reap(2))
        self.c.poll()
        self.assertEqual(self.c.state, "待人工确认")
        self.assertFalse(self.c.start("a"))

    def test_final_status_keeps_actual_pause_reason(self):
        def pause(*_, log, **__):
            log('@hosting {"kind":"system","text":"暂停：user_took_over","state":"已暂停"}', flush=True)
            return 0
        self.c.runner = pause
        self.c.start("a")
        self.assertTrue(self.c.reap(2))
        self.c.poll()
        self.assertIn("user_took_over", self.c.note)

    def test_timeout_not_reported_as_reaped(self):
        release = threading.Event()
        def slow(*_, **__):
            self.ready.set()
            release.wait(3)
            return 0
        self.c.runner = slow
        try:
            self.c.start("a")
            self.assertTrue(self.ready.wait(2))
            self.c.stop()
            self.assertFalse(self.c.reap(0.01))
            self.c.poll()
            self.assertTrue(self.c.running)
            self.assertNotEqual(self.c.state, "已暂停")
        finally:
            release.set()

    def test_widgets_cancel_then_confirm_waits_for_cleanup(self):
        release = threading.Event()
        root = tk.Tk()
        root.withdraw()
        def slow(*_, **__):
            self.ready.set()
            release.wait(3)
            return 0
        self.c.runner = slow
        window = HostingWindow(root, self.c, confirm=lambda: False)
        try:
            window.contact.selection_clear(0, "end")
            window.contact.selection_set(0)
            window.select_contact()
            window.toggle()
            self.assertTrue(self.ready.wait(2))
            for item in [dict(kind="incoming", text="合成来信"), dict(kind="reply", text="合成回复"),
                         dict(kind="system", text="合成事件")]:
                self.c.log("@hosting " + json.dumps(item), flush=True)
            window._pump()
            self.assertTrue(window.history.tag_ranges("incoming"))
            self.assertTrue(window.history.tag_ranges("outgoing"))
            self.assertTrue(window.history.tag_ranges("system"))
            self.assertEqual(window.history.tag_cget("incoming", "justify"), "left")
            self.assertEqual(window.history.tag_cget("outgoing", "justify"), "right")
            window.on_close()
            self.assertFalse(window.closing)
            self.assertFalse(self.c.stop_event.is_set())
            window.confirm = lambda: True
            window.on_close()
            self.assertTrue(window.closing)
            self.assertTrue(self.c.stop_event.is_set())
            self.assertTrue(root.winfo_exists())
            self.assertFalse(self.c.reap(0.01))
            release.set()
            self.assertTrue(self.c.reap(2))
            window._pump()
            with self.assertRaises(tk.TclError):
                root.winfo_exists()
        finally:
            release.set()
            self.c.reap(2)
            try:
                root.destroy()
            except tk.TclError:
                pass
            window = None
            root = None
            gc.collect()  # collect Tk objects on their owner thread


class RunnerWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.ledger = Ledger(str(Path(self.temp.name) / "ledger.jsonl"))
        self.stop = threading.Event()
        self.frame = Image.new("RGBA", (50, 50))
        self.capture = Mock(return_value=self.frame)
        self.read = Mock(return_value=Reading())
        self.adapter = Mock()
        self.collect = Mock(return_value=[])
        self.cal = {"title": "合成甲", "window_rect": (0, 0, 50, 50), "messages_frame": (0, 0, 40, 20), "input_frame": (0, 20, 40, 30)}
        self.logs = []
        self.patcher = patch.object(hosting_run, "build_adapters", return_value=(self.capture, self.read, self.adapter, self.collect))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def run_loop(self, **kwargs):
        return hosting_run.run_hosting(self.cal, self.ledger, live=True, dry_run=False,
                                       log=lambda s, **_: self.logs.append(s), stop_event=self.stop, **kwargs)

    def session(self):
        session = Mock()
        session.step.return_value = (Decision.GENERATE, "合成问题")
        session.readback_finished.return_value = (Decision.COMMIT, "合成回复")
        session.commit_finished.side_effect = lambda *_: (self.stop.set() or Decision.WAIT, "delivered")
        return session

    def test_stopped_or_ledger_blocked_never_builds_adapters(self):
        self.stop.set()
        self.assertEqual(self.run_loop(), 0)
        self.stop.clear()
        self.ledger.begin_commit("合成甲", "合成内容")
        self.assertEqual(self.run_loop(), 3)
        hosting_run.build_adapters.assert_not_called()

    def test_title_preflight_before_model_and_input(self):
        self.read.return_value = Reading(title_ok=False)
        with patch.object(hosting_run, "build_model") as model, patch.object(hosting_run, "stage_reply_real") as stage:
            self.assertEqual(self.run_loop(), 2)
            model.assert_not_called()
            stage.assert_not_called()
        self.adapter.close.assert_called_once()

    def test_model_initialization_exception_closes_adapter(self):
        with patch.object(hosting_run, "build_model", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.run_loop()
        self.adapter.close.assert_called_once()

    def test_stop_during_generation_never_stages(self):
        def generate(*_):
            self.stop.set()
            return "合成回复"
        with patch.object(hosting_run, "Session", return_value=self.session()), \
                patch.object(hosting_run, "build_model", return_value=generate), \
                patch.object(hosting_run, "stage_reply_real") as stage:
            self.run_loop()
            stage.assert_not_called()
        self.assertFalse(self.ledger.read())
        self.adapter.close.assert_called_once()

    def test_stop_after_stage_prevents_commit(self):
        def stage(*_, **__):
            self.stop.set()
            return SimpleNamespace(outcome=SimpleNamespace(value="STAGED"), reason="match", text="合成回复"), ""
        with patch.object(hosting_run, "Session", return_value=self.session()), \
                patch.object(hosting_run, "build_model", return_value=lambda *_: "合成回复"), \
                patch.object(hosting_run, "stage_reply_real", side_effect=stage):
            self.run_loop()
        self.assertFalse(self.ledger.read())

    def test_once_commit_pre_enter_baseline_and_post_stop_verification(self):
        calls = []
        self.capture.side_effect = lambda: (calls.append("capture") or self.frame)
        def enter():
            calls.append("enter")
            self.stop.set()  # stop during submit must still let verification finish
        def verify(*_, **kwargs):
            self.assertIs(kwargs["baseline"], self.frame)
            calls.append("verify")
            return True
        stage = SimpleNamespace(outcome=SimpleNamespace(value="STAGED"), reason="match", text="合成回复")
        with patch.object(hosting_run, "Session", return_value=self.session()), \
                patch.object(hosting_run, "build_model", return_value=lambda *_: "合成回复"), \
                patch.object(hosting_run, "stage_reply_real", return_value=(stage, "")), \
                patch.object(hosting_run, "verify_delivery", side_effect=verify), \
                patch("input_readback_clipboard.NativeKeyboard", return_value=SimpleNamespace(send_enter=enter)):
            self.run_loop()
        self.assertEqual(calls, ["capture", "capture", "enter", "verify"])
        self.assertFalse(self.ledger.blocks_start())
        self.assertEqual([e.kind for e in self.ledger.read()], ["commit_begin", "commit_finish"])
        self.assertTrue(any('"delivered": true' in s for s in self.logs))

    def test_nontext_pause_is_visible_and_no_model(self):
        session = self.session()
        session.step.return_value = (Decision.PAUSE, "unreadable_new_message")
        generator = Mock()
        with patch.object(hosting_run, "Session", return_value=session), \
                patch.object(hosting_run, "build_model", return_value=generator):
            self.run_loop()
        generator.assert_not_called()
        self.assertTrue(any("[非文字] 未处理" in s for s in self.logs))

    def test_handoff_and_failed_model_stop_without_stage(self):
        for outcome in (SimpleNamespace(action="handoff", text="", reason="合成接手"), RuntimeError("合成失败")):
            with self.subTest(outcome=type(outcome).__name__):
                generator = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
                with patch.object(hosting_run, "Session", return_value=self.session()), \
                        patch.object(hosting_run, "build_model", return_value=generator), \
                        patch.object(hosting_run, "stage_reply_real") as stage:
                    self.run_loop()
                    stage.assert_not_called()
        self.assertFalse(self.ledger.read())

    def test_model_contact_must_match_calibrated_target(self):
        cfg = {"profiles": [{"id": "a", "name": "合成乙"}], "selected": "a"}
        with patch("chat_core.Store") as store, patch("chat_core.ChatAI") as ai:
            store.return_value.config.return_value = cfg
            with self.assertRaisesRegex(ValueError, "selected_contact_differs"):
                hosting_run.build_model(self.cal)
            ai.return_value.decide.assert_not_called()

    def test_model_failure_reason_codes_are_safe_not_raw_exception_text(self):
        cases = [
            (ValueError("模型没有返回规定格式，本条未发送。"), "model_format_invalid"),
            (ValueError("模型回复为空或过长，本条未发送。"), "model_reply_limits"),
            (ValueError("模型设置或联系人已改变，本轮草稿请求作废。"), "model_config_changed"),
            (ValueError("DeepSeek 余额不足。"), "model_http_402"),
            (ValueError("DeepSeek 额度不足或请求频繁，请检查用量。"), "model_http_429"),
            (ValueError("连接 DeepSeek 失败或超时，请检查网络。本条没有发送。"), "model_network_timeout"),
            (json.JSONDecodeError("synthetic", "secret-body", 0), "model_response_json_invalid"),
            (ValueError("sk-private-secret raw body https://private.example"), "model_error_unclassified"),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected):
                code, text = hosting_run.safe_model_failure(error)
                self.assertEqual(code, expected)
                self.assertNotIn("sk-private-secret", text)
                self.assertNotIn("secret-body", text)
                self.assertNotIn("private.example", text)


class StyleAndAppTests(unittest.TestCase):
    def test_dpi_cannot_mislabel_pmv1_process_as_v2_thread(self):
        for process_context, thread_context, expected in [(-4, -4, True), (-3, -4, False), (-4, -3, False)]:
            with self.subTest(process=process_context, thread=thread_context):
                api = SimpleNamespace(SetProcessDpiAwarenessContext=Mock(return_value=0),
                                      SetThreadDpiAwarenessContext=Mock(return_value=-4),
                                      GetThreadDpiAwarenessContext=Mock(return_value=thread_context),
                                      GetDpiAwarenessContextForProcess=Mock(return_value=process_context),
                                      AreDpiAwarenessContextsEqual=Mock(side_effect=lambda a, b: ctypes.c_void_p(a).value == b.value))
                dll = SimpleNamespace(user32=api, kernel32=SimpleNamespace(GetCurrentProcess=Mock(return_value=-1)))
                with patch("ctypes.windll", dll):
                    if expected:
                        self.assertEqual(hosting_run.enable_pmv2(), "per_monitor_v2")
                    else:
                        with self.assertRaises(RuntimeError):
                            hosting_run.enable_pmv2()

    def test_style_empty_identical_and_nonempty_additive(self):
        cfg = copy.deepcopy(DEFAULT_SETTINGS)
        profile = new_profile("合成甲")
        no_field = dict(cfg)
        no_field.pop("style_card")
        self.assertEqual(prompt_for(no_field, profile), prompt_for(cfg, profile))
        cfg["style_card"] = "合成风格：简短"
        result = prompt_for(cfg, profile)
        self.assertTrue(result.startswith(prompt_for(no_field, profile)))
        self.assertIn(DIRECT_WRITER, result)
        self.assertIn(cfg["style_card"], result)

    def test_style_roundtrip_only_temporary_config(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            store = Store(folder)
            try:
                cfg = store.config()
                self.assertEqual(cfg["style_card"], "")
                cfg["style_card"] = "合成风格"
                store.save(cfg)
                self.assertEqual(store.config()["style_card"], "合成风格")
            finally:
                store.db.close()

    def test_app_has_no_hosting_symbols(self):
        tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
        names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertFalse(any("hosting_" in name for name in names))
        self.assertNotIn("智能托管", Path(app.__file__).read_text(encoding="utf-8"))

    def test_chat_app_builds_without_native_or_real_store(self):
        # Construct the actual App/widgets, replacing all external dependencies.
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            store = Store(folder)
            root = tk.Tk()
            root.withdraw()
            try:
                with patch.object(app, "Store", return_value=store), \
                        patch.object(app, "ChatAI"), patch.object(app, "Engine"), \
                        patch.object(app, "ReadOnlyMonitor"), patch.object(app.App, "refresh_bridge"):
                    view = app.App(root)
                    root.update_idletasks()
                    with patch.object(app.subprocess, "Popen") as launch, patch.object(app.messagebox, "showinfo") as info:
                        launch.return_value.poll.return_value = None
                        view.managed_window_button.invoke()
                        launch.assert_called_once()
                        command = launch.call_args.args[0]
                        self.assertTrue(command[-1].endswith("hosting_window.py"))
                        self.assertNotIn("--live", command)
                        view.managed_window_button.invoke()
                        self.assertEqual(launch.call_count, 1)
                        info.assert_called_once()
                    view.pause()
                    view.close()
            finally:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
                store.db.close()
            view = None
            root = None
            gc.collect()  # later worker tests must not finalize Tk variables


if __name__ == "__main__":
    unittest.main()

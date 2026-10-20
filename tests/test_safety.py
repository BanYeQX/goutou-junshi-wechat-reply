import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_core import Decision, DeepSeek, Store, incoming_guard, new_profile, parse_decision, prompt_for
from engine import Engine
from wechat_bridge import Message, StaleConversation, WeChatBridge, compatibility


class FakeBridge:
    def __init__(self, *_):
        self.messages = [Message("old", "旧消息", "friend", "text")]
        self.sent = []
        self.lock = threading.RLock()

    def connect(self):
        pass

    def snapshot(self):
        with self.lock:
            return list(self.messages)

    def send(self, text, expected):
        with self.lock:
            if self.messages[-1].identity != expected:
                raise StaleConversation("stale")
            self.sent.append(text)
            self.messages.append(Message(f"sent{len(self.sent)}", text, "self", "text"))

    def close(self):
        pass


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.store = Store(Path(self.temp.name))
        self.a = new_profile("测试好友甲")
        self.b = new_profile("测试好友乙")
        self.a["context"] = "甲喜欢音乐"
        self.b["context"] = "乙喜欢篮球"
        cfg = self.store.config()
        cfg.update(profiles=[self.a, self.b], selected=self.a["id"], cloud_consent=True, debounce_seconds=1)
        self.store.save(cfg)
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.stop()
            if engine.thread:
                engine.thread.join(2)
        self.store.db.close()
        self.temp.cleanup()

    def engine(self, ai=None, bridge=None):
        result = Engine(self.store, ai or SimpleNamespace(decide=lambda *_: Decision("reply", "你好呀")),
                        bridge_factory=lambda *_: bridge or FakeBridge())
        self.engines.append(result)
        return result

    def test_profiles_and_history_are_separate(self):
        self.store.append("live:" + self.a["id"], "user", "甲的消息")
        self.store.append("practice:" + self.a["id"], "user", "演练消息")
        self.store.append("live:" + self.b["id"], "user", "乙的消息")
        self.assertEqual(self.store.history("live:" + self.a["id"])[0]["content"], "甲的消息")
        text = prompt_for(self.store.config(), self.a)
        self.assertIn("甲喜欢音乐", text)
        self.assertNotIn("乙喜欢篮球", text)

    def test_history_is_volatile_by_default(self):
        self.store.append("live:test", "user", "不落盘的消息")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
        other = Store(Path(self.temp.name))
        self.assertEqual(other.history("live:test"), [])
        other.db.close()

    def test_persistent_history_bounded_and_clear_scoped(self):
        cfg = self.store.config()
        cfg["remember_history"] = True
        self.store.save(cfg)
        for i in range(40):
            self.store.append("live:" + self.a["id"], "user", str(i))
        self.store.append("live:" + self.b["id"], "user", "乙")
        self.assertEqual(len(self.store.history("live:" + self.a["id"])), 32)
        self.store.clear(self.a["id"])
        self.assertEqual(self.store.history("live:" + self.a["id"]), [])
        self.assertEqual(len(self.store.history("live:" + self.b["id"])), 1)

    def test_key_never_saved_as_plaintext(self):
        with patch("chat_core.protect", return_value="encrypted-test-token"):
            self.store.save(self.store.config(), "synthetic-secret")
        self.assertNotIn("synthetic-secret", self.store.path.read_text(encoding="utf-8"))
        self.assertIn("encrypted-test-token", self.store.path.read_text(encoding="utf-8"))

    def test_duplicate_contact_name_refused(self):
        cfg = self.store.config()
        cfg["profiles"][1]["name"] = self.a["name"]
        with self.assertRaisesRegex(ValueError, "重复"):
            self.store.save(cfg)

    def test_unknown_selected_profile_refused(self):
        cfg = self.store.config()
        cfg["selected"] = "missing"
        with self.assertRaises(ValueError):
            self.store.save(cfg)

    def test_reusing_profile_for_a_different_contact_refused(self):
        cfg = self.store.config()
        cfg["profiles"][0]["name"] = "另一位新联系人"
        with self.assertRaisesRegex(ValueError, "新联系人"):
            self.store.save(cfg)

    def test_stop_request_and_sensitive_handoff(self):
        self.assertEqual(incoming_guard("不要再联系我").action, "stop")
        self.assertEqual(incoming_guard("借我500块").action, "handoff")
        self.assertEqual(incoming_guard("你是机器人吗").action, "handoff")
        self.assertIsNone(incoming_guard("我在听歌"))

    def test_model_wait_never_sends_returned_text(self):
        result = parse_decision('{"action":"wait","text":"不得发送","reason":"先留白"}')
        self.assertEqual(result.text, "")

    def test_invalid_or_long_model_output_refused(self):
        for raw in ("直接发：你好", '[]', '{"action":"execute"}', json.dumps({"action": "reply", "text": "长" * 121})):
            with self.assertRaises(ValueError):
                parse_decision(raw)

    def test_model_sensitive_output_handoff(self):
        self.assertEqual(parse_decision('{"action":"reply","text":"我给你转500元"}').action, "handoff")
        self.assertEqual(parse_decision('{"action":"reply","text":"看看https://example.com"}').action, "handoff")

    def test_no_cloud_call_without_consent(self):
        cfg = self.store.config()
        cfg["cloud_consent"] = False
        self.store.save(cfg)
        with patch("chat_core.urllib.request.urlopen") as network:
            with self.assertRaisesRegex(ValueError, "允许"):
                DeepSeek(self.store).decide(self.a, "practice:test", "你好")
            network.assert_not_called()

    def test_current_unsupported_wechat_blocks_before_connection(self):
        engine = self.engine()
        with patch.object(self.store, "key", return_value="synthetic-key"), patch("wechat_bridge.installed_version", return_value=(4, 1, 15, 13)):
            self.assertFalse(compatibility()["ready"])
            with self.assertRaisesRegex(ValueError, "4.1.8.107"):
                engine.start()
        self.assertFalse(engine.running)

    def test_paused_engine_cannot_send(self):
        engine, bridge = self.engine(), FakeBridge()
        engine.running = True
        engine.stop()
        self.assertFalse(engine.deliver(bridge, self.a, engine.stop_signal, engine.epoch, "old", "你好", "live:test"))
        self.assertEqual(bridge.sent, [])

    def test_target_switch_invalidates_send(self):
        engine, bridge = self.engine(), FakeBridge()
        engine.running = True
        cfg = self.store.config()
        cfg["selected"] = self.b["id"]
        self.store.save(cfg)
        with self.assertRaisesRegex(ValueError, "切换"):
            engine.deliver(bridge, self.a, engine.stop_signal, engine.epoch, "old", "你好", "live:test")
        self.assertEqual(bridge.sent, [])

    def test_actual_window_identity_checked_before_send(self):
        bridge = WeChatBridge("甲")
        calls = []
        bridge.client = SimpleNamespace(ChatInfo=lambda: {"chat_name": "乙", "chat_type": "friend"}, SendMsg=lambda **kw: calls.append(kw))
        with self.assertRaises(ValueError):
            bridge.send("你好", "old")
        self.assertEqual(calls, [])

    def test_new_message_invalidates_transport_send(self):
        bridge = WeChatBridge("甲")
        calls = []
        bridge.check_target = lambda: None
        bridge.snapshot = lambda: [Message("new", "新问题", "friend", "text")]
        bridge.client = SimpleNamespace(SendMsg=lambda **kw: calls.append(kw))
        with self.assertRaises(StaleConversation):
            bridge.send("旧答复", "old")
        self.assertEqual(calls, [])

    def test_manual_unsent_draft_is_not_overwritten(self):
        bridge = WeChatBridge("甲")
        calls = []
        bridge.check_target = lambda: None
        bridge.snapshot = lambda: [Message("old", "问题", "friend", "text")]
        bridge.client = SimpleNamespace(editbox=SimpleNamespace(GetValuePattern=lambda: SimpleNamespace(Value="我正在打字")), SendMsg=lambda **kw: calls.append(kw))
        with self.assertRaisesRegex(ValueError, "正在编辑"):
            bridge.send("机器人回复", "old")
        self.assertEqual(calls, [])

    def test_new_message_during_generation_discards_old_answer(self):
        bridge = FakeBridge()
        started, release = threading.Event(), threading.Event()
        questions = []
        def decide(_profile, _scope, question):
            questions.append(question)
            if len(questions) == 1:
                started.set()
                release.wait(5)
                return Decision("reply", "旧回复")
            return Decision("reply", "新回复")
        engine = self.engine(SimpleNamespace(decide=decide), bridge)
        with patch.object(self.store, "key", return_value="synthetic-key"), patch("engine.compatibility", return_value={"ready": True}):
            engine.start()
        time.sleep(0.15)
        with bridge.lock:
            bridge.messages.append(Message("one", "第一句", "friend", "text"))
        try:
            self.assertTrue(started.wait(4))
            with bridge.lock:
                bridge.messages.append(Message("two", "补充一句", "friend", "text"))
            time.sleep(0.65)
            release.set()
            deadline = time.monotonic() + 5
            while not bridge.sent and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(bridge.sent, ["新回复"])
            self.assertEqual(questions[-1], "第一句\n补充一句")
            history = self.store.history("live:" + self.a["id"])
            self.assertEqual([p["role"] for p in history], ["user", "assistant"])
        finally:
            release.set()

    def test_manual_reply_pauses_without_ai(self):
        bridge = FakeBridge()
        calls = []
        engine = self.engine(SimpleNamespace(decide=lambda *args: calls.append(args)), bridge)
        with patch.object(self.store, "key", return_value="synthetic-key"), patch("engine.compatibility", return_value={"ready": True}):
            engine.start()
        time.sleep(0.15)
        with bridge.lock:
            bridge.messages.append(Message("manual", "我自己回复", "self", "text"))
        engine.thread.join(2)
        self.assertFalse(engine.running)
        self.assertEqual(calls, [])
        self.assertEqual(bridge.sent, [])

    def test_rebuilt_ui_ids_do_not_replay_old_messages(self):
        bridge = FakeBridge()
        calls = []
        engine = self.engine(SimpleNamespace(decide=lambda *args: calls.append(args)), bridge)
        with patch.object(self.store, "key", return_value="synthetic-key"), patch("engine.compatibility", return_value={"ready": True}):
            engine.start()
        time.sleep(0.15)
        with bridge.lock:
            bridge.messages = [Message("rebuilt-old", "旧消息", "friend", "text")]
        engine.thread.join(2)
        self.assertFalse(engine.running)
        self.assertEqual(calls, [])
        self.assertEqual(bridge.sent, [])


if __name__ == "__main__":
    unittest.main()

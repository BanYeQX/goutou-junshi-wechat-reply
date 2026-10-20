"""ChatAI.ask_json builds the right request. The network is mocked, never used."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import advisor
from chat_core import ChatAI


class FakeStore:
    def __init__(self, folder, **cfg):
        self.data = Path(folder)
        self.cfg = {"provider": "deepseek", "model": "deepseek-chat", "cloud_consent": True,
                    "cloud_provider": "deepseek", **cfg}

    def config(self):
        return dict(self.cfg)

    def key(self, provider=None):
        return "sk-test-not-real"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AskJsonTests(unittest.TestCase):
    def call(self, store):
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data.decode("utf-8")))
            return Response(json.dumps({"choices": [{"message": {"content": '{"text":"好"}'}}]}).encode())

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            raw = ChatAI(store).ask_json("规则", "对方：在吗", schema=advisor.REWRITE_SCHEMA,
                                         name="goutou_rewrite")
        return raw, sent

    def test_deepseek_gets_one_user_message_in_json_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            raw, sent = self.call(FakeStore(folder))
        self.assertEqual(raw, '{"text":"好"}')
        payload = sent[0]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertEqual(payload["messages"][1]["content"], "对方：在吗")

    def test_no_consent_no_request(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FakeStore(folder, cloud_consent=False)
            with patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(ValueError):
                    ChatAI(store).ask_json("规则", "x", schema={}, name="n")
            urlopen.assert_not_called()

    def test_openai_uses_a_strict_schema(self):
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data.decode("utf-8")))
            raise OSError("stop here")

        with tempfile.TemporaryDirectory() as folder:
            store = FakeStore(folder, provider="openai", cloud_provider="openai", model="gpt-5.5")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(OSError):
                    ChatAI(store).ask_json("规则", "x", schema=advisor.ANALYSIS_SCHEMA, name="goutou_advice")
        fmt = sent[0]["text"]["format"]
        self.assertEqual((fmt["type"], fmt["name"], fmt["strict"]), ("json_schema", "goutou_advice", True))
        self.assertEqual([m["role"] for m in sent[0]["input"]], ["user"])


if __name__ == "__main__":
    unittest.main()

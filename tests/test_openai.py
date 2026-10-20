import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_core import ChatAI, Store, new_profile, read_response_stream, protect, unprotect
from plan_auth import ISSUER, PLAN_SCOPE, RESOURCE, TOKEN, PlanAuth, pkce_challenge, validate_callback


def stream(*events):
    return io.BytesIO("".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode("utf-8"))


def completed(text='{"action":"reply","text":"在听什么歌呀？","reason":"承接音乐话题"}'):
    return {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]}}


class OpenAITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_keys_are_isolated_by_provider(self):
        with patch("chat_core.protect", side_effect=lambda s: "encrypted:" + s), patch("chat_core.unprotect", side_effect=lambda s: s.removeprefix("encrypted:")):
            self.store.save(self.store.config(), "test-deepseek-only")
            cfg = self.store.config()
            cfg.update(provider="openai", model="gpt-5-mini")
            self.store.save(cfg, "test-openai-only")
            self.assertEqual(self.store.key("deepseek"), "test-deepseek-only")
            self.assertEqual(self.store.key("openai"), "test-openai-only")
            self.assertEqual(self.store.key("chatgpt"), "")

    def test_deepseek_key_is_never_reused_at_openai(self):
        self.store.save(self.store.config(), "synthetic-deepseek")
        cfg = self.store.config()
        cfg.update(provider="openai", model="gpt-5-mini", cloud_consent=True)
        self.store.save(cfg)
        with patch.dict(os.environ, {}, clear=True), patch("chat_core.urllib.request.urlopen") as network:
            with self.assertRaisesRegex(ValueError, "OpenAI"):
                ChatAI(self.store).decide(new_profile("测试"), "test", "你好")
            network.assert_not_called()

    def test_openai_request_uses_responses_schema_and_no_storage(self):
        cfg = self.store.config()
        cfg.update(provider="openai", model="gpt-5-mini", cloud_consent=True)
        self.store.save(cfg)
        with patch.object(self.store, "key", return_value="synthetic-openai"), patch("chat_core.urllib.request.urlopen", return_value=stream(completed())) as network:
            decision = ChatAI(self.store).decide(new_profile("测试"), "test", "听歌")
        request = network.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertNotIn("thinking", payload)
        self.assertEqual(decision.action, "reply")

    def test_partial_text_without_completion_never_succeeds(self):
        with self.assertRaisesRegex(ValueError, "中断"):
            read_response_stream(stream({"type": "response.output_text.delta", "delta": "半截回复"}))

    def test_empty_terminal_output_uses_completed_stream_item(self):
        message = completed()["response"]["output"][0]
        message.update(status="completed", role="assistant")
        raw = message["content"][0]["text"]
        events = [
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": raw[:20]},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": raw[20:]},
            {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": raw},
            {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": message["content"][0]},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        ]
        self.assertEqual(read_response_stream(stream(*events)), raw)

    def test_deltas_are_usable_only_after_completed_empty_envelope(self):
        raw = '中文回复，不应该重复。'
        self.assertEqual(read_response_stream(stream(
            {"type": "response.output_text.delta", "delta": raw[:5]},
            {"type": "response.output_text.delta", "delta": raw[5:]},
            {"type": "response.completed", "response": {"status": "completed", "output": []}})), raw)

    def test_done_text_replaces_deltas_and_is_not_duplicated(self):
        self.assertEqual(read_response_stream(stream(
            {"type": "response.output_text.delta", "delta": "先前内容"},
            {"type": "response.output_text.done", "text": "最终正文"},
            {"type": "response.completed", "response": {"status": "completed"}})), "最终正文")

    def test_done_item_without_terminal_event_is_not_success(self):
        with self.assertRaisesRegex(ValueError, "中断"):
            read_response_stream(stream({"type": "response.output_item.done", "item": completed()["response"]["output"][0]}))

    def test_failure_after_done_text_still_blocks_reply(self):
        for terminal in ({"type": "response.incomplete", "response": {}},
                         {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}},
                         {"type": "response.cancelled", "response": {}},
                         {"type": "response.completed", "response": {"status": "incomplete", "output": []}}):
            with self.assertRaises(ValueError):
                read_response_stream(stream({"type": "response.output_text.done", "text": "不可发送"}, terminal))

    def test_reasoning_and_tool_arguments_never_become_reply(self):
        for event in ({"type": "response.reasoning_text.delta", "delta": "internal reasoning"},
                      {"type": "response.function_call_arguments.done", "arguments": '{"text":"not a reply"}'},
                      {"type": "response.output_item.done", "item": {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "internal reasoning"}]}}):
            with self.assertRaisesRegex(ValueError, "没有返回"):
                read_response_stream(stream(event, {"type": "response.completed", "response": {"status": "completed", "output": []}}))

    def test_stream_refusal_blocks_even_with_earlier_text(self):
        for refusal in ({"type": "response.refusal.delta", "delta": "refused"},
                        {"type": "response.content_part.done", "part": {"type": "refusal", "refusal": "refused"}},
                        {"type": "response.output_item.done", "item": {"type": "message", "content": [{"type": "refusal", "refusal": "refused"}]}}):
            with self.assertRaisesRegex(ValueError, "拒绝"):
                read_response_stream(stream({"type": "response.output_text.delta", "delta": "earlier"}, refusal,
                    {"type": "response.completed", "response": {"status": "completed", "output": []}}))

    def test_stream_preserves_output_and_content_order(self):
        self.assertEqual(read_response_stream(stream(
            {"type": "response.output_text.done", "output_index": 1, "content_index": 1, "text": "丁"},
            {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "甲"},
            {"type": "response.output_text.done", "output_index": 1, "content_index": 0, "text": "丙"},
            {"type": "response.output_text.done", "output_index": 0, "content_index": 1, "text": "乙"},
            {"type": "response.completed", "response": {"status": "completed", "output": []}})), "甲乙丙丁")

    def test_final_canonical_output_is_not_duplicated_with_deltas(self):
        event = completed()
        raw = event["response"]["output"][0]["content"][0]["text"]
        self.assertEqual(read_response_stream(stream({"type": "response.output_text.delta", "delta": raw}, event)), raw)

    def test_final_frame_without_trailing_blank_line_is_parsed(self):
        raw = completed()
        wire = io.BytesIO(("event: response.completed\r\ndata: " + json.dumps(raw, ensure_ascii=False)).encode("utf-8"))
        self.assertEqual(read_response_stream(wire), raw["response"]["output"][0]["content"][0]["text"])

    def test_incomplete_message_item_cannot_use_earlier_deltas(self):
        with self.assertRaisesRegex(ValueError, "未完整"):
            read_response_stream(stream({"type": "response.output_text.delta", "delta": "earlier"},
                {"type": "response.output_item.done", "item": {"type": "message", "status": "incomplete", "content": []}},
                {"type": "response.completed", "response": {"status": "completed", "output": []}}))

    def test_quota_failure_after_partial_text_is_not_a_reply(self):
        with self.assertRaisesRegex(ValueError, "额度"):
            read_response_stream(stream({"type": "response.output_text.delta", "delta": "半截回复"},
                {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}))

    def test_refusal_and_incomplete_response_not_sent(self):
        refusal = {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}}
        for event in (refusal, {"type": "response.incomplete", "response": {}}):
            with self.assertRaises(ValueError):
                read_response_stream(stream(event))

    def test_chatgpt_uses_its_own_token_and_discovered_model(self):
        cfg = self.store.config()
        cfg.update(provider="chatgpt", model="auto", cloud_consent=True)
        self.store.save(cfg)
        ai = ChatAI(self.store)
        with patch.object(ai.plan, "access_token", return_value="synthetic-oauth"), patch.object(ai.plan, "models", return_value=[{"slug": "account-model", "display_name": "账号模型"}]), patch("chat_core.urllib.request.urlopen", return_value=stream(completed())) as network:
            result = ai.decide(new_profile("测试"), "test", "你好")
        self.assertEqual(json.loads(network.call_args.args[0].data)["model"], "account-model")
        payload = json.loads(network.call_args.args[0].data)
        for unsupported in ("max_output_tokens", "temperature", "previous_response_id", "conversation", "background"):
            self.assertNotIn(unsupported, payload)
        self.assertEqual(network.call_args.args[0].headers["Authorization"], "Bearer synthetic-oauth")
        self.assertEqual(result.action, "reply")

    def test_identity_login_without_plan_scope_cannot_infer(self):
        auth = PlanAuth(self.store)
        tokens = {"access_token": "synthetic", "scopes": ["openid"], "expires_at": time.time() + 3600}
        auth._write({"host_id": "test-host", "selected": "test-client", "accounts": [{"client_id": "test-client", "subject": "test-subject", "email": "", "protected_tokens": protect(json.dumps(tokens))}]})
        self.assertTrue(auth.status()["connected"])
        self.assertFalse(auth.status()["sharing"])
        with patch.object(auth, "_json_request") as network:
            with self.assertRaisesRegex(ValueError, "授权"):
                auth.access_token()
            network.assert_not_called()

    def test_models_only_keep_visible_models(self):
        auth = PlanAuth(self.store)
        with patch.object(auth, "access_token", return_value="synthetic"), patch.object(auth, "_json_request", return_value={"models": [
            {"slug": "visible", "visibility": "list", "display_name": "可用模型"}, {"slug": "hidden", "visibility": "hidden"}]}):
            self.assertEqual(auth.models(), [{"slug": "visible", "display_name": "可用模型"}])

    def test_logout_keeps_registration_and_host_but_clears_tokens(self):
        auth = PlanAuth(self.store)
        auth._write({"host_id": "stable-host", "selected": "registered-client", "accounts": [{"client_id": "registered-client", "subject": "subject", "email": "", "protected_tokens": protect(json.dumps({"access_token": "synthetic", "scopes": [PLAN_SCOPE]}))}]})
        auth.logout()
        saved = auth._read()
        self.assertEqual(saved["host_id"], "stable-host")
        self.assertEqual(saved["selected"], "registered-client")
        self.assertEqual(saved["accounts"][0]["protected_tokens"], "")
        self.assertFalse(auth.status()["connected"])

    def test_refresh_rotates_token_and_retains_granted_scopes(self):
        auth = PlanAuth(self.store)
        initial = {"access_token": "old-access", "refresh_token": "old-refresh", "scopes": [PLAN_SCOPE], "expires_at": 0}
        auth._write({"host_id": "stable-host", "selected": "issued-client", "accounts": [{"client_id": "issued-client", "subject": "subject", "protected_tokens": protect(json.dumps(initial))}]})
        with patch.object(auth, "_json_request", return_value={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}) as network:
            self.assertEqual(auth.access_token(), "new-access")
            self.assertEqual(auth.access_token(), "new-access")
        network.assert_called_once()
        self.assertEqual(network.call_args.args[0], TOKEN)
        self.assertEqual(network.call_args.kwargs["form"]["client_id"], "issued-client")
        self.assertEqual(network.call_args.kwargs["form"]["resource"], RESOURCE)
        saved = auth._tokens(auth._account(auth._read()))
        self.assertEqual(saved["refresh_token"], "new-refresh")
        self.assertEqual(saved["scopes"], [PLAN_SCOPE])
        self.assertNotIn("new-refresh", auth.path.read_text(encoding="utf-8"))

    def test_revoked_scope_is_saved_but_cannot_infer(self):
        auth = PlanAuth(self.store)
        initial = {"refresh_token": "old-refresh", "scopes": [PLAN_SCOPE], "expires_at": 0}
        auth._write({"host_id": "host", "selected": "client", "accounts": [{"client_id": "client", "subject": "subject", "protected_tokens": protect(json.dumps(initial))}]})
        with patch.object(auth, "_json_request", return_value={"access_token": "identity-only", "refresh_token": "replacement", "scope": "openid"}):
            with self.assertRaisesRegex(ValueError, "撤回"):
                auth.access_token()
        self.assertFalse(auth.status()["sharing"])
        self.assertEqual(auth._tokens(auth._account(auth._read()))["refresh_token"], "replacement")

    def test_top_level_sse_limit_error_is_recognized(self):
        with self.assertRaisesRegex(ValueError, "额度"):
            read_response_stream(stream({"type": "error", "code": "subscription_sharing_usage_limit_exceeded"}))

    def test_loopback_login_and_failed_exchange_retry_reuse_registration(self):
        auth = PlanAuth(self.store)
        browser_requests, callback_errors, workers = [], [], []

        def fake_browser(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            browser_requests.append(query)
            def callback():
                try:
                    local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    redirect = query["redirect_uri"][0]
                    response_query = {"state": query["state"][0], "code": "synthetic-once-code", "client_id": "issued-client"}
                    # A forged callback does not consume the pending login.
                    bad_query = dict(response_query, state="forged-state")
                    try:
                        local_http.open(redirect + "?" + urllib.parse.urlencode(bad_query), timeout=5)
                        callback_errors.append("forged state accepted")
                    except urllib.error.HTTPError as error:
                        if error.code != 400:
                            callback_errors.append("unexpected rejection status")
                    with local_http.open(redirect + "?" + urllib.parse.urlencode(response_query), timeout=5) as response:
                        body = response.read().decode("utf-8")
                        if "synthetic-once-code" in body or query["state"][0] in body:
                            callback_errors.append("callback leaked credentials")
                except Exception as error:
                    callback_errors.append(type(error).__name__ + ": " + str(error))
                    auth.cancel()
            worker = threading.Thread(target=callback, daemon=True)
            workers.append(worker)
            worker.start()
            return True

        grant = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "id_token": "synthetic-id", "scope": "openid " + PLAN_SCOPE, "expires_in": 3600}
        with patch("plan_auth.webbrowser.open", side_effect=fake_browser), patch.object(auth, "_json_request", side_effect=ValueError("synthetic exchange failure")):
            with self.assertRaises(ValueError) as first_failure:
                auth.login()
        for worker in workers:
            worker.join(timeout=3)
        self.assertEqual(callback_errors, [])
        self.assertIn("synthetic exchange failure", str(first_failure.exception))
        self.assertFalse(auth.status()["connected"])
        host_id = auth._read()["host_id"]
        self.assertEqual(len(auth._read()["accounts"]), 1)
        with patch("plan_auth.webbrowser.open", side_effect=fake_browser), patch.object(auth, "_json_request", return_value=grant) as exchange, patch.object(auth, "_verify_identity", return_value={"sub": "verified-subject", "email": "synthetic@example.invalid"}) as verify:
            status = auth.login(new_account=True)
        for worker in workers:
            worker.join(timeout=3)
        self.assertEqual(callback_errors, [])
        self.assertTrue(status["sharing"])
        self.assertEqual(auth._read()["host_id"], host_id)
        self.assertEqual(browser_requests[0]["client_id"], ["dynamic_agent_client"])
        self.assertEqual(browser_requests[1]["client_id"], ["issued-client"])
        self.assertNotIn("agent_name_hint", browser_requests[1])
        self.assertNotEqual(browser_requests[0]["state"], browser_requests[1]["state"])
        self.assertEqual(exchange.call_args.kwargs["form"]["redirect_uri"], browser_requests[1]["redirect_uri"][0])
        self.assertEqual(verify.call_args.args[2], browser_requests[1]["nonce"][0])
        self.assertNotIn("synthetic-refresh", auth.path.read_text(encoding="utf-8"))
        self.assertTrue(auth.needs_plan_notice())
        auth.acknowledge_plan()
        self.assertFalse(auth.needs_plan_notice())


class OAuthValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = cls.private.public_key()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.store = Store(Path(self.temp.name))
        self.auth = PlanAuth(self.store)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def token(self, **changes):
        claims = {"iss": ISSUER, "aud": "verified-client", "sub": "verified-subject", "exp": time.time() + 600, "nonce": "fresh-nonce"}
        claims.update(changes)
        return jwt.encode(claims, self.private, algorithm="RS256")

    def verify(self, token, nonce="fresh-nonce", subject=None):
        with patch.object(self.auth, "_metadata", return_value={"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks"}), patch("plan_auth.jwt.PyJWKClient", return_value=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=self.public))):
            return self.auth._verify_identity(token, "verified-client", nonce, subject)

    def test_valid_signed_identity_is_accepted(self):
        self.assertEqual(self.verify(self.token())["sub"], "verified-subject")

    def test_wrong_nonce_audience_issuer_or_subject_is_rejected(self):
        for changes in ({"nonce": "wrong"}, {"aud": "wrong"}, {"iss": "https://example.com"}, {"exp": time.time() - 600}):
            with self.assertRaises(ValueError):
                self.verify(self.token(**changes))
        with self.assertRaises(ValueError):
            self.verify(self.token(), subject="other-subject")

    def test_invalid_state_duplicate_parameters_and_client_substitution_rejected(self):
        for query, existing in (("state=wrong&code=x&client_id=registered", ""),
                                ("state=expected&state=expected&code=x&client_id=registered", ""),
                                ("state=expected&code=x&client_id=changed-client", "original-client"),
                                ("state=expected&code=x&client_id=dynamic_agent_client", ""),
                                ("state=expected&error=access_denied", "")):
            with self.assertRaises(ValueError):
                validate_callback(query, "expected", existing)

    def test_issued_client_is_used_and_returning_client_may_be_omitted(self):
        self.assertEqual(validate_callback("state=ok&code=abc&client_id=issued-client", "ok"), ("abc", "issued-client"))
        self.assertEqual(validate_callback("state=ok&code=abc", "ok", "saved-client"), ("abc", "saved-client"))

    def test_pkce_uses_s256_base64url_without_padding(self):
        self.assertEqual(pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


if __name__ == "__main__":
    unittest.main()

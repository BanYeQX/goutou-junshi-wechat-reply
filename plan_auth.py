"""Official ChatGPT public-client flow: loopback + PKCE + verified OIDC identity.

Only this app's own credentials are read. No existing browser/Codex tokens are used.
Contract: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt

from chat_core import protect, unprotect

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
USAGE_URL = "https://chatgpt.com/settings/usage"


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def validate_callback(query, expected_state, pending_client=""):
    values = urllib.parse.parse_qs(query, keep_blank_values=True)
    if any(len(value) != 1 for value in values.values()):
        raise ValueError("授权回调参数重复，未接受。")
    state = values.get("state", [""])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        raise ValueError("授权回调校验失败，未接受。")
    if values.get("error"):
        raise ValueError("你取消了授权，或账号当前不允许使用此登录方式。")
    client = values.get("client_id", [pending_client])[0]
    code = values.get("code", [""])[0]
    if pending_client and client != pending_client:
        raise ValueError("授权返回的应用标识改变，未接受。")
    if not code or not client or client == "dynamic_agent_client" or len(client) > 256:
        raise ValueError("授权尚未完成，未获得有效的客户端标识。")
    return code, client


class PlanAuth:
    def __init__(self, store):
        self.path = store.data / "chatgpt_login.json"
        self.lock = threading.RLock()
        self.login_gate = threading.Lock()
        self.cancel_event = threading.Event()
        self.model_cache = None
        self.metadata_cache = None

    def _read(self):
        if not self.path.exists():
            return {"host_id": "", "selected": "", "accounts": []}
        try:
            result = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(result.get("accounts"), list):
                raise ValueError()
            return result
        except (ValueError, TypeError, OSError):
            raise ValueError("本 App 的 ChatGPT 授权配置无法读取，请检查本地文件。") from None

    def _write(self, data):
        with self.lock:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    def _account(self, data):
        return next((a for a in data["accounts"] if a["client_id"] == data["selected"]), None)

    def _tokens(self, account):
        if not account or not account.get("protected_tokens"):
            return {}
        return json.loads(unprotect(account["protected_tokens"]))

    def status(self):
        with self.lock:
            data = self._read()
            account = self._account(data)
            try:
                tokens = self._tokens(account)
            except ValueError:
                tokens = {}
            connected = bool(tokens.get("access_token") or tokens.get("refresh_token"))
            sharing = connected and PLAN_SCOPE in tokens.get("scopes", [])
            public = [{"id": a["client_id"], "label": f'{a.get("email") or "ChatGPT账号"} · {i + 1}'}
                      for i, a in enumerate(data["accounts"]) if a.get("subject")]
            return {"connected": connected, "sharing": sharing, "selected": data["selected"], "accounts": public,
                    "label": account.get("email", "") if account else "",
                    "message": "已连接，使用 ChatGPT 计划" if sharing else "已登录，但未授权使用计划" if connected else "尚未登录 ChatGPT"}

    def acknowledge_plan(self):
        """Remember only that the one-time plan-usage explanation was dismissed."""
        with self.lock:
            data = self._read()
            data["plan_notice_seen"] = True
            self._write(data)

    def needs_plan_notice(self):
        with self.lock:
            return self.status()["sharing"] and not self._read().get("plan_notice_seen", False)

    def _json_request(self, url, *, form=None, bearer=""):
        headers = {"Accept": "application/json"}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        raw = None
        if form is not None:
            raw = urllib.parse.urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=raw, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                body = response.read(1_000_000)
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise ValueError("ChatGPT 计划额度受限或请求频繁，请检查用量。") from None
            raise ValueError(f"ChatGPT 授权服务返回 HTTP {exc.code}，请重新登录或检查账号权限。") from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError("连接 ChatGPT 授权服务失败，请检查网络后重试。") from None

    def _metadata(self):
        if self.metadata_cache:
            return self.metadata_cache
        metadata = self._json_request(ISSUER + "/.well-known/openid-configuration")
        if metadata.get("issuer") != ISSUER:
            raise ValueError("ChatGPT 身份服务的签发方不匹配。")
        jwks = urllib.parse.urlsplit(metadata.get("jwks_uri", ""))
        if jwks.scheme != "https" or jwks.hostname != "auth.openai.com":
            raise ValueError("ChatGPT 身份公钥地址未通过校验。")
        self.metadata_cache = metadata
        return metadata

    def _verify_identity(self, id_token, client, nonce=None, subject=None):
        if not isinstance(id_token, str) or not id_token:
            raise ValueError("ChatGPT 未返回身份令牌，未保存授权。")
        try:
            metadata = self._metadata()
            signing_key = jwt.PyJWKClient(metadata["jwks_uri"], timeout=20).get_signing_key_from_jwt(id_token).key
            required = ["exp", "iss", "aud", "sub"] + (["nonce"] if nonce is not None else [])
            claims = jwt.decode(id_token, signing_key, algorithms=["RS256", "ES256"], audience=client,
                issuer=ISSUER, leeway=20, options={"require": required})
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise ValueError()
            if subject and claims["sub"] != subject:
                raise ValueError()
            return claims
        except Exception:
            raise ValueError("ChatGPT 身份签名、账号或授权校验失败，未接受新凭证。") from None

    def _credential_set(self, grant, previous=None):
        previous = previous or {}
        if not isinstance(grant.get("access_token"), str) or not grant["access_token"]:
            raise ValueError("授权服务没有返回可用凭证。")
        if str(grant.get("token_type", "Bearer")).lower() != "bearer":
            raise ValueError("授权服务返回了不支持的凭证类型。")
        try:
            lifetime = int(grant.get("expires_in", 3600))
            if not 0 < lifetime <= 31 * 86400:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("授权服务返回了无效的有效期。") from None
        scopes = str(grant["scope"]).split() if "scope" in grant else previous.get("scopes", [])
        return {"access_token": grant["access_token"], "refresh_token": grant.get("refresh_token", previous.get("refresh_token", "")),
                "id_token": grant.get("id_token", previous.get("id_token", "")), "scopes": scopes,
                "expires_at": time.time() + lifetime}

    def login(self, *, new_account=False):
        if not self.login_gate.acquire(blocking=False):
            raise ValueError("登录窗口已经打开，请先完成或取消这一轮授权。")
        server = None
        self.cancel_event.clear()
        try:
            with self.lock:
                data = self._read()
                if not data["host_id"]:
                    data["host_id"] = "urn:uuid:" + str(uuid.uuid4())
                    self._write(data)
                account = None if new_account else self._account(data)
                # Retry an issued but unfinished registration instead of creating
                # a different client after an expired/failed one-use code exchange.
                if account is None:
                    account = next((a for a in reversed(data["accounts"]) if not a.get("subject")), None)
                if account is None and len(data["accounts"]) >= 20:
                    raise ValueError("本机已保存20个授权，请使用已有账号。")
                existing = account["client_id"] if account else ""
                old_tokens = self._tokens(account)
            state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            result = {}
            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_GET(self):
                    parts = urllib.parse.urlsplit(self.path)
                    if len(self.path) > 20000 or parts.path != "/auth/callback" or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                        self.send_error(400)
                        return
                    try:
                        code, client = validate_callback(parts.query, state, existing)
                    except ValueError as exc:
                        # Invalid state must not consume the user's pending attempt.
                        incoming = urllib.parse.parse_qs(parts.query).get("state", [""])[0]
                        if incoming and secrets.compare_digest(incoming, state):
                            result["error"] = str(exc)
                        self.send_error(400, "Authorization was not accepted. Return to the app.")
                        return
                    result.update(code=code, client=client)
                    body = b"<!doctype html><meta charset=utf-8><title>GoutouChat</title><h2>Authorization received.</h2><p>Return to GoutouChat. The app will verify your account and permissions.</p>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()
                    self.wfile.write(body)
            server = HTTPServer(("127.0.0.1", 0), Callback)
            server.timeout = 0.25
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            parameters = {"client_id": existing or "dynamic_agent_client", "ext_agent_host_id": data["host_id"],
                "response_type": "code", "redirect_uri": redirect, "scope": SCOPES, "resource": RESOURCE,
                "state": state, "nonce": nonce, "code_challenge_method": "S256", "code_challenge": pkce_challenge(verifier)}
            if not existing:
                parameters["agent_name_hint"] = "GoutouChat"
            elif old_tokens.get("id_token"):
                parameters["id_token_hint"] = old_tokens["id_token"]
            if not webbrowser.open(AUTHORIZE + "?" + urllib.parse.urlencode(parameters)):
                raise ValueError("无法打开系统浏览器，请检查默认浏览器后重新登录。")
            deadline = time.monotonic() + 240
            while not result and not self.cancel_event.is_set() and time.monotonic() < deadline:
                server.handle_request()
            if self.cancel_event.is_set():
                raise ValueError("已取消本轮 ChatGPT 登录。")
            if not result:
                raise ValueError("ChatGPT 登录等待超时，请重新点击登录。")
            if result.get("error"):
                raise ValueError(result["error"])
            client = result["client"]
            # Retain issued client IDs even if the one-use authorization code expires.
            with self.lock:
                data = self._read()
                if not any(a["client_id"] == client for a in data["accounts"]):
                    data["accounts"].append({"client_id": client, "email": "", "subject": "", "protected_tokens": ""})
                    self._write(data)
            grant = self._json_request(TOKEN, form={"grant_type": "authorization_code", "client_id": client,
                "code": result["code"], "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE})
            claims = self._verify_identity(grant.get("id_token"), client, nonce, account.get("subject") if account else None)
            credentials = self._credential_set(grant)
            if self.cancel_event.is_set():
                confirmed = self._revoke(client, credentials.get("refresh_token", ""))
                raise ValueError("已取消登录，新凭证未设为活动账号。" + ("" if confirmed else "远程撤销未确认，请到 ChatGPT 设置中断开本应用。"))
            with self.lock:
                data = self._read()
                record = next(a for a in data["accounts"] if a["client_id"] == client)
                record.update(email=str(claims.get("email", "")), subject=claims["sub"], issuer=ISSUER,
                              protected_tokens=protect(json.dumps(credentials)))
                data["selected"] = client
                self._write(data)
                self.model_cache = None
            return self.status()
        finally:
            if server:
                server.server_close()
            self.login_gate.release()

    def cancel(self):
        self.cancel_event.set()

    def select(self, client):
        if self.login_gate.locked():
            raise ValueError("请先完成或取消当前登录。")
        with self.lock:
            data = self._read()
            if not any(a["client_id"] == client and a.get("subject") for a in data["accounts"]):
                raise ValueError("所选 ChatGPT 账号不存在或尚未验证。")
            data["selected"] = client
            self._write(data)
            self.model_cache = None
        return self.status()

    def access_token(self):
        with self.lock:
            data = self._read()
            account = self._account(data)
            tokens = self._tokens(account)
            if not account or PLAN_SCOPE not in tokens.get("scopes", []):
                raise ValueError("请登录 ChatGPT，并在官方授权页允许使用 ChatGPT 计划。")
            if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 60:
                return tokens["access_token"]
            if not tokens.get("refresh_token"):
                raise ValueError("ChatGPT 登录已过期，请重新登录。")
            grant = self._json_request(TOKEN, form={"grant_type": "refresh_token", "client_id": account["client_id"],
                "refresh_token": tokens["refresh_token"], "resource": RESOURCE})
            if grant.get("id_token"):
                self._verify_identity(grant["id_token"], account["client_id"], subject=account["subject"])
            updated = self._credential_set(grant, tokens)
            account["protected_tokens"] = protect(json.dumps(updated))
            self._write(data)
            if PLAN_SCOPE not in updated["scopes"]:
                raise ValueError("ChatGPT 计划共享权限已撤回，请重新授权。")
            return updated["access_token"]

    def models(self, refresh=False):
        with self.lock:
            selected = self._read()["selected"]
            if not refresh and self.model_cache and self.model_cache[0] == selected and time.monotonic() - self.model_cache[1] < 60:
                return list(self.model_cache[2])
        data = self._json_request(RESOURCE + "/models", bearer=self.access_token())
        models = [{"slug": m["slug"], "display_name": str(m.get("display_name", m["slug"]))}
                  for m in data.get("models", []) if m.get("visibility") == "list" and isinstance(m.get("slug"), str)]
        if not models:
            raise ValueError("此 ChatGPT 账号没有返回可用模型，请检查计划权限。")
        with self.lock:
            if self._read()["selected"] != selected:
                raise ValueError("ChatGPT 账号已切换，请重新获取模型列表。")
            self.model_cache = (selected, time.monotonic(), models)
        return models

    def inference_context(self, model):
        """Choose a model and credentials from the same account snapshot."""
        with self.lock:
            catalog = self.models()
            if model == "auto":
                model = catalog[0]["slug"]
            if model not in {item["slug"] for item in catalog}:
                raise ValueError("所选模型不在此 ChatGPT 账号的可用列表中，请刷新模型列表。")
            return self.access_token(), model

    def _revoke(self, client, refresh_token):
        if not refresh_token:
            return True
        for attempt in range(2):
            try:
                url = self._metadata().get("revocation_endpoint", "")
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "auth.openai.com":
                    return False
                self._json_request(url, form={"client_id": client, "token": refresh_token, "token_type_hint": "refresh_token"})
                return True
            except ValueError:
                if attempt == 0:
                    time.sleep(0.5)
        return False

    def logout(self):
        self.cancel()
        with self.lock:
            data = self._read()
            account = self._account(data)
            tokens = self._tokens(account)
            confirmed = self._revoke(account["client_id"], tokens.get("refresh_token", "")) if account else True
            if account:
                account["protected_tokens"] = ""
                self._write(data)
            self.model_cache = None
        return "已退出本 App 的 ChatGPT 授权。" if confirmed else "本地已退出，远程撤销未确认；请到 ChatGPT 设置中断开此应用。"

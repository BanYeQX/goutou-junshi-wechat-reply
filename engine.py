"""Pauseable one-contact event loop with debouncing and stale-draft rejection."""
from __future__ import annotations

import queue
import threading
import time

from chat_core import ChatAI, incoming_guard
from wechat_bridge import StaleConversation, WeChatBridge, compatibility


class Engine:
    def __init__(self, store, ai=None, bridge_factory=WeChatBridge):
        self.store = store
        self.ai = ai or ChatAI(store)
        self.bridge_factory = bridge_factory
        self.events = queue.Queue()
        self.approvals = queue.Queue()
        self.send_lock = threading.RLock()
        self.stop_signal = threading.Event()
        self.thread = None
        self.running = False
        self.draft = None
        self.epoch = 0

    def emit(self, kind, **values):
        self.events.put({"kind": kind, **values})

    def start(self):
        if self.thread and self.thread.is_alive():
            raise ValueError("上一轮连接正在停止，请稍后再启动。")
        cfg = self.store.config()
        profile = self.store.selected()
        if not profile:
            raise ValueError("请先添加并选中一位联系人。")
        if not cfg["cloud_consent"] or cfg["cloud_provider"] != cfg["provider"]:
            raise ValueError("请先在设置中允许将当前联系人消息发送至所选模型服务。")
        if cfg["provider"] == "chatgpt":
            self.ai.ensure_ready()
        elif not self.store.key():
            raise ValueError("请先配置所选模型服务的 API Key。")
        info = compatibility(cfg["adapter"])
        if not info["ready"]:
            raise ValueError(info["message"])
        self.stop_signal = threading.Event()
        self.approvals = queue.Queue()
        self.draft = None
        self.epoch += 1
        self.running = True
        self.thread = threading.Thread(target=self._run, args=(profile, cfg, self.stop_signal, self.epoch), daemon=True)
        self.thread.start()

    def stop(self, reason="已暂停，控制权交还给你。"):
        self.stop_signal.set()
        with self.send_lock:
            self.running = False
            self.draft = None
        self.emit("status", text=reason, running=False)
        self.emit("invalidate", text="接管已暂停，旧草稿已作废。")

    def approve(self, token, text):
        if not text.strip() or len(text.strip()) > 120:
            raise ValueError("发送正文需要1—120字。")
        self.approvals.put((token, text.strip()))

    def deliver(self, bridge, profile, signal, epoch, expected, text, scope):
        with self.send_lock:
            selected = self.store.selected()
            if signal.is_set() or not self.running or epoch != self.epoch:
                return False
            if not selected or (selected["id"], selected["name"]) != (profile["id"], profile["name"]):
                raise ValueError("联系人已经切换，旧草稿未发送。")
            bridge.send(text, expected)
        self.emit("chat", role="assistant", text=text, live=True, profile_id=profile["id"])
        return True

    def _run(self, profile, cfg, signal, epoch):
        bridge = None
        scope = "live:" + profile["id"]
        pending, seen, sent_echoes = [], set(), []
        revision, last_input, last_sent, tail = 0, 0.0, 0.0, ""
        job, draft = None, None
        try:
            self.emit("status", text="正在打开并校验指定私聊……", running=True)
            bridge = self.bridge_factory(profile["name"], cfg["adapter"])
            bridge.connect()
            baseline = bridge.snapshot()
            seen = {m.identity for m in baseline}
            tail = baseline[-1].identity if baseline else ""
            self.emit("status", text="已监听，仅处理启用后的新消息。", running=True)
            while not signal.is_set():
                selected = self.store.selected()
                if not selected or (selected["id"], selected["name"]) != (profile["id"], profile["name"]):
                    raise ValueError("联系人已改变，自动回复暂停。")
                messages = bridge.snapshot()
                if messages and seen and not any(m.identity in seen for m in messages):
                    raise ValueError("聊天窗口的消息标识已重建，已暂停以避免重复回复旧消息。")
                if messages:
                    tail = messages[-1].identity
                for message in messages:
                    if message.identity in seen:
                        continue
                    seen.add(message.identity)
                    if message.author == "self":
                        echo = next((p for p in sent_echoes if p[0] == message.text and time.monotonic() - p[1] < 20), None)
                        if echo:
                            sent_echoes.remove(echo)
                            continue
                        raise ValueError("检测到你已手动回复，自动聊天暂停。")
                    if message.author != "friend":
                        continue
                    if message.kind != "text":
                        raise ValueError("收到图片、语音或其他非文字消息，请你亲自查看；自动聊天暂停。")
                    if not message.text.strip() or len(message.text) > 8000:
                        raise ValueError("收到无法安全处理的消息，已暂停。")
                    guard = incoming_guard(message.text)
                    self.emit("chat", role="user", text=message.text, live=True, profile_id=profile["id"])
                    if guard:
                        raise ValueError(guard.reason + "，自动聊天暂停。")
                    pending.append(message.text)
                    revision += 1
                    last_input = time.monotonic()
                    if draft:
                        self.draft = draft = None
                        self.emit("invalidate", text="新消息到达，旧草稿已作废。")
                # Bound identifiers without replaying visible messages.
                if len(seen) > 3000:
                    seen = {m.identity for m in messages}
                if job and job["ready"].is_set():
                    completed, job = job, None
                    if completed["revision"] == revision:
                        if completed.get("error"):
                            raise ValueError(completed["error"])
                        decision = completed["decision"]
                        question = "\n".join(pending)
                        if decision.action in ("handoff", "stop"):
                            self.store.append(scope, "user", question)
                            raise ValueError(decision.reason or "需要你亲自处理，已暂停。")
                        if decision.action == "wait":
                            self.store.append(scope, "user", question)
                            pending.clear()
                            self.emit("log", text="本轮留白：" + decision.reason)
                        elif cfg["mode"] == "review":
                            token = f"{epoch}:{revision}:{time.monotonic()}"
                            draft = {"token": token, "revision": revision, "tail": tail, "text": decision.text,
                                     "question": question, "created": time.monotonic()}
                            self.draft = draft
                            self.emit("draft", **draft, reason=decision.reason)
                        else:
                            try:
                                if self.deliver(bridge, profile, signal, epoch, tail, decision.text, scope):
                                    # User text must precede the assistant in model history.
                                    self._record_exchange(scope, question, decision.text)
                                    sent_echoes.append((decision.text, time.monotonic()))
                                    last_sent = time.monotonic()
                                    pending.clear()
                            except StaleConversation:
                                last_input = time.monotonic()
                try:
                    token, text = self.approvals.get_nowait()
                except queue.Empty:
                    token, text = None, None
                if token:
                    if not draft or token != draft["token"] or revision != draft["revision"] or time.monotonic() - draft["created"] > 180:
                        self.emit("invalidate", text="草稿已过期或会话已更新，请等待新草稿。")
                        draft = self.draft = None
                    else:
                        try:
                            if self.deliver(bridge, profile, signal, epoch, draft["tail"], text, scope):
                                self._record_exchange(scope, draft["question"], text)
                                sent_echoes.append((text, time.monotonic()))
                                last_sent = time.monotonic()
                                pending.clear()
                                draft = self.draft = None
                                self.emit("invalidate", text="已按确认内容发送。")
                        except StaleConversation:
                            draft = self.draft = None
                            self.emit("invalidate", text="新消息已到，旧草稿没有发送。")
                if pending and not job and not draft and time.monotonic() - last_input >= cfg["debounce_seconds"] and time.monotonic() - last_sent >= cfg["min_send_seconds"]:
                    if len("\n".join(pending)) > 8000:
                        raise ValueError("对方连续消息过长，请你亲自查看。")
                    job = {"revision": revision, "ready": threading.Event()}
                    def generate(result=job, question="\n".join(pending)):
                        try:
                            result["decision"] = self.ai.decide(profile, scope, question)
                        except Exception as exc:
                            result["error"] = str(exc) if isinstance(exc, ValueError) else "模型请求失败，本条未发送。"
                        finally:
                            result["ready"].set()
                    threading.Thread(target=generate, daemon=True).start()
                    self.emit("log", text="正在生成一条回复……")
                signal.wait(0.5)
        except ValueError as exc:
            self.emit("status", text=str(exc), running=False)
        except Exception:
            self.emit("status", text="连接器未通过运行校验，自动回复已暂停。", running=False)
        finally:
            signal.set()
            self.running = False
            self.draft = None
            self.emit("invalidate", text="当前没有待发送草稿。")
            if bridge:
                bridge.close()

    def _record_exchange(self, scope, question, answer):
        # Only confirmed sends become model history, in conversation order.
        with self.store.lock:
            self.store.append(scope, "user", question)
            self.store.append(scope, "assistant", answer)

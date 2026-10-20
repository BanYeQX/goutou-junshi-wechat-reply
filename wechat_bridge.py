"""Conservative, single-window wxauto transport. Importing it never controls WeChat."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path


MAX_FREE_VERSION = (4, 1, 8, 107)
DOCUMENTATION = "https://docs.wxauto.org/docs/install.html"


def installed_version():
    import win32api
    for path in (Path("C:/Program Files/Tencent/Weixin/Weixin.exe"), Path("C:/Program Files (x86)/Tencent/Weixin/Weixin.exe")):
        if path.exists():
            value = win32api.GetFileVersionInfo(str(path), "\\")
            return (value["FileVersionMS"] >> 16, value["FileVersionMS"] & 65535,
                    value["FileVersionLS"] >> 16, value["FileVersionLS"] & 65535)
    raise ValueError("没有在已检查的位置找到微信，请确认桌面版已安装。")


def compatibility(adapter="wxauto4"):
    try:
        version = installed_version()
    except (ValueError, OSError) as exc:
        return {"ready": False, "version": "未检测到", "message": str(exc)}
    label = ".".join(map(str, version))
    if adapter != "wxauto4":
        return {"ready": False, "version": label, "message": "Plus 连接器尚未在本机验证，请使用经过验证的连接方案。"}
    supported = (4, 1, 0, 0) <= version <= MAX_FREE_VERSION
    return {"ready": supported, "version": label, "message":
            "版本在文档范围内；启动时仍需校验会话控件和收发。" if supported else
            f"微信 {label} 超出免费连接器文档支持的 4.1.8.107，当前不能自动收发。"}


class StaleConversation(ValueError):
    pass


@dataclass(frozen=True)
class Message:
    identity: str
    text: str
    author: str
    kind: str


class WeChatBridge:
    def __init__(self, target, adapter="wxauto4"):
        self.target = target
        self.adapter = adapter
        self.client = None
        self.pythoncom = None

    def connect(self):
        info = compatibility(self.adapter)
        if not info["ready"]:
            raise ValueError(info["message"])
        import pythoncom
        self.pythoncom = pythoncom
        pythoncom.CoInitialize()
        library = importlib.import_module(self.adapter)
        if hasattr(library, "WxParam"):
            library.WxParam.ENABLE_FILE_LOGGER = False
        self.client = library.WeChat(resize=False)
        self.client.ChatWith(who=self.target, exact=True)
        self.check_target()

    def check_target(self):
        info = self.client.ChatInfo()
        if info.get("chat_name") != self.target or info.get("chat_type") != "friend":
            raise ValueError("当前窗口不再是指定的私聊，已暂停。请使用不重复的微信备注名。")

    def snapshot(self):
        self.check_target()
        result = []
        for msg in self.client.GetAllMessage():
            author = str(getattr(msg, "attr", ""))
            kind = str(getattr(msg, "type", ""))
            identity = str(getattr(msg, "id", "") or "")
            if author in ("friend", "self") and not identity:
                raise ValueError("连接器未提供稳定消息标识，无法可靠去重，已暂停。")
            if identity:
                result.append(Message(identity, str(getattr(msg, "content", "") or ""), author, kind))
        self.check_target()
        return result

    def send(self, text, expected_identity):
        self.check_target()
        latest = self.snapshot()
        if not latest or latest[-1].identity != expected_identity:
            raise StaleConversation("新消息已到达，草稿将重新生成。")
        editor = getattr(self.client, "editbox", None)
        if editor is None:
            # wxauto4 exposes ChatBox in its package interface. Accept only a
            # unique edit control; ambiguous or unavailable controls stop sends.
            box = getattr(self.client, "ChatBox", None)
            try:
                candidates = [value for value in vars(box).values()
                              if getattr(value, "ControlTypeName", "") == "EditControl"]
            except TypeError:
                candidates = []
            if len(candidates) == 1:
                editor = candidates[0]
        if editor is None:
            raise ValueError("连接器无法校验输入框，未发送。")
        try:
            value = editor.GetValuePattern().Value
        except Exception:
            try:
                value = editor.GetTextPattern().DocumentRange.GetText(-1)
            except Exception:
                raise ValueError("无法确认微信输入框为空，未发送。") from None
        if value and value.strip():
            raise ValueError("检测到你正在编辑微信消息，已交还控制权。")
        self.check_target()
        result = self.client.SendMsg(msg=text, clear=True)
        if not result:
            raise ValueError("微信未确认发送成功，已暂停且不会自动重发。")

    def close(self):
        if self.pythoncom:
            self.pythoncom.CoUninitialize()
            self.pythoncom = None


"""Pure read-only visual contracts. No model, capture, keyboard or mouse APIs."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass, replace


class VisualPause(ValueError):
    pass


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def inside(self, width, height):
        return 0 <= self.left < self.right <= width and 0 <= self.top < self.bottom <= height

    def overlaps(self, other):
        return self.left < other.right and other.left < self.right and self.top < other.bottom and other.top < self.bottom

    def contains(self, other):
        return self.left <= other.left < other.right <= self.right and self.top <= other.top < other.bottom <= self.bottom

    def tuple(self):
        return self.left, self.top, self.right, self.bottom


@dataclass(frozen=True)
class WindowBinding:
    hwnd: int
    pid: int
    exe: str
    class_name: str
    width: int
    height: int
    dpi: int


@dataclass(frozen=True)
class Calibration:
    binding: WindowBinding
    profile_id: str
    expected_title: str
    title: Box
    messages: Box
    single_chat_confirmed: bool
    target_kind: str = "contact"
    version: int = 1

    def validate(self):
        if self.version != 1 or not self.expected_title.strip() or not self.single_chat_confirmed:
            raise VisualPause("尚未确认唯一单聊目标，未开始读取。")
        if self.target_kind not in ("contact", "filehelper") or not self.profile_id:
            raise VisualPause("视觉目标标识不完整，请重新校准。")
        for box in (self.title, self.messages):
            if not box.inside(self.binding.width, self.binding.height):
                raise VisualPause("校准区域越过微信窗口边界，请重新校准。")
        if self.title.width < 60 or not 18 <= self.title.height <= 160:
            raise VisualPause("请框选完整联系人标题，不含聊天列表和标题栏按钮。")
        if self.messages.width < 220 or self.messages.height < 100 or self.title.bottom > self.messages.top:
            raise VisualPause("消息区应在标题下方，且不包含输入框、工具栏或聊天列表。")
        if self.title.overlaps(self.messages):
            raise VisualPause("标题区和消息区不能重叠。")
        return self

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        # Independent PrintWindow calibration is metadata, NOT legacy client
        # coordinates. Keep the old parser usable without activating new ROIs.
        data.pop("maximized_calibration", None)
        data["binding"] = WindowBinding(**data["binding"])
        data["title"] = Box(**data["title"])
        data["messages"] = Box(**data["messages"])
        return cls(**data).validate()


def exact_title(actual, expected):
    # No fuzzy match, substring match, punctuation removal or internal-space
    # removal. Similar-looking names must not authorize reading another chat.
    return unicodedata.normalize("NFC", actual.strip()) == unicodedata.normalize("NFC", expected.strip())


@dataclass(frozen=True)
class Candidate:
    text: str
    direction: str
    kind: str
    box: Box
    fingerprint: str
    uncertainty: str = ""
    identity: str = ""
    origin: str = "候选"
    evidence_boxes: tuple[Box, ...] = ()

    @property
    def safe(self):
        return self.kind == "text_candidate" and self.direction in ("friend", "self") and not self.uncertainty

    @property
    def signature(self):
        return self.direction, self.kind, self.text, self.fingerprint


class ObservationTracker:
    """Occurrence + geometry + crop identity, never text-only deduplication.

    Only unambiguous fixed-position appends are called new. Scrolling, lost
    anchors, reflow, refresh changes and repeated-message ambiguity pause.
    A pixel-identical replacement is observationally indistinguishable; this
    phase does not claim to solve it or permit automated replies.
    """
    def __init__(self):
        self.previous = None
        self.serial = 0

    def _new(self, item, origin):
        self.serial += 1
        return replace(item, identity=f"visual:{self.serial}:{item.fingerprint[:12]}", origin=origin)

    def update(self, rows):
        if self.previous is None:
            result = tuple(self._new(item, "启动基线") for item in rows)
            self.previous = result
            return result, ()
        old = self.previous
        if len(rows) < len(old):
            raise VisualPause("可见消息减少或发生滚动，无法确认新旧，已暂停。")
        if any(item.signature != rows[i].signature or item.box != rows[i].box for i, item in enumerate(old)):
            raise VisualPause("消息位置、内容或画面锚点变化，可能滚动/重排/重复消息，已暂停，不猜测新旧。")
        result = tuple(replace(rows[i], identity=item.identity, origin="已见基线") for i, item in enumerate(old))
        added = tuple(self._new(item, "新增候选") for item in rows[len(old):])
        result += added
        self.previous = result
        return result, added


class RevisionGate:
    """Future draft binding; phase one never creates a sendable draft."""
    def __init__(self):
        self.revision = 0
        self.active = False

    def start(self):
        self.revision += 1
        self.active = True
        return self.revision

    def invalidate(self):
        self.revision += 1

    def stop(self):
        self.active = False
        self.invalidate()

    def accepts(self, token):
        return self.active and token == self.revision


def candidate_fingerprint(pixels, direction, text):
    payload = direction.encode() + b"\0" + text.encode("utf-8") + b"\0" + pixels
    return hashlib.sha256(payload).hexdigest()


def save_calibration(path, calibration):
    calibration.validate()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(calibration.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)

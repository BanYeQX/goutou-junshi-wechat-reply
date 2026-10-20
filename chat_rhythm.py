"""聊天节奏 K 线: daily candles of who sent the messages. No model, no content.

CSV format is the jev-chat one (MIT, github.com/shengjidaguai-china/
goutoujunshi-jev-chat, examples/relationship_cases), so their example files
import as they are: header `timestamp,sender,message`, `sender` is `me` or
`other`, ISO timestamps in ascending order. This is an independent
implementation of the same idea:

  a running balance starts at 0; each message from `other` is +1, from `me`
  is -1. One candle per calendar day that has messages: open = balance before
  the day's first message, close = after its last, high/low include the open.

The curve shows how the direction of messages moves. It is not a score of the
relationship, of interest or of affection - the UI says so next to it.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import io
from pathlib import Path

MAX_BYTES = 4_000_000
MAX_ROWS = 20_000
REQUIRED = ("timestamp", "sender", "message")
DISCLAIMER = "曲线只反映消息方向的变化（对方发得多 ↑，我发得多 ↓），不代表关系质量、好感或爱意分数。"


class RhythmError(ValueError):
    pass


@dataclass(frozen=True)
class Candle:
    day: str
    open: int
    high: int
    low: int
    close: int
    mine: int = 0           # messages I sent that day ("volume")
    theirs: int = 0         # messages they sent that day


def candles(events) -> tuple[Candle, ...]:
    """events: (day "YYYY-MM-DD", sender "me"/"other") in chronological order."""
    out = []
    balance = 0
    current = None
    for day, sender in events:
        if sender not in ("me", "other"):
            raise RhythmError("sender 只能是 me 或 other")
        if current is None or current["day"] != day:
            if current is not None:
                out.append(Candle(**current))
            current = dict(day=day, open=balance, high=balance, low=balance, close=balance,
                           mine=0, theirs=0)
        balance += 1 if sender == "other" else -1
        current["high"] = max(current["high"], balance)
        current["low"] = min(current["low"], balance)
        current["close"] = balance
        current["mine" if sender == "me" else "theirs"] += 1
    if current is not None:
        out.append(Candle(**current))
    return tuple(out)


def parse_csv_text(text: str) -> tuple[Candle, ...]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or any(name not in reader.fieldnames for name in REQUIRED):
        raise RhythmError("CSV 需要三列表头：timestamp,sender,message")
    events = []
    previous = None
    for number, row in enumerate(reader, start=2):
        if len(events) >= MAX_ROWS:
            raise RhythmError(f"记录超过 {MAX_ROWS} 条，请拆分后再导入")
        stamp = (row.get("timestamp") or "").strip()
        sender = (row.get("sender") or "").strip()
        message = (row.get("message") or "").strip()
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            raise RhythmError(f"第 {number} 行时间无法识别（要 ISO 格式，如 2026-10-20 14:05）") from None
        moment = moment.replace(tzinfo=None)      # only the calendar day is used
        if sender not in ("me", "other") or not message:
            raise RhythmError(f"第 {number} 行：sender 要是 me 或 other，message 不能为空")
        if previous and moment < previous:
            raise RhythmError(f"第 {number} 行时间比上一行早：请按时间先后排序")
        previous = moment
        events.append((moment.date().isoformat(), sender))
    if not events:
        raise RhythmError("CSV 里没有聊天记录")
    return candles(events)


def load_csv(path) -> tuple[Candle, ...]:
    path = Path(path)
    if path.suffix.lower() != ".csv" or not path.is_file():
        raise RhythmError("请选择一个 .csv 文件")
    if path.stat().st_size > MAX_BYTES:
        raise RhythmError("文件超过 4 MB，请拆分后再导入")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RhythmError("请把 CSV 另存为 UTF-8 编码") from None
    try:
        return parse_csv_text(text)
    except csv.Error:
        raise RhythmError("CSV 格式无法读取") from None


# Five fictional patterns, written for this project: per day, a string of who
# sent each message in order ("o" = other, "m" = me). Computed by candles(),
# never hard-coded, so the demo is exactly what an import would draw.
DEMOS = {
    "双向升温": ["mo", "mom", "omom", "omoo", "momoo", "omooo", "omoom", "oomoo"],
    "热聊后降温": ["omomomoo", "omomoomo", "omomom", "mom", "mm", "mmo", "mm", "m"],
    "冲突后修复": ["omom", "omo", "mmmm", "mm", "mom", "omom", "omoo", "oomo"],
    "忙但仍兑现": ["mo", "o", "mo", "om", "o", "mo", "om", "o"],
    "明确边界后收线": ["omom", "mom", "mm", "mo", "m", "", "", ""],
}


def demo(name: str, start: date = date(2026, 1, 1)) -> tuple[Candle, ...]:
    if name not in DEMOS:
        raise RhythmError("没有这个示例")
    events = [((start + timedelta(days=i)).isoformat(), "other" if ch == "o" else "me")
              for i, day in enumerate(DEMOS[name]) for ch in day]
    return candles(events)

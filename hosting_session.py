"""Session: turns captured frames into Observations and drives the agent loop.

Every side effect is injected, so the whole thing runs offline with synthetic
frames. The real runner passes the calibrated geometry, the capture adapter, an
OCR reader, the clipboard readback and the sender.

What this layer is responsible for (and what has failed before):
  * rejecting frames when the window geometry no longer matches the calibration;
  * turning a raw frame into "new incoming text / outgoing bubble / nothing";
  * never inventing a message out of interface noise.

The decision itself lives in hosting_loop, not here.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Iterable

from hosting_loop import Decision, Loop, Observation


@dataclass(frozen=True)
class Geometry:
    window_rect: tuple          # (l, t, r, b) measured when calibrated
    messages_frame: tuple       # (l, t, r, b) inside the window canvas
    change_threshold: float = 0.004


@dataclass(frozen=True)
class Reading:
    """What the reader found in the newest block of the message area."""
    text: str | None = None
    incoming: bool | None = None
    geometry_ok: bool = True
    title_ok: bool = True
    scrolling: bool = False
    unreadable: bool = False
    title_seen: str = ""
    # Texts of the blocks right above the newest one, nearest first, produced
    # lazily (each costs an OCR call). None means the reader does not check.
    above: Callable[[], Iterable[str]] | None = None


class Session:
    def __init__(self, *, capture, read, geometry, loop=None, clock=time.monotonic):
        self.capture = capture          # () -> frame
        self.read = read                # (frame, changed) -> Reading
        self.geometry = geometry
        self.loop = loop or Loop()
        self.clock = clock
        self._last = None
        self.history = []

    # ------------------------------------------------------------------ public

    def step(self) -> tuple[Decision, str]:
        frame = self.capture()
        change = self._change(frame)
        # Only ask what the newest block is when the message area actually
        # changed. An unchanged frame describes history, not an event.
        reading = self.read(frame, change > self.geometry.change_threshold)
        if reading.unreadable:
            decision, detail = self.loop.pause("unreadable_new_message")
            self.history.append((decision, detail))
            return decision, detail
        observation = Observation(
            now=self.clock(),
            change=change,
            text=reading.text,
            incoming=reading.incoming,
            outgoing=reading.incoming is False,
            geometry_ok=reading.geometry_ok,
            title_ok=reading.title_ok,
            scrolling=reading.scrolling,
            above=reading.above,
        )
        decision, detail = self.loop.observe(observation)
        self.history.append((decision, detail))
        return decision, detail

    def generation_finished(self, text):
        return self.loop.generation_finished(text)

    def generation_skipped(self):
        return self.loop.generation_skipped()

    def readback_finished(self, matched, reason):
        return self.loop.readback_finished(matched, reason)

    def commit_finished(self, delivered):
        return self.loop.commit_finished(delivered, now=self.clock())

    def resume(self):
        self.loop.resume()
        self._last = None            # resume rebuilds the baseline

    def paused(self):
        return self.loop.pause_reason

    # ----------------------------------------------------------------- private

    def _change(self, frame) -> float:
        previous, self._last = self._last, frame
        if previous is None:
            return 0.0
        if previous.size != frame.size:
            return 1.0               # a resize is a change, never a message
        left, top, right, bottom = self.geometry.messages_frame
        box = (max(0, left), max(0, top), min(frame.width, right), min(frame.height, bottom))
        old = previous.crop(box).tobytes()
        new = frame.crop(box).tobytes()
        if len(old) != len(new) or not old:
            return 1.0
        differing = sum(1 for a, b in zip(old, new) if a != b)
        return differing / len(old)


# The newest bubble's bottom padding must stay within this many pixels of the
# anchor. Glyph-only reads move the lowest ink row by a few pixels between
# messages; a message such as "一" or "……" can move it more and is then held as
# "scrolled" until the next message (safe direction).
BOTTOM_GAP_TOLERANCE = 6
# Less blank than this under the lowest ink row: a message is cut by the edge
# of the message area, which only happens while scrolled up.
MIN_BOTTOM_GAP = 4


class BottomAnchor:
    """Is the message area showing the bottom of the conversation?

    WeChat leaves a fixed padding under the newest bubble when the view is at
    the bottom. Scrolled up, the strip is filled with history and the padding is
    whatever the scroll position left. `expected` is that padding: pinned from
    the calibration when known, otherwise learned from the first frame - which
    is why hosting must be started at the bottom of the chat.

    The gap alone can match by coincidence when a scroll ends exactly one
    message spacing away; the loop's continuity check covers that case.
    """

    def __init__(self, expected=None, *, tolerance=BOTTOM_GAP_TOLERANCE,
                 minimum=MIN_BOTTOM_GAP):
        self.expected = expected
        self.tolerance = tolerance
        self.minimum = minimum
        self.started = expected is not None
        self.last_gap = None

    def scrolled(self, gap) -> bool:
        self.last_gap = gap
        if not self.started:
            # Learn once, from the first frame only. Learning later could learn
            # a scrolled position. An unusable first frame leaves no anchor and
            # every frame reads as scrolled: the runner refuses to start.
            self.started = True
            if gap is not None and gap >= self.minimum:
                self.expected = gap
        if self.expected is None or gap is None or gap < self.minimum:
            return True
        return abs(gap - self.expected) > self.tolerance


def passes_geometry(window_rect, expected) -> bool:
    return tuple(window_rect) == tuple(expected)

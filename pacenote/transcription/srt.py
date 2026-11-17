"""SRT parsing and formatting: each spoken language's cues as a session writes them (`<locale>.srt`, on the
session clock), read back by a replay and by the plan writer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TS = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?")
_ARROW = "-->"


@dataclass(frozen=True)
class Cue:
    index: int
    start: float  # seconds
    end: float  # seconds
    text: str


def parse_timestamp(value: str) -> float:
    m = _TS.search(value)
    if not m:
        raise ValueError(f"bad SRT timestamp: {value!r}")
    h, mnt, s, ms = m.groups()
    frac = int((ms or "0").ljust(3, "0"))
    return int(h) * 3600 + int(mnt) * 60 + int(s) + frac / 1000


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_clock(seconds: float) -> str:
    """mm:ss (or h:mm:ss) for display and prompts."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_block(block: str) -> Cue | None:
    lines = [ln.strip() for ln in block.strip().split("\n")]
    lines = [ln for ln in lines if ln]
    arrow_at = next((i for i, ln in enumerate(lines) if _ARROW in ln), None)
    if arrow_at is None:
        return None
    try:
        left, right = lines[arrow_at].split(_ARROW, 1)
        start, end = parse_timestamp(left), parse_timestamp(right)
    except ValueError:
        return None
    index = 0
    if arrow_at > 0 and lines[arrow_at - 1].isdigit():
        index = int(lines[arrow_at - 1])
    text = " ".join(lines[arrow_at + 1 :]).strip()
    if not text:
        return None
    return Cue(index=index, start=start, end=end, text=text)


def parse_srt(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    blocks = [b for b in re.split(r"\n[ \t]*\n", text) if b.strip()]
    return [c for c in (_parse_block(b) for b in blocks) if c is not None]


def load_srt(path: str | Path) -> list[Cue]:
    return parse_srt(Path(path).read_text(encoding="utf-8", errors="replace"))



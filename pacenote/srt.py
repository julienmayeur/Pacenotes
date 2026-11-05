"""SRT parsing, including incremental tailing of a file that is still being written.

`yap dictate --srt` writes cues to stdout as speech is recognised. We redirect that
stdout to a file and tail it, consuming only *complete* cues (a cue is complete
once it is followed by a blank line), so a half-written cue is never read.
"""

from __future__ import annotations

import codecs
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


def _split_complete(buffer: str, final: bool) -> tuple[list[str], str]:
    """Split buffer into complete blocks + remainder that may still be growing."""
    parts = re.split(r"\n[ \t]*\n", buffer)
    if final:
        return [p for p in parts if p.strip()], ""
    # The last part is not terminated by a blank line yet: keep it for later.
    return [p for p in parts[:-1] if p.strip()], parts[-1]


def parse_srt(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    blocks, _ = _split_complete(text, final=True)
    return [c for c in (_parse_block(b) for b in blocks) if c is not None]


def load_srt(path: str | Path) -> list[Cue]:
    return parse_srt(Path(path).read_text(encoding="utf-8", errors="replace"))


class SrtTailer:
    """Incrementally read new complete cues from an SRT file that is being appended to."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._offset = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._seen: set[tuple[int, float, str]] = set()

    def poll(self, final: bool = False) -> list[Cue]:
        if self.path.exists():
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
            self._offset += len(chunk)
            if chunk:
                text = self._decoder.decode(chunk)
                self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        if self._buffer.startswith("﻿"):
            self._buffer = self._buffer[1:]
        blocks, self._buffer = _split_complete(self._buffer, final=final)
        cues: list[Cue] = []
        for block in blocks:
            cue = _parse_block(block)
            if cue is None:
                continue
            key = (cue.index, cue.start, cue.text)
            if key in self._seen:  # tolerate re-emitted cues
                continue
            self._seen.add(key)
            cues.append(cue)
        return cues

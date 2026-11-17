"""Dated knowledge briefs the plan writer receives instead of relying on what a model remembers.

pacenote/knowledge/data/
  capabilities.md   "what agents can do today": the plan never claims a capability absent from it,
                    and its per-use-case model choice leans on it; the plan footer states its date
  delivery-practice.md   "how value is proven": baselines, pilot targets, first verified win, kill
                    criteria, buy before build

Each file starts with YAML front matter (`as_of:`, `sources:`). The shipped briefs are tracked; a brief in
the local knowledge folder (knowledge.local_dir) wins over the shipped one, and that is where
`pacenote capabilities refresh` (refresh.py) writes. A brief older than STALE_DAYS is flagged by
`pacenote check` and in the Plan view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from pacenote.knowledge import SHIPPED_DIR, local_dir

KNOWLEDGE_DIR = SHIPPED_DIR  # the shipped briefs
BRIEFS = {"capabilities": "capabilities.md", "delivery-practice": "delivery-practice.md"}
STALE_DAYS = 30
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass
class Brief:
    name: str
    path: Path
    as_of: date | None
    body: str
    sources: list[dict] = field(default_factory=list)
    title: str = ""

    def age_days(self, today: date | None = None) -> int | None:
        return None if self.as_of is None else ((today or date.today()) - self.as_of).days

    def stale(self, today: date | None = None, days: int = STALE_DAYS) -> bool:
        age = self.age_days(today)
        return age is None or age > days

    @property
    def as_of_text(self) -> str:
        return self.as_of.isoformat() if self.as_of else "unknown"


def knowledge_dir() -> Path:
    """Where briefs are written (the local knowledge folder)."""
    return local_dir()


def parse_brief(name: str, text: str, path: Path) -> Brief:
    m = _FRONT.match(text)
    meta, body = ({}, text) if not m else (yaml.safe_load(m.group(1)) or {}, m.group(2))
    as_of = meta.get("as_of")
    if isinstance(as_of, str):
        try:
            as_of = date.fromisoformat(as_of.strip())
        except ValueError:
            as_of = None
    return Brief(name=name, path=path, as_of=as_of if isinstance(as_of, date) else None, body=body.strip(),
                 sources=[s for s in meta.get("sources") or [] if isinstance(s, dict)], title=str(meta.get("title") or ""))


def brief_path(name: str) -> Path:
    """The brief in force: the local one when this machine refreshed it, else the shipped one."""
    local = knowledge_dir() / BRIEFS[name]
    return local if local.exists() else KNOWLEDGE_DIR / BRIEFS[name]


def load_brief(name: str, directory: Path | None = None) -> Brief | None:
    path = Path(directory) / BRIEFS[name] if directory else brief_path(name)
    try:
        return parse_brief(name, path.read_text(encoding="utf-8"), path)
    except OSError:
        return None


def capabilities_as_of() -> str:
    b = load_brief("capabilities")
    return b.as_of_text if b else "unknown"


def stale_briefs(today: date | None = None) -> list[Brief]:
    out = []
    for name in BRIEFS:
        b = load_brief(name)
        if b is None or b.stale(today):
            out.append(b or Brief(name=name, path=knowledge_dir() / BRIEFS[name], as_of=None, body=""))
    return out


def render_brief(name: str, as_of: str, title: str, body: str, sources: list[dict]) -> str:
    meta = {"as_of": as_of, "title": title, "sources": [{"title": s.get("title", ""), "url": s.get("url", "")}
                                                          for s in sources]}
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=120) + "---\n\n" + body.strip() + "\n"

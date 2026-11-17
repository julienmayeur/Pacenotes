"""What the plan writer reads: the dated knowledge briefs and the recipe library.

The shipped versions live in `data/` (tracked). What this machine writes at run time (a refreshed brief,
evidence recorded on a recipe) goes to the local knowledge folder instead, outside the repository, and wins
over the shipped file of the same name.
"""

from __future__ import annotations

import os
from pathlib import Path

SHIPPED_DIR = Path(__file__).parent / "data"


def local_dir() -> Path:
    """Where `pacenote capabilities refresh` and `pacenote recipes record` write: PACENOTE_KNOWLEDGE_DIR,
    default ~/.pacenote/knowledge (never inside the repository)."""
    return Path(os.environ.get("PACENOTE_KNOWLEDGE_DIR") or Path.home() / ".pacenote" / "knowledge")

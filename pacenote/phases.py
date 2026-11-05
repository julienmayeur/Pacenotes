"""The interview's backbone: seven ordered phases, enforced in code (state.py).

The phases are an internal structure, never a form read out to the CEO. Inside a phase the
questions stay conversational; between phases the focus only moves forward. Phases 1 to 4 ship
with the app as core questions (core/questions.yaml, merged into every client's questionnaire by
clients.tailor_questionnaire); a client questionnaire keeps the opening (wealth), the use-case
walkthroughs (deep-dives) and the return questions.

Each phase is framed by value, from the lens the whole interview follows: where the company makes
money, where the signal (information) travels and gets lost, where expertise is spent on
low-value work. Titles shown to people come from the locale files (`phases.<id>`); the English
goals below are for the model only.
"""

from __future__ import annotations

PHASE_IDS = ("wealth", "signal-carriers", "signal-flow", "knowledge", "decisions", "deep-dives", "return")
PHASE_INDEX = {p: i for i, p in enumerate(PHASE_IDS)}
CORE_PHASES = ("signal-carriers", "signal-flow", "knowledge", "decisions")  # shipped with the app
DEEP_DIVES = "deep-dives"

# Default minutes for a 15-minute interview; scaled to target_minutes, overridable per questionnaire
# (`phase_minutes:`).
DEFAULT_MINUTES = {"wealth": 1.5, "signal-carriers": 1.5, "signal-flow": 3.0, "knowledge": 2.0,
                   "decisions": 2.0, "deep-dives": 4.0, "return": 1.0}

PHASE_GOALS = {
    "wealth": "where the company makes and loses money, and what limits its growth today (capacity: who would "
              "be swamped first if volume doubled, what eats the team's time)",
    "signal-carriers": "who carries the signal: the people around the CEO, who reports to him, how many people "
                       "come to him each week and for what, who holds the keys to the business",
    "signal-flow": "how the signal travels between people and departments: what passes, through which channel "
                   "(messaging, email, phone, fax, meetings, paper), how often, who waits on whom and how long; "
                   "the two CEO patterns: work waiting on the CEO (approvals, prices, answers), and the CEO "
                   "having to ask someone who opens a spreadsheet to know where things stand",
    "knowledge": "where knowledge lives and how it is reused: where the document or figure was the last time it "
                 "was needed (inbox, shared drive, Excel, ERP, someone's head), who found it, how long it took; "
                 "what is confidential and what extract others need; unwritten rules and expertise that live in "
                 "one person's head",
    "decisions": "where expertise decides: the recurring decisions (orders, suppliers, prices, the weekly "
                 "meeting), who decides, what context they arrive with, time spent catching up vs deciding; for "
                 "key people, the share of time on judgment vs repetitive glue (chasing status, re-keying, "
                 "jumping between systems)",
    "deep-dives": "walkthroughs of the concrete workflows (use cases), drilled through the integration slots, "
                  "linked to the people, channels and tools already mapped",
    "return": "the expected return: what the freed time would allow, with a number and a date, who signs off, "
              "budget range, day-to-day contact",
}

# The maps each phase fills (see contract.MapsUpdate); a phase whose maps hold enough facts can end
# once at least one of its questions was asked.
PHASE_MAPS = {
    "signal-carriers": ("org",),
    "signal-flow": ("flows",),
    "knowledge": ("stores", "catalog"),
    "decisions": ("decisions",),
}
MAP_ENOUGH = {"org": 3, "flows": 3, "stores": 2, "catalog": 3, "decisions": 2}  # facts per map


def valid(phase: str | None) -> bool:
    return phase in PHASE_INDEX


def default_phase(theme_id: str, question_id: str = "", use_case: str | None = None) -> str:
    """Where a question of a questionnaire written before phases belongs: a use-case walkthrough
    is a deep dive; capacity / strategy / growth questions open the interview; ROI and budget close
    it; anything else (IT, data, pre-plan ideas) is a deep dive."""
    if use_case:
        return DEEP_DIVES
    key = f"{theme_id} {question_id}".lower()
    if any(w in key for w in ("roi", "budget", "return")):
        return "return"
    if any(key.startswith(w) or f" {w}" in key for w in ("capacity", "strategy", "growth", "wealth")):
        return "wealth"
    return DEEP_DIVES


def budgets(target_minutes: float, overrides: dict[str, float] | None = None) -> dict[str, float]:
    """Seconds per phase: the defaults (or the questionnaire's `phase_minutes`), scaled so they add
    up to the interview's target length."""
    minutes = {p: float((overrides or {}).get(p, DEFAULT_MINUTES[p])) for p in PHASE_IDS}
    total = sum(minutes.values()) or 1.0
    scale = float(target_minutes) / total
    return {p: round(m * scale * 60.0, 1) for p, m in minutes.items()}


def next_phase(phase: str | None) -> str | None:
    i = PHASE_INDEX.get(phase or "", -1) + 1
    return PHASE_IDS[i] if i < len(PHASE_IDS) else None

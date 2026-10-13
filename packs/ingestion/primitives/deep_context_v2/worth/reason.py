"""The reason a worth verdict carries, in plain words, from worth's own yes/no answers.

Six of worth's questions (worth_questions.json) explain it. A real relationship, a work signal and
professional standing point toward yes; noise and a transactional-only exchange toward no; thin
evidence toward maybe. The reason names what points toward the verdict and holds, then what would
have pointed away from it and is absent.

Created: 2026-10-06
Changelog:
  2026-10-13: from worth's answers alone: the frozen model and its contributions are gone with JEV.
"""
from __future__ import annotations

from packs.ingestion.primitives.deep_context_v2.worth.luna import Labels

CLEAR_HIGH = 0.6
CLEAR_LOW = 0.4

# question -> (phrase when it holds, phrase after "little evidence of")
PHRASES: dict[str, tuple[str, str]] = {
    "real_relationship": ("direct correspondence", "an established relationship"),
    "work_signal": ("work-related contact", "work-related context"),
    "professional_standing": ("an established professional background", "an established professional background"),
    "noise": ("unsolicited outreach or broadcasts", "unsolicited outreach or broadcasts"),
    "transactional_only": ("mainly transactional contact", "transactional contact"),
    "evidence_incomplete": ("limited context about the relationship", "gaps in the relationship context"),
}
TOWARD_YES = ("real_relationship", "work_signal", "professional_standing")
TOWARD_NO = ("noise", "transactional_only")
# verdict -> (what points toward it, what would have pointed away from it)
EXPLAINS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "yes": (TOWARD_YES, TOWARD_NO),
    "no": (TOWARD_NO, TOWARD_YES),
    "maybe": (("evidence_incomplete",), ()),
}


def reason(labels: Labels, verdict: str) -> str:
    """"Looks like ..." for what holds, "There's little evidence of ..." for what is absent."""
    toward, away = EXPLAINS[verdict]
    # A question Luna declined says nothing either way.
    holds: list[str] = [PHRASES[name][0] for name in toward if name in labels and float(labels[name]) >= CLEAR_HIGH]
    absent: list[str] = [PHRASES[name][1] for name in away if name in labels and float(labels[name]) <= CLEAR_LOW]
    sentences: list[str] = []
    if holds:
        sentences.append("Looks like " + _joined(holds, " and ") + ".")
    if absent:
        sentences.append("There's little evidence of " + _joined(absent, ", or ") + ".")
    if not sentences:
        return "The signals give no clear explanation to single out."
    return " ".join(sentences)


def _joined(parts: list[str], last_joiner: str) -> str:
    """"a", "a and b", "a, b and c"."""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + last_joiner + parts[-1]

"""The reason a worth verdict carries, in plain words, from the detectors' probabilities.

Worth no names the detector that fired, with its probability; worth yes says nothing stood out,
or names the closest call when one detector came near the threshold, so the owner sees why.

Created: 2026-10-06
Changelog:
  2026-10-13: from worth's answers alone: the frozen model and its contributions are gone with JEV.
  2026-10-28: from the four detectors; the probability rides along so the page can show it.
"""
from __future__ import annotations

from packs.ingestion.primitives.deep_context_v2.worth.luna import DETECTORS, Labels

# A detector at this probability or above makes the family worth no (the lab on a real store:
# true mailbox 1.00, assistant .92, vendor .81; the one false signal on a kept person was .50).
NO_THRESHOLD = 0.75
# Below the threshold but above this, a yes still names the closest call.
NEAR_MISS = 0.5

PHRASES: dict[str, str] = {
    "broadcast": "a newsletter or automated sender",
    "mailbox": "a shared mailbox or role account",
    "vendor_only": "a vendor the owner only buys from",
    "intermediary": "an assistant acting for someone else",
}
NOTHING = "Nothing automated or transactional stands out."


def detections(labels: Labels) -> list[tuple[str, float]]:
    """Every detector Luna answered, strongest first. A declined detector did not fire."""
    found: list[tuple[str, float]] = []
    for name in DETECTORS:
        if name in labels:
            found.append((name, float(labels[name])))
    found.sort(key=lambda item: -item[1])
    return found


def decide(labels: Labels) -> str:
    """"no" when any detector is at the threshold or above, else "yes"."""
    for _, probability in detections(labels):
        if probability >= NO_THRESHOLD:
            return "no"
    return "yes"


def reason(labels: Labels, verdict: str) -> str:
    """"Looks like a shared mailbox or role account (0.98)." for no; for yes, what came closest, if anything."""
    found = detections(labels)
    if verdict == "no":
        fired = [f"{PHRASES[name]} ({_shown(probability)})" for name, probability in found if probability >= NO_THRESHOLD]
        return "Looks like " + _joined(fired) + "."
    near = [(name, probability) for name, probability in found if probability >= NEAR_MISS]
    if not near:
        return NOTHING
    name, probability = near[0]
    return f"{NOTHING[:-1]}; the closest call was {PHRASES[name]} ({_shown(probability)})."


def _shown(probability: float) -> str:
    """Two decimals, never rounded up: a yes at 0.7499 reads 0.74, not the threshold."""
    return f"{int(probability * 100) / 100:.2f}"


def _joined(parts: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]

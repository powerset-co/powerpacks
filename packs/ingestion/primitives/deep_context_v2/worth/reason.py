"""The reason a JEV worth verdict carries: the answers that pushed hardest toward it, in plain words.

Each question's contribution is its standardized value times the gap between the verdict's weight
and the nearest other class's weight. The three strongest questions with a positive contribution are
phrased from the answer (never from the verdict) and grouped into at most three sentences.

Created: 2026-10-06
"""
from __future__ import annotations

from packs.ingestion.primitives.deep_context_v2.worth.jev import MODEL, Answer, best_index, class_scores, features

REASON_LIMIT = 3
CLEAR_HIGH = 0.6
CLEAR_LOW = 0.4
LITTLE = "little indication of "
UNCERTAIN = "uncertain evidence of "
SOME_UNCERTAINTY = "some uncertainty about "

# question -> (phrase when the answer is high, phrase after "little indication of")
NOUL_PHRASES: dict[str, tuple[str, str]] = {
    "is_family": ("a family connection", "a family connection"),
    "is_close_friend": ("a close friendship", "a close friendship"),
    "is_founder": ("a founder background", "a founder background"),
    "is_investor": ("an investing background", "an investing background"),
    "is_coworker_current": ("a current coworker relationship", "a current coworker relationship"),
    "is_coworker_past": ("a former coworker relationship", "a former coworker relationship"),
    "is_classmate": ("a shared school background", "a shared school background"),
    "is_mentor_or_advisor": ("a mentoring or advisory relationship", "a mentoring or advisory relationship"),
    "is_client": ("a client relationship", "a client relationship"),
    "is_recruiter": ("recruiting activity", "recruiting activity"),
    "is_service_provider": ("a service-provider relationship", "a service-provider relationship"),
    "is_automated_sender": ("automated messages", "automated messages"),
    "is_stranger": ("unsolicited contact", "unsolicited contact"),
    "is_transactional": ("mainly transactional contact", "transactional contact"),
    "transactional_only": ("mainly transactional contact", "transactional contact"),
    "is_professional": ("work-related contact", "work-related context"),
    "work_signal": ("work-related contact", "work-related context"),
    "is_personal": ("a personal relationship", "a personal relationship"),
    "is_vendor_or_partner": ("a vendor or business partnership", "a vendor or business partnership"),
    "is_mentee_or_report": ("someone you mentor or manage", "a mentoring or reporting relationship"),
    "is_neighbor_or_local": ("a connection through your local community", "a local connection"),
    "met_in_person": ("having met in person", "having met in person"),
    "owner_would_intro": ("someone you would introduce", "comfort making an introduction"),
    "they_would_take_owner_call": ("someone who would take your call", "a connection strong enough for a call"),
    "notable": ("public recognition in their field", "public recognition in their field"),
    "sensitive_context": ("sensitive topics", "sensitive topics"),
    "is_healthcare_legal_or_financial_provider": ("medical, legal, or financial services", "medical, legal, or financial services"),
    "confidential_dealings": ("confidential matters", "confidential matters"),
    "is_minor": ("someone under 18", "the person being under 18"),
    "real_relationship": ("direct correspondence", "an established relationship"),
    "professional_standing": ("an established professional background", "an established professional background"),
    "noise": ("unsolicited outreach or broadcasts", "unsolicited outreach or broadcasts"),
    "evidence_incomplete": ("limited context about the relationship", "gaps in the relationship context"),
}
CHOICE_PHRASES: dict[str, str] = {
    "family": "a family connection", "romantic_partner": "a romantic relationship",
    "close_friend": "a close friendship", "friend": "a friendship", "acquaintance": "a casual acquaintance",
    "colleague": "a coworker relationship", "business_contact": "a work connection",
    "service_provider": "a service-provider relationship", "community": "a community connection",
    "stranger": "unsolicited contact",
    "professional_only": "work-only correspondence", "personal_only": "personal-only correspondence",
    "mixed": "both personal and work-related correspondence",
    "cold_outreach": "an unsolicited introduction", "mutual_friend": "an introduction through a mutual friend",
    "work": "meeting through work", "school": "meeting through school", "online": "meeting online",
    "event": "meeting at an event", "manager": "someone who managed you", "peer": "a peer relationship",
    "report": "someone you managed", "none": "no shared reporting line",
    "executive": "an executive role", "senior": "a senior role", "mid": "an established career",
    "junior": "an early career", "student": "current studies", "retired": "retirement",
}
UNKNOWN_PHRASES: dict[str, str] = {
    "relationship_kind": "relationship context", "mode": "personal or work context",
    "hierarchy": "reporting relationship", "intro_source": "how you met",
    "seniority": "career stage", "function": "work background",
}
WARMTH_PHRASES: tuple[str, ...] = (
    "little relationship beyond automated contact", "a distant acquaintance",
    "a friendly connection", "a close connection", "an inner-circle connection",
)


def _supporting(answers: dict[str, Answer], decision: str) -> list[tuple[str, str, float]]:
    """Per question with a positive total contribution, strongest first: its strongest option ("" on
    a noul answer) and that option's probability. The worth answer itself is left out: it is circular."""
    normalized, scores = class_scores(answers)
    winner: int = MODEL.classes.index(decision)
    other: int = best_index(scores, winner)
    values: dict[str, float] = features(answers)
    totals: dict[str, float] = {}
    strongest: dict[str, tuple[float, str, float]] = {}
    for feature, value, weight, opposing in zip(MODEL.features, normalized, MODEL.coefficients[winner],
                                               MODEL.coefficients[other]):
        # A feature is "<kind>:<question>" or "<kind>:<question>=<option>".
        question_and_option: str = feature.split(":", 1)[1]
        name, _, option = question_and_option.partition("=")
        if name == "worth":
            continue
        contribution: float = value * (weight - opposing)
        totals[name] = totals.get(name, 0.0) + contribution
        if name not in strongest or contribution > strongest[name][0]:
            strongest[name] = (contribution, option, values[feature])
    # Strongest total first; a stable sort keeps the model's order on a tie.
    positive: list[tuple[float, int, str]] = []
    for order, name in enumerate(totals):
        if totals[name] > 0:
            positive.append((-totals[name], order, name))
    positive.sort()
    supporting: list[tuple[str, str, float]] = []
    for _total, _order, name in positive:
        supporting.append((name, strongest[name][1], strongest[name][2]))
    return supporting


def _phrase(name: str, option: str, probability: float) -> str:
    if option == "unknown":
        description: str = "unclear " + UNKNOWN_PHRASES[name]
        if probability >= CLEAR_HIGH:
            return description
        if probability <= CLEAR_LOW:
            return LITTLE + description
        return SOME_UNCERTAINTY + description
    if name == "warmth":
        positive: str = WARMTH_PHRASES[int(option)]
        negative: str = positive
    elif option:
        positive = CHOICE_PHRASES.get(option, option.replace("_", " ") + " work")
        negative = positive
    else:
        positive, negative = NOUL_PHRASES[name]
    if probability >= CLEAR_HIGH:
        return positive
    if probability <= CLEAR_LOW:
        return LITTLE + negative
    return UNCERTAIN + positive


def reason(answers: dict[str, Answer], decision: str) -> str:
    """At most three phrases, strongest first, as one to three sentences grouped by how sure each is."""
    phrases: list[str] = []
    for name, option, probability in _supporting(answers, decision):
        phrase: str = _phrase(name, option, probability)
        if phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) == REASON_LIMIT:
            break
    if not phrases:
        return "The combined signals give no clear explanation to single out."
    # Split each phrase into its certainty prefix ("little indication of ", ...) and the rest.
    by_prefix: dict[str, list[str]] = {"": [], UNCERTAIN: [], SOME_UNCERTAINTY: [], LITTLE: []}
    for phrase in phrases:
        prefix: str = ""
        for candidate in (LITTLE, UNCERTAIN, SOME_UNCERTAINTY):
            if phrase.startswith(candidate):
                prefix = candidate
        by_prefix[prefix].append(phrase[len(prefix):])
    sentences: list[str] = []
    if by_prefix[""]:
        sentences.append("Looks like " + _joined(by_prefix[""], " and ") + ".")
    if by_prefix[UNCERTAIN] or by_prefix[SOME_UNCERTAINTY]:
        sentences.append("The context is less clear about " + _joined(by_prefix[UNCERTAIN] + by_prefix[SOME_UNCERTAINTY], " and ") + ".")
    if by_prefix[LITTLE]:
        sentences.append("There's little evidence of " + _joined(by_prefix[LITTLE], ", or ") + ".")
    return " ".join(sentences)


def _joined(parts: list[str], last_joiner: str) -> str:
    """"a", "a and b", "a, b and c"."""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + last_joiner + parts[-1]

"""Which label badges a person shows on the review cards.

Changelog:
  2026-10-01: moved out of rendering.py, which went with the Jinja review page. The page
    shows the first three titles and folds the rest into "+N".
"""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.db.view_models import ParentViewRow

# 33 badge titles: the 27 share labels plus the 6 worth signals. A person shows
# every title whose probability clears the threshold, highest first.
_LABEL_TITLES = (
    ("is_family", "Family"), ("is_close_friend", "Close friend"),
    ("is_founder", "Founder"), ("is_investor", "Investor"),
    ("is_coworker_current", "Coworker"), ("is_coworker_past", "Former coworker"),
    ("is_classmate", "Classmate"), ("is_mentor_or_advisor", "Mentor / advisor"),
    ("is_client", "Client"), ("is_recruiter", "Recruiter"),
    ("is_service_provider", "Service provider"), ("is_automated_sender", "Automated sender"),
    ("is_stranger", "Stranger"), ("is_transactional", "Transactional"),
    ("is_professional", "Work-related"), ("is_personal", "Personal"),
    ("is_vendor_or_partner", "Vendor / partner"), ("is_mentee_or_report", "Mentee / report"),
    ("is_neighbor_or_local", "Neighbor / local"), ("met_in_person", "Met in person"),
    ("owner_would_intro", "Would introduce"), ("they_would_take_owner_call", "Would take your call"),
    ("notable", "Public figure"), ("sensitive_context", "Sensitive topics"),
    ("is_healthcare_legal_or_financial_provider", "Medical / legal / financial services"),
    ("confidential_dealings", "Confidential"), ("is_minor", "Under 18"),
    ("real_relationship", "Direct contact"), ("work_signal", "Work-related"),
    ("professional_standing", "Established professional"), ("noise", "Spam / broadcasts"),
    ("transactional_only", "Transactional"), ("evidence_incomplete", "Limited context"),
)
_LABEL_THRESHOLD = 0.85


def label_titles(parent: ParentViewRow) -> tuple[str, ...]:
    """Every label badge title that clears the threshold, highest probability first."""

    def sort_key(item: tuple[str, float]) -> float:
        return -item[1]

    labels = dict(parent.labels)
    scores: dict[str, float] = {}
    for key, title in _LABEL_TITLES:
        if key in labels:
            scores[title] = max(scores.get(title, 0.0), float(labels[key]))
    relationship = str(labels.get("relationship_kind") or "")
    if relationship and relationship != "unknown" and "relationship_kind_p" in labels:
        title = relationship.replace("_", " ").capitalize()
        scores[title] = max(scores.get(title, 0.0), float(labels["relationship_kind_p"]))
    return tuple(
        title
        for title, score in sorted(scores.items(), key=sort_key)
        if score >= _LABEL_THRESHOLD
    )

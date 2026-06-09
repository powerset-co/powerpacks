"""Title clustering for role enrichment — ported from prod roles_impl.py.

Clusters raw job titles by regex pattern so each cluster can be sent to
its dedicated LLM prompt for higher-quality classification.
"""
from __future__ import annotations

import re
from typing import Dict, List

# ---------------------------------------------------------------------------
# Cluster patterns — order matters (first match wins).
# ---------------------------------------------------------------------------
CLUSTER_PATTERNS = {
    # Leadership & Executive (have dedicated prompts)
    # vp_level BEFORE c_suite so "Vice President" doesn't match "president" in c_suite
    'vp_level': r'(?i)\b(vp|vice\s+president|evp|svp|avp|gm\b|general\s+manager)\b',
    'c_suite': r'(?i)\b(chief|ceo|cto|cfo|coo|cmo|cpo|cro|cio|cco|cso|cdo|cao|clo|cgo|cxo|csm|ciso|president)\b',
    'founder': r'(?i)\b(founder|co-founder|cofounder|entrepreneur|founding\s+team)\b',
    'investor': r'(?i)\b(partner|investor|venture\s+capital|angel|limited\s+partner|general\s+partner|managing\s+partner|vc\b|pe\b|investment\s+bank\w*|private\s+equity|hedge\s+fund|trader|trading|scout|family\s+office)\b',
    'board_governance': r'(?i)\b(board|advisor|advisory|chairman|chairperson|chair\b|co-chair|observer|mentor|trustee|commissioner)\b',
    'director': r'(?i)\b(director|head\s+of)\b',

    # Technical roles - BEFORE generic manager so "Engineering Manager" -> engineering
    'engineering': r'(?i)\b(engineer|engineering|developer|architect|devops|sre|swe\b|software|backend|frontend|fullstack|full\s+stack|ios\b|android|mobile\s+dev|qa\b|sde|sdet|security|platform|infrastructure|cloud|coder|programmer|data\s+scientist|data\s+science|data|machine\s+learning|ml\b|ai\b|nlp\b|deep\s+learning|data\s+analyst|bi\s+analyst|analytics|research\s+scientist|scientist|science|it\s+technician|webmaster|technical)\b',
    'product': r'(?i)\b(product\s+manager|product\s+management|product\s+owner|product\s+lead|product\s+marketing|group\s+pm|tpm\b|apm\b|product\b)\b',
    'design': r'(?i)\b(designer|design\s+lead|ux\b|ui\b|creative|graphic|visual\s+design)\b',

    # Generic manager (after technical, so "Engineering Manager" matches engineering first)
    'manager': r'(?i)\b(manager|lead\b|team\s+lead|team\s+leader|supervisor|principal|program\s+manager|project\s+leader|group\s+leader)\b',

    # Business functions
    'sales_bd': r'(?i)\b(sales|business\s+development|account\s+executive|ae\b|sdr\b|bdr\b|revenue|go-to-market|gtm\b)\b',
    'business_functions': r'(?i)\b(marketing|growth|brand\b|communications|pr\b|public\s+relations|finance|financial|accountant|accounting|cpa|treasury|audit|controller|operations|ops\b|logistics|supply\s+chain|legal|counsel|attorney|lawyer|paralegal|compliance|human\s+resources|recruiter|recruiting|talent|people\s+ops|hr\b|sourcer|sourcing|headhunter|staffing)\b',

    # General professional
    'general_professional': r'(?i)\b(consult|analyst|specialist|coordinator|associate|strategist|officer|writer|editor|content|copywriter|journalist|assistant|admin|administrator|administrative|representative|rep\b|support|customer\s+service|client\s+success|freelance|contractor|independent|executive|exec|owner)\b',

    # Entry level
    'intern': r'(?i)\bintern\b',

    # Academic
    'academic': r'(?i)\b(adjunct|professor|postdoc|phd|lecturer|teaching\s+assistant|research\s+assistant|fellow|researcher|research\b|student|graduate\s+student|undergrad|teacher|instructor|tutor|educator|visiting\s+scholar|faculty|scholar)\b',

    # Miscellaneous
    'misc': r'(?i)\b(nurse|physician|doctor|therapist|pharmacist|medical|clinical|healthcare|player|athlete|sports|member|coach|staff)\b',
}

SKIPPED_CLUSTERS_DEFAULT = {'misc', 'intern', 'noise'}

NOISE_PATTERNS = [
    r'(?i)^stealth\b',
    r'(?i)\bstealth\s*\(',
    r'(?i)^\d+\s*(under|over)\s*\d+',
    r'(?i)^acquired\b',
    r'(?i)^\d{4}\s+(cohort|graduate|fellow)',
    r'(?i)^\d+$',
    r'(?i)^\d+\+?\s*companies',
    r'(?i)^(yc|y combinator)\s*[wsf]\d{2}',
]

COMPOUND_SEPARATOR_PATTERN = r'(?i)(?:\s+and\s+|\s*&\s*|\s*,\s*|\s*/\s*|\s*\|\s*|\s+-\s+)'

COMPOUND_ROLE_KEYWORDS = [
    r'\b(ceo|cto|cfo|coo|cmo|cpo|cro|cio|ciso|cdo|cso|clo|cgo)\b',
    r'\bchief\s+\w+\s+officer\b',
    r'\bpresident\b',
    r'\b(vp|svp|evp|avp)\b',
    r'\bvice\s+president\b',
    r'\b(director|head\s+of)\b',
    r'\b(gm|general\s+manager)\b',
    r'\b(founder|co-founder|cofounder)\b',
    r'\b(chairman|chairperson|board\s+member|board\s+director)\b',
    r'\b(partner|gp|lp|managing\s+partner|general\s+partner)\b',
]


def is_compound_title(title: str) -> bool:
    if not isinstance(title, str):
        return False
    if not re.search(COMPOUND_SEPARATOR_PATTERN, title):
        return False
    parts = re.split(COMPOUND_SEPARATOR_PATTERN, title)
    if len(parts) < 2:
        return False
    combined_pattern = '|'.join(COMPOUND_ROLE_KEYWORDS)
    parts_with_roles = sum(
        1 for p in parts if p.strip() and re.search(combined_pattern, p.strip(), re.IGNORECASE)
    )
    return parts_with_roles >= 2


def cluster_title(title: str) -> str:
    """Return the cluster name for a single title."""
    if not isinstance(title, str):
        return "other"
    for noise_pattern in NOISE_PATTERNS:
        if re.search(noise_pattern, title):
            return "noise"
    if is_compound_title(title):
        return "compound"
    for cluster_name, pattern in CLUSTER_PATTERNS.items():
        if re.search(pattern, title):
            return cluster_name
    return "other"


def cluster_titles(titles: List[str]) -> Dict[str, List[str]]:
    """Cluster a list of titles. Returns {cluster_name: [titles]}."""
    clusters: Dict[str, List[str]] = {}
    for title in titles:
        cluster = cluster_title(title)
        clusters.setdefault(cluster, []).append(title)
    return clusters

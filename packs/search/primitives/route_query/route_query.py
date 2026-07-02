"""Deterministic query router for the `$search` family.

Encodes the routing heuristic — the AGENTS.md skill-routing prose + the `$search`
SKILL "deep mode / local mode / TurboPuffer mode" rules — as inspectable,
ordered, first-match-wins rules. This is:

  1. the BASELINE the routing eval scores, and
  2. the router `$search` wires up, replacing "agent reads the prose" with a
     deterministic classifier that can be measured and regression-tested.

Routes (the surfaces a people/JD/company query can land on):
  deep      deep JD -> judged shortlist ($search's deep mode): job-posting URL,
            pasted JD, multi-trait role brief, "build a shortlist", "more people like <url>".
  contacts  my/set contacts + contact-field filtering ($search-contacts).
  sql       relational / aggregate / career-shape predicates ($search-sql).
  company   company lookup / ids / investors / funding / sector / company-set ($search-company).
  network   fast people retrieval — the default ($search fast path).

For `network`, a secondary `subroute` (local | turbopuffer) mirrors the $search SKILL's
local-vs-TurboPuffer signals; it is advisory (retrieval env decides the rest).

No LLM, no network, no spend — pure string rules.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict

ROUTES = ("deep", "contacts", "sql", "company", "network")

# --- signal vocabularies (kept explicit + inspectable) -------------------------------------------

# Job-board / ATS hosts that mark a URL as a job posting.
_JOB_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workday", "myworkdayjobs.com",
              "jobs.", "job-boards.", "boards.greenhouse", "careers.", "/careers", "/jobs/")
# JD section markers — a pasted JD usually shows several of these.
_JD_MARKERS = ("responsibilities", "qualifications", "requirements", "about the role",
               "what you'll do", "what you will do", "who you are", "nice to have",
               "minimum qualifications", "preferred qualifications", "you may be a good fit",
               "about the job", "the role", "what we're looking for")
# Natural-language triggers a user might type for a deep JD/shortlist search (matches user input,
# not our feature name).
_DEEP_INTENT = ("build a shortlist", "build me a shortlist", "shortlist of candidates",
                "shortlist", "recruit ", "recruit a", "recruit for", "candidates for",
                "strong candidates", "source candidates", "sourcing for", "fill this role",
                "fits this jd", "fit this jd", "for this role", "for this jd", "hire for",
                "more people like", "people similar to", "people like this", "similar to this profile")

_CONTACTS_INTENT = ("my contacts", "my set contacts", "set contacts", "contacts at", "contacts with",
                    "contacts who", "contacts tagged", "contacts in my", "in my contacts",
                    "contact field", "of my contacts", "filter my contacts", "my contact list")

# Relational / aggregate / career-shape predicates that a single position-row filter can't express.
_SQL_SHAPED = ("overlapped with", "overlap with", "overlapped at", "overlap at", "worked alongside",
               "both worked at", "worked with", "who else worked",
               "became", "went from", "transitioned from", "moved from", "switched from",
               "how many", "most common", "count of", "number of people", "on average",
               "startup stints", "startup stint", "2+ startups", "multiple startups",
               "before age", "in a row", "at two different", "at multiple companies",
               "career", "same company as", "co-workers", "coworkers")
# YOE / stint counting patterns.
_SQL_PATTERNS = (r"\b\d+\+\s*(startups?|companies|stints?|roles?|years)\b",
                 r"\bmore than \d+ (companies|startups?|roles?)\b",
                 r"\b\d+\s*or more (companies|startups?|stints?)\b")

_COMPANY_INTENT = ("company id", "company ids", "look up", "resolve company", "resolve the company",
                   "company set", "raised a series", "raised series", "series a", "series b",
                   "series c", "series d", "backed by", "funded by", "investors in", "investor in",
                   "portfolio of", "portfolio companies", "sector", "in the space", "companies that",
                   "companies in", "find companies", "which companies")
# People nouns — their presence flips a "companies in X" query back to a people search (network).
_PEOPLE_NOUNS = ("people", "person", "who", "engineers", "engineer", "designers", "designer",
                 "managers", "manager", "founders", "founder", "researchers", "scientists",
                 "developers", "candidates", "employees", "staff ", "executives", "leaders",
                 "recruiters", "operators", "investors who", "pms", "product managers")

_LOCAL_SIGNALS = ("local:", "local ", " offline", "my imported", "imported network", "imported contacts")
_TURBOPUFFER_SIGNALS = ("powerset", "team network", "shared network", "the set", " set ", "set id", "set-id")

# Explicit skill-prefix -> route (highest-priority intent signal). $search-network stays a
# recognized deprecated alias for $search; deep JD/shortlist searches fold into $search deep mode.
_PREFIX_ROUTE = {
    "$search-sql": "sql", "$search-company": "company", "$search-contacts": "contacts",
    "$search-network": "network", "$search": "network",
}


@dataclass
class Decision:
    route: str
    rule: str
    subroute: str | None = None


def _has_url(q: str) -> bool:
    return "http://" in q or "https://" in q or bool(re.search(r"\bwww\.", q))


def _looks_like_job_url(q: str) -> bool:
    if not _has_url(q):
        return False
    ql = q.lower()
    return any(h in ql for h in _JOB_HOSTS)


def _looks_like_linkedin_profile(q: str) -> bool:
    return "linkedin.com/in/" in q.lower()


def _looks_like_pasted_jd(q: str) -> bool:
    ql = q.lower()
    hits = sum(1 for m in _JD_MARKERS if m in ql)
    multiline = q.count("\n") >= 2
    # A pasted JD shows several section markers, or is a long multi-line role dump.
    if hits >= 2:
        return True
    return multiline and len(q) > 600 and hits >= 1


def _any(ql: str, needles) -> bool:
    return any(n in ql for n in needles)


def _explicit_prefix(q: str) -> str | None:
    head = q.strip().lower()
    for pfx, route in _PREFIX_ROUTE.items():
        if head.startswith(pfx):
            return route
    return None


def _network_subroute(ql: str) -> str | None:
    if _any(ql, _LOCAL_SIGNALS):
        return "local"
    if _any(ql, _TURBOPUFFER_SIGNALS):
        return "turbopuffer"
    return None


def classify(query: str) -> Decision:
    """Route a natural-language query to a search surface. First matching rule wins."""
    q = query.strip()
    ql = q.lower()
    sub = _network_subroute(ql)

    # 0. Explicit skill prefix is the strongest intent — honor it, except a network prefix carrying
    #    a JD/URL still means the deep-search lane ($search's deep mode).
    prefix = _explicit_prefix(q)
    if prefix:
        if prefix == "network" and (_looks_like_job_url(q) or _looks_like_pasted_jd(q)):
            return Decision("deep", "prefix-network-but-jd", None)
        return Decision(prefix, "explicit-prefix", sub if prefix == "network" else None)

    # 1. DEEP — deep JD -> shortlist: job URL, pasted JD, role brief, shortlist intent, similar-person.
    if _looks_like_job_url(q):
        return Decision("deep", "job-url", None)
    if _looks_like_pasted_jd(q):
        return Decision("deep", "pasted-jd", None)
    if "more people like" in ql and _looks_like_linkedin_profile(q):
        return Decision("deep", "similar-person", None)
    if _any(ql, _DEEP_INTENT):
        return Decision("deep", "deep-intent", None)

    # 2. CONTACTS — the "contacts" noun wins over company/network ("my contacts at X" != "people at X").
    if _any(ql, _CONTACTS_INTENT):
        return Decision("contacts", "contacts-intent", None)

    # 3. SQL — relational / aggregate / career-shape predicates a per-row filter can't express.
    if _any(ql, _SQL_SHAPED) or any(re.search(p, ql) for p in _SQL_PATTERNS):
        return Decision("sql", "relational-predicate", None)

    # 4. COMPANY — subject is COMPANIES (lookup/ids/investors/funding/sector), not people.
    if _any(ql, _COMPANY_INTENT) and not _any(ql, _PEOPLE_NOUNS):
        return Decision("company", "company-subject", None)

    # 5. NETWORK — default fast people retrieval (local vs TurboPuffer decided by env/subroute).
    return Decision("network", "default-people-search", sub)


def main() -> None:
    ap = argparse.ArgumentParser(description="Route a search query to a surface (deep/contacts/sql/company/network).")
    ap.add_argument("--query", required=True)
    args = ap.parse_args()
    d = classify(args.query)
    print(json.dumps({"primitive": "route_query", **asdict(d)}, indent=2))


if __name__ == "__main__":
    main()

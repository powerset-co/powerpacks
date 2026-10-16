"""Original profile evidence shared by the JD capability and candidate judges."""

from __future__ import annotations

import json

_POSITION_FIELDS = (
    ("title", ("title", "position_title")), ("company", ("company", "company_name")),
    ("start", ("start", "start_date")), ("end", ("end", "end_date")),
    ("is_current", ("is_current",)), ("description", ("description",)),
    ("company_description", ("company_description",)), ("headcount", ("headcount", "company_headcount")),
    ("stage", ("stage", "company_stage")), ("funding_total", ("funding_total", "company_funding_total")),
    ("investors", ("investors", "investor_names")),
)


def profile_evidence(person: dict) -> dict:
    """Match qlora-20260913-epoch2-v1 model_text plus ce_expanded_data's city removal."""
    profile = {key: person[key] for key in ("headline", "title", "company", "summary") if person.get(key)}
    if person.get("education"):
        profile["education"] = [{key: entry.get(key) for key in ("school_name", "degree", "field_of_study")}
                                for entry in person["education"]]
    positions, companies = [], {}
    for position in person.get("positions") or []:
        source = {key: None if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}
                  else value for key, value in position.items()}
        role = {key: next((source[alias] for alias in aliases if source.get(alias) is not None), None)
                for key, aliases in _POSITION_FIELDS}
        positions.append({key: role[key] for key in ("title", "company", "start", "end", "is_current", "description")
                          if role[key] is not None})
        context = {key: role[key] for key in ("company", "company_description", "headcount", "stage",
                                            "funding_total", "investors") if role[key] is not None}
        companies[json.dumps(context, sort_keys=True, ensure_ascii=False)] = context
    profile.update(positions=positions, companies=list(companies.values()))
    return profile

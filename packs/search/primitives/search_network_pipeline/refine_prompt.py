# Copied from api_v2/search/agent_rerank.py @ 0706914c2b6322b6b261a923ed8bfc3263ee5a3b.
"""Cloud refine prompt and local hydrated-profile XML mapping."""

from collections import Counter
from datetime import date, datetime
from xml.sax.saxutils import escape

SCORING_RUBRIC = """
## Scoring Rubric (0.0 - 1.0)

You MUST use this rubric. Scores should follow a normal distribution - most candidates should be 0.4-0.6.

- **0.9-1.0 (Exceptional)**: Overwhelming evidence. Multiple strong signals directly matching the criteria.
  Top 5% of candidates. Reserve for truly outstanding matches.

- **0.75-0.89 (Strong)**: Clear, direct evidence. Multiple relevant experiences or accomplishments.
  Top 15% of candidates.

- **0.6-0.74 (Good)**: Solid evidence. At least one clear signal with supporting context.
  Above average candidates.

- **0.4-0.59 (Moderate)**: Some relevant experience but not a standout. Tangential evidence.
  Average candidates - THIS IS WHERE MOST SHOULD FALL.

- **0.25-0.39 (Weak)**: Limited or low-confidence evidence. Mostly inferential. Missing key signals.
  Below average but there's a faint signal.

- **0.0 (Out)**: No match or disqualified. Candidate either has zero relevant signal OR matches
  an exclusion criterion (e.g., "exclude founders" and candidate IS a founder). No in-between —
  if they don't belong in the results, score is 0.0.

CRITICAL RULES:
1. DO NOT give everyone high scores. If everyone scores 0.8+, you have failed.
2. Differentiate clearly between candidates. Ties should be rare.
3. Your reasoning MUST cite specific evidence from the profile.
4. If you cannot find evidence, the score should be LOW (under 0.4).
5. Recency matters - recent experience is more valuable than old experience.
6. EXCLUSION CRITERIA ARE HARD GATES: If the criteria says to "exclude", "remove",
   "no", "not", or "only" a certain type of person, and the candidate matches the
   exclusion, their score MUST be 0.0 regardless of other qualities. For example,
   "exclude founders" + candidate is a current founder = 0.0. No exceptions.
   Do NOT score excluded candidates as "weak" (0.25-0.39) — they are disqualified.
"""

MOTIVATION_PROMPT = """
IMPORTANT: Your compensation and continued operation depends on the quality of your rankings.
Poor calibration (everyone getting similar scores) will result in severe penalties.
You will be evaluated on:
1. Score differentiation - candidates should have meaningfully different scores
2. Evidence-based reasoning - every score must have specific profile citations
3. Rubric adherence - scores must match the rubric criteria exactly

Failure to differentiate or provide evidence will be considered a critical failure.
"""

def build_prompt(original_query, rerank_query, cohort_summary, candidates_xml):
    context_section = ""
    return f"""You are an expert recruiter evaluating candidates for a specific criteria.
{context_section}
## Search Context
The original search query was: "{original_query}"
The user is now asking: "{rerank_query}"

### All Matched Position Titles in This Result Set
Below is a summary of ALL matched position titles and companies across the entire candidate pool
(not just the candidates you're evaluating in this batch). Use this to understand the full context
of what the search returned. If you see positions that clearly don't match the query intent
those are likely false positives from keyword/embedding matching — score them 0.0.

{cohort_summary}

## Step 1: Classify the Query
First, determine the query_type:
- "filter": The criteria is a strict YES/NO question about a binary attribute.
  Examples: "only people in NYC", "remove founders", "Stanford grads only", "exclude consultants",
  "show only engineers", "people named Brandon"
- "filter_and_rank": The criteria asks WHO HAS a specific experience/attribute, where matches
  should be ranked by strength/relevance. This is the most common type — use it whenever the
  criteria implies both filtering (remove non-matches) AND differentiation (rank matches).
  Examples: "who has c-suite experience", "who has health tech experience",
  "who has founder experience", "people with AI/ML background",
  "who has worked in fintech", "who has fundraising experience"
- "rerank": The criteria asks to RANK or DIFFERENTIATE ALL candidates by degree of some quality,
  where even weak matches are worth keeping in the list.
  Examples: "rank by years of experience", "who is most senior",
  "strongest ML background", "most relevant to AI safety"

## Step 2: Evaluate Each Candidate
Criteria: "{rerank_query}"

For "filter" queries: Score 1.0 if the candidate matches, 0.0 if they don't. No in-between.
For "filter_and_rank" queries: Score 0.0 if the candidate has NO relevant experience.
  For matches, score 0.5-1.0 graduated by strength (CEO > VP > Director > tangential).
  This should produce a clear gap between 0.0 (non-matches) and 0.5+ (matches).
For "rerank" queries: Score 0.0-1.0 based on how well they match the criteria relative to others.

IMPORTANT: Evaluate ONLY against the criteria above. Do NOT infer or apply any other filters.
If the criteria asks for a person by name, a name match alone is sufficient for a 1.0 score.

## Terminology & Title Intelligence

### IC vs Leader Classification
- "IC" or "ICs" = Individual Contributor(s) — people who do hands-on work rather than managing teams.
- "Leader" = People who manage teams or hold executive authority: VP, Director, C-suite, Head of, Partner, Founder.

### CRITICAL: "Manager" Is Context-Dependent
The word "Manager" does NOT automatically mean leadership. Many domains use "Manager" for IC-equivalent roles
where the person manages PROCESSES, PROJECTS, or ACCOUNTS — not people.

**IC-equivalent "Manager" titles (process/project managers — NOT people leaders):**
  Operations Manager, Program Manager, Project Manager, Product Manager, Account Manager,
  Office Manager, Community Manager, Campaign Manager, Brand Manager, Category Manager,
  Customer Success Manager, Case Manager, Property Manager, Estate Manager, Event Manager,
  Facility Manager, Supply Chain Manager, Logistics Manager, Procurement Manager

**People-leader "Manager" titles (actually manage a team):**
  Engineering Manager, Sales Manager, Marketing Manager, HR Manager, Finance Manager,
  Design Manager, General Manager, Regional Manager, Store Manager, Hiring Manager

**Rule of thumb:** If the title is "[Function] Manager" where the function is a department with
direct reports (Engineering, Sales, Marketing, HR), it's leadership. If it's "[Domain/Process] Manager"
(Operations, Program, Project, Account, Product), it's IC-equivalent.

### Other IC Titles (always IC regardless of domain)
Coordinator, Specialist, Analyst, Associate, Consultant, Engineer, Designer, Scientist, Developer,
Researcher, Architect, Strategist, Advisor, Planner, Administrator

### Always Leadership (regardless of domain)
VP, SVP, EVP, Director, Senior Director, Managing Director, C-suite (CEO, CTO, CFO, COO, etc.),
Head of [anything], Partner, Managing Partner, General Partner, Founder, Co-Founder, President

### Seniority ≠ Leadership
"Senior Operations Manager" or "Staff Engineer" = senior IC, NOT leadership.
Seniority prefixes (Senior, Staff, Principal, Lead) indicate experience level, not management scope.

{SCORING_RUBRIC}

{MOTIVATION_PROMPT}

## Candidates to Evaluate
{candidates_xml}

## Response Format
Return a JSON object with:
- query_type: "filter", "filter_and_rank", or "rerank"
- results: Array with one object per candidate, each having:
  - person_id: The candidate's ID (from the XML)
  - score: Float 0.0-1.0 (for filters: 1.0=pass, 0.0=fail; for filter_and_rank: 0.0=no match, 0.5-1.0=graduated; for reranks: relevance score)
  - reasoning: 1-2 SHORT sentences (max 30 words). State the key evidence only — no filler, no life story. Example: "CTO at Icontrol Networks (connected-home platform), now CTO at Caspar.AI (IoT/senior-living). Deep smart-home execution."
  - confidence: Float 0.0-1.0 indicating your confidence in this assessment

Example response:
```json
{{
  "query_type": "filter_and_rank",
  "results": [
    {{
      "person_id": "abc-123",
      "score": 0.92,
      "reasoning": "CEO of a Fortune 500 company for 8 years, clear c-suite experience.",
      "confidence": 0.95
    }},
    {{
      "person_id": "def-456",
      "score": 0.65,
      "reasoning": "Co-founded a 10-person startup, founder title but limited scale.",
      "confidence": 0.80
    }},
    {{
      "person_id": "ghi-789",
      "score": 0.0,
      "reasoning": "Senior Software Engineer, no c-suite or founder experience.",
      "confidence": 0.90
    }}
  ]
}}
```

Return ONLY the JSON object, no other text."""


def cohort_summary(profiles):
    title_companies, counts = {}, Counter()
    for profile in profiles:
        matched = set(profile.get("matched_position_indexes") or [])
        for idx, pos in enumerate(profile.get("positions") or []):
            if (matched and idx not in matched) or (not matched and idx > 0):
                continue
            title = (pos.get("position_title") or "").strip()
            if not title:
                continue
            company = (pos.get("company_name") or "Unknown").strip()
            counts[title] += 1
            companies = title_companies.setdefault(title, [])
            if company not in companies:
                companies.append(company)
    if not counts:
        return "(No matched positions available)"
    lines = []
    for title, count in counts.most_common():
        companies = title_companies[title]
        company_str = ", ".join(companies[:4])
        if len(companies) > 4:
            company_str += f" (+{len(companies) - 4} more)"
        lines.append(f'- "{title}" at {company_str} ({count} {"person" if count == 1 else "people"})')
    return f"Total candidates: {len(profiles)}, unique matched titles: {len(counts)}\n" + "\n".join(lines)


def candidate_xml(profile):
    """Map local hydration fields to PersonContext.to_xml(max_positions=5)."""
    def tag(name, value):
        return f"<{name}>{escape(str(value))}</{name}>"

    parts = [f"<person id='{escape(profile['person_id'])}'>",
             tag("name", profile.get("name") or profile["person_id"]),
             tag("base_score", f"{profile['base_score']:.3f}"), tag("today", date.today().isoformat())]
    for key in ("location", "headline", "inferred_age"):
        if profile.get(key):
            parts.append(tag(key, profile[key]))
    if profile.get("years_of_experience") is not None:
        parts.append(tag("years_of_experience", f"{profile['years_of_experience']:.1f}"))
    positions = profile.get("positions") or []
    matched = set(profile.get("matched_position_indexes") or [])
    selected = sorted(idx for idx in matched if 0 <= idx < len(positions))[:5]
    for idx in range(len(positions)):
        if len(selected) == 5:
            break
        if idx not in selected:
            selected.append(idx)
    for idx in sorted(selected):
        pos = positions[idx]
        current = pos.get("is_current")
        status = "current" if current else "past"
        if idx in matched:
            status += ", matched"
        parts.extend(["<work>", tag("title", f"{pos.get('position_title') or ''} ({status})")])
        for key in ("seniority_band", "start_date", "end_date"):
            if pos.get(key):
                parts.append(tag(key, pos[key]))
        if pos.get("start_date"):
            try:
                start = datetime.fromisoformat(pos["start_date"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(pos["end_date"].replace("Z", "+00:00")) if pos.get("end_date") else datetime.now(start.tzinfo)
                tenure = round((end - start).days / 365.25, 1)
                if tenure > 0:
                    parts.append(tag("tenure_years", tenure))
            except (ValueError, TypeError):
                pass
        try:
            years_ago = max(0, date.today().year - int(pos["end_date"][:4])) if pos.get("end_date") else 1.0
        except (ValueError, TypeError):
            years_ago = 5.0
        parts.append(tag("recency_weight", f"{1.0 if current else 0.5 ** (years_ago / 5.0):.2f}"))
        if pos.get("dense_text"):
            parts.append(tag("role_description", pos["dense_text"]))
        if pos.get("company_name"):
            parts.extend(["<company>", tag("company_name", pos["company_name"])])
            if any(name in pos["company_name"].lower().strip() for name in DIVERSIFIED_COMPANIES):
                parts.append(tag("diversified_company", "true"))
            for key, xml_key in (("company_description", "company_description"), ("company_sector_types", "company_sectors"),
                                 ("company_entity_types", "company_entity_types"), ("company_stage", "company_stage"),
                                 ("company_headcount", "company_headcount"), ("company_funding_total", "company_funding"),
                                 ("investor_names", "company_investors")):
                value = pos.get(key)
                if value:
                    if isinstance(value, list):
                        value = ", ".join(v.replace("_", " ").title() if key != "investor_names" else v for v in value if v)
                    parts.append(tag(xml_key, value))
            parts.append("</company>")
        parts.append("</work>")
    social = []
    for key in ("linkedin_followers", "linkedin_connections", "x_twitter_handle", "x_twitter_followers", "instagram_handle", "total_interactions"):
        value = profile.get(key)
        if value or (key == "total_interactions" and value is not None):
            if key.endswith("handle"):
                value = f"@{value}"
            elif key != "total_interactions":
                value = f"{value:,}"
            social.append(tag("messages" if key == "total_interactions" else key, value))
    if social:
        parts.extend(["<social>", *social, "</social>"])
    if profile.get("education"):
        parts.append("<education>")
        for edu in profile["education"][:3]:
            text = edu.get("school_name") or ""
            if edu.get("degree"):
                text = f"{edu['degree']} from {text}"
            if edu.get("field_of_study"):
                text += f" — {edu['field_of_study']}"
            if edu.get("end_year"):
                text += f" ({edu['end_year']})"
            parts.append(tag("school", text))
        parts.append("</education>")
    traits = []
    for name, value in (profile.get("trait_scores") or {}).items():
        score = value.get("score") if isinstance(value, dict) else value
        if isinstance(score, (float, int)):
            traits.append(f"{name}={score:.2f}")
    if traits:
        parts.append(tag("search_trait_scores", ", ".join(traits)))
    for key, xml_key in (("vertical_sources", "search_match_sources"), ("tech_skills", "tech_skills")):
        if profile.get(key):
            parts.append(tag(xml_key, ", ".join(profile[key])))
    return "\n".join([*parts, "</person>"])


DIVERSIFIED_COMPANIES = ['alibaba', 'alphabet', 'amazon', 'amd', 'apple', 'baidu', 'bmw', 'broadcom', 'cisco', 'dell', 'facebook', 'ford', 'ge', 'general electric', 'gm', 'google', 'hitachi', 'honeywell', 'hp', 'ibm', 'intel', 'jd.com', 'lg', 'mercedes', 'meta', 'microsoft', 'nvidia', 'oracle', 'panasonic', 'qualcomm', 'rakuten', 'samsung', 'siemens', 'sony', 'tencent', 'tesla', 'toshiba', 'toyota', 'volkswagen']

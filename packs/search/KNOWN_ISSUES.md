# Search Network — Known Issues & Validation Queue

Tracks user-reported search-quality and pipeline issues, their suspected root
cause, current status, and what needs validation after fixes land.

Last updated: 2026-06-10 (Jake Zeller feedback batch, Jun 3 – Jun 9)

Status legend:
- `fixed-validate` — believed fixed on main; needs a validation run to confirm
- `open` — confirmed gap, not yet fixed
- `partial` — some of the failure mode is addressed, rest open

---

## A. Remote / JD search quality (Hebbia Data Engineer run, Jun 9)

### A1. Founders/executives in JD shortlists — `fixed-validate`
Report: Ivan Shcheklein (founder) shortlisted for a hands-on senior DE role.
Root cause: per-probe rerank scored skill traits independently of seniority;
no seniority-gated final evaluation existed.
Fixed by: `search-profile` skill + `evaluate_profile_candidates` primitive
(seniority hard gate in code: too_senior/too_junior/wrong_track → out), plus
the rerank prompt's EXPLICIT SENIORITY MATCHING section (ported 1:1 from
network-search-api bulk_scoring).
Validate: rerun the Hebbia JD end-to-end; confirm zero current
founders/CEOs/CTOs/VPs/advisors in the sendable shortlist.

### A2. No seniority/role-interpretation confirmation before JD search — `partial`
Report: flow never asked Jake to confirm seniority levels before searching.
Current state: `search-profile` plan preview shows traits + profiles and asks
`Execute this search plan or modify it?`; seniority policy is enforced
silently (deliberately not echoed). The skill says to ask only when the JD
band is genuinely ambiguous.
Gap: "ambiguous" is judgment-based. If users expect an explicit seniority
confirmation for senior/exec-adjacent roles, consider one compact line in the
preview (e.g. `Targeting: senior/staff hands-on ICs`) without reciting the
full policy. Decision needed — do not implement until product call is made.

## B. Local search pipeline failures (Hebbia run fallback chain, Jun 9)

### B1. Local company resolution: `attribute "company_urn" not found in schema` — `fixed-validate`
### B2. Local role retrieval: `attribute "company_name" not found in schema` — `fixed-validate`
Root cause: local DuckDB schema drift vs the prod-shaped query layer.
Believed fixed by the local/prod parity work (#37 `mirror prod local search
execution pools`, local backend split into `primitives/local/` +
`primitives/turbopuffer/` with shared contracts).
Validate: rerun the original failing query against a freshly built local
index:
`using powerset network search across the powerset set, find top 5-10
candidates for <hebbia JD url>` with local routing forced, and confirm
company resolution + role retrieval complete without schema errors.

### B3. MCP semantic search: `sequence item 0: expected str instance, dict found` — `open`
Where: MCP search path (fallback #2 in the failure chain). Type bug joining a
list that contains dicts (likely company/sector filter values or
semantic-query list).
Next step: reproduce via the MCP server with the Hebbia payload from Jake's
attached ledgers; fix the join/serialization at the boundary.

## C. Local interaction counting / ranking (VC-partner search, Jun 3–9)

### C1. Alias merge undercount (Anamitra Banerji) — `open`
Report: `local_person_profiles.all_emails` only kept the Gmail alias
(3 messages); the high-volume work email `anamitra@afore.vc` (180 deduped
messages) was dropped even though directory.csv maps both emails to the same
LinkedIn person at confidence 1.00.
Root cause: alias merge does not propagate all directory-resolved emails into
the merged profile's `all_emails`, and interaction counting joins only
against `all_emails`.
Fix direction (from report, agreed):
- when directory.csv has multiple email source keys confidently mapped to the
  same linkedin/person_id, union all of them into the merged profile's
  `all_emails`
- and/or make the interaction-count layer join against directory-resolved
  aliases (network_contact_sources), not only `local_person_profiles.all_emails`
Acceptance: Anamitra ranks ~top-5 in the SF/Bay VC list with ~183 combined
deduped messages.

### C2. Duplicate-message overcount (Lenny Pruss) — `open`
Report: ~23-25 counted vs 13 actual; same RFC822 messages appear under
multiple msgvault `conversation_id`s and get double-counted.
Current state: `gmail_network_import` counts `COUNT(DISTINCT m.id)` —
distinct msgvault message rows, which does NOT dedupe the same RFC822 message
stored under multiple conversations/accounts.
Fix direction:
- dedupe by `rfc822_message_id` first, then `source_message_id`, then a
  conservative `(sent_at, sender, subject, normalized recipients)` fallback
- count a message only when the person is actual sender or recipient
Acceptance: Lenny = 13 direct messages, not 23.

### C3. Wrong current-position attach (Salil Deshpande) — `open`
Report: `local_person_profiles` says current = General Partner @ Uncorrelated
Ventures, but `local_people_positions` attaches his current GP row to
"PLS, Inc (Sold to Enverus)"; his VC-firm rows are all marked past. A query
requiring a current vc_firm position drops him.
Root cause: indexing-time current-position mapping can disagree with the
profile-level `current_company`/`current_title`. Likely in the
flatten/position-mapping stage of `build_processing_pipeline`.
Fix direction: when profile-level current_company/title disagree with the
position marked current, prefer consistency — either re-attach the current
flag to the position matching the profile current_company, or emit both with
a data-quality flag. Add a validation check that counts such mismatches per
index build.
Acceptance: Salil retrieved for "current VC partners in SF/Bay" and ranked
~18-20 by his 69 deduped interactions.

Related display-side fix already on main: `persist_search_results` now treats
no-end-date positions as current and falls back to most recent dated role
(fixes blank current-role display, not the index-side attach bug).

## D. IC-strictness for explicit IC searches (Nick Clouse, Jun 3)

### D1. seniority_band alone treated as IC proof — `partial`
Report: "Bay Area PM ICs (mid/senior/staff/principal; not manager or
founder)" still returned profiles Jake considers non-IC; ranking favored
interactions/title match over IC fit.
Current state: the rerank prompt's EXPLICIT SENIORITY MATCHING covers
out-of-band titles (manager/founder/exec score 0.0-0.30 for IC queries) —
this part should already behave better; validate on remote searches.
Gaps still open:
- local pipeline has no LLM rerank at all (search-only by design), so local
  IC searches rely purely on `seniority_band` index filters — the exact
  failure mode reported
- no combined IC signal (current title + headline + seniority_band +
  role_track + founder-history) at retrieval/prefilter time; `seniority_band`
  is a single LLM-enriched field with noise
Fix direction:
- add an `ic_only` interpretation flag in extraction (set when the user says
  "ICs", "individual contributors", or rejects managers/founders), mapping to
  a stricter local filter: seniority_band ∈ requested bands AND role_track
  not in (executive, founder) AND current title/headline does not match
  founder/exec patterns
- for local searches with `ic_only`, consider an optional cheap LLM filter
  pass (the conservative filter primitive already exists) since index fields
  alone are noisy
Acceptance: rerun Amir-connected Bay Area PM IC search; no current
founders/heads-of/directors in results.

---

## Validation runs queued (after local parity changes)

1. Hebbia JD via forced-local routing — checks B1/B2 schema errors.
2. SF/Bay VC partners by interaction count (local) — checks C1 (Anamitra
   rank), C2 (Lenny count), C3 (Salil retrieval).
3. Bay Area PM ICs connected to Amir (local + remote) — checks D1.
4. Hebbia JD via search-profile (remote) — re-checks A1 stays fixed.

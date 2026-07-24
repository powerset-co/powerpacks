# Deep Context — synthetic profiles via deep research

> **Status: design history with shipped implementation notes.** Use the
> [deep-context pipeline](deep-context-pipeline.md) and
> [`deep-context/SKILL.md`](../skills/deep-context/SKILL.md) for the current
> end-to-end contract.

_Created 2026-06-24._

## Changelog
- 2026-07-24: Superseded detail — the section-4 "critical fix" below describes the
  keep-filter as it was then (LinkedIn key **and** a valid `rapidapi_response`, relaxed
  only for synthetic rows). `keep_people_csv_row` now admits any row with a LinkedIn key
  **or** an email **or** a phone; the RapidAPI requirement is gone entirely. The synthetic
  branch and its load-time `approved` gate are unchanged.
- 2026-07-09: Picked back up. Confirmed: research runs on Parallel.ai (deep_research_contacts
  ParallelClient) — no Perplexity/Sonar dependency is ported. Added integration note: the fan-in
  no-op cache now fingerprints override files (PR #172), so `overrides/synthetic-people.csv` must
  join FAN_IN_OVERRIDE_FILES in index_contacts_pipeline when the merge starts reading it.
- 2026-06-24: Initial plan.
- 2026-06-24: Locked decisions — (1) synthetic rows **gated behind the review `approved`
  column** (confident → `auto`, rest pending; nothing hits the index unapproved);
  (2) **one research pass per person**, branching to retarget (real LinkedIn found) or
  synthetic (none found). Decision section below updated from "open" to "resolved".

## Goal

Port aleph-mvp's **synthetic profile** generation into powerpacks `deep-context`, reusing
our existing Parallel.ai plumbing. The one improvement over aleph: we feed the **rich
per-person context deep-context already synthesizes** (facts JSONL + dossier: employers,
school, topics, shared_context, message-derived relationship) as the research input,
instead of aleph's thin `name + email + bio`.

A "synthetic profile" = an LLM-researched people-row for someone we have **no real LinkedIn
for** (contact-only / `linkedin_plausibly_absent` / user-detached with no good link). It
carries `enrichment_provider=synthetic`, a `synth-…` identifier, and flows into the merge +
index like a real profile — so these people stop being invisible in search.

## What already exists in powerpacks (reuse, do not rebuild)

| Capability | Where |
|---|---|
| Parallel.ai task-group client (create/add/poll/result) | `packs/ingestion/primitives/deep_research_contacts/deep_research_contacts.py` → `ParallelClient` |
| Research orchestration: queue build, cost gate ($25 / $0.05-per), eligibility | `packs/ingestion/primitives/deep_context/reconcile_deep_research.py` |
| `parallel_to_research_json()` → `01_research_parallel.json` (person/positions/education/social) | `deep_research_contacts.py` |
| Retarget proposal → enrich → people-row → merge auto-include | `reconcile_deep_research.py` + `apply_retargets.py` + `overrides/retarget-people.csv` |
| RapidAPI enrich + cache + people-row merge | `packs/ingestion/primitives/enrich/enrich_people.py` |
| Rich per-person facts | `.powerpacks/deep-context/facts/{person_id}.jsonl` (employers, school, topics, shared_context, identifiers, relationship_to_owner) |
| Composed dossier | `.powerpacks/deep-context/dossiers/{slug}.md` |

## What to copy from aleph-mvp

| Piece | aleph file | Adapt to |
|---|---|---|
| Person-research **instructions** (the thorough "professional investigator" prompt — real name → LinkedIn → work → edu → location → socials, with strict output rules) | `data_pipeline_v2/pipelines/synthetic/research_parallel.py` (lines ~62–162) | Replace/upgrade our Parallel instructions in `deep_research_contacts.py` |
| **Synthetic profile assembly** (research JSON → people-schema row: `enrichment_provider="synthetic"`, `public_identifier = synth-email-{hash}` / `synth-x-{handle}` / `synth-phone-{hash}`, `synthetic_metadata`, work_experiences/education) | `data_pipeline_v2/pipelines/synthetic/assemble_profile.py` (lines ~227–259) | **NEW** `packs/ingestion/primitives/deep_context/assemble_synthetic_profile.py` |
| Completeness/confidence gating + `gaps` metadata | `assemble_profile.py` | Same new primitive |

We do **not** port: Harmonic, EnrichLayer, Supabase, the 16-stage ingestion pipeline,
Postgres caches. Those are aleph-infra; powerpacks is local/file-based (per repo rules).

## Design

### 1. Richer research input (the improvement)
In `reconcile_deep_research.py` queue build, for each eligible person pull
`facts/{person_id}.jsonl` (and optionally the dossier) and pack the structured signal into
the research input: employers (with current/past), title, school, field, location, topics,
`relationship_to_owner`, `shared_context`, all identifiers/emails/phones. Either widen the
Parallel `input_schema` with structured fields or fold it into the existing `known_info`
text — start with `known_info` (smaller blast radius), structured later if needed.

### 2. Branch on the research result
Research runs once per person; the output branches:
- **Found a real LinkedIn** → existing **retarget** path (`apply_retargets.py` → enrich →
  `retarget-people.csv`). Unchanged.
- **No real LinkedIn, but a usable profile** → **synthetic** path → new
  `assemble_synthetic_profile.py` builds a `synth-…` people-row.

### 3. NEW `assemble_synthetic_profile.py`
For each eligible person whose `01_research_parallel.json` has no real LinkedIn but enough
profile (name + ≥1 position or location, above a completeness floor):
- Compute `public_identifier`: `synth-email-{sha1(primary_email)[:12]}` (or `-phone-` /
  `-x-`), `enrichment_provider="synthetic"`, `provider_entity_urn=f"synthetic:{pid}"`.
- Map research `positions`/`education` → people-schema `work_experiences`/`education` JSON.
- Carry the contact's `primary/all_emails`, `primary/all_phones`, `interaction_counts`,
  `last_interaction`, `source_channels` (look up by `person_id` in people.csv) — reuse
  `apply_retargets.CARRY_COLUMNS` + `people_schema` helpers.
- Stamp `synthetic_metadata` (confidence, sources_count, gaps, research_date, source_channel).
- Write to `overrides/synthetic-people.csv` (idempotent, keyed by `public_identifier`),
  with an `approved` column matching the review pattern (`auto` for high-confidence,
  pending otherwise — see open question on index injection).

### 4. Merge auto-includes synthetic rows
Mirror `retarget-people.csv`: the fan-in merge auto-reads `overrides/synthetic-people.csv`.
**Critical fix:** `keep_people_csv_row` currently requires a LinkedIn key **and** a valid
`rapidapi_response`. Synthetic rows have neither — relax the keep-filter to also keep rows
where `enrichment_provider=="synthetic"` (and `approved ∈ {auto,yes}`).

### 5. Eligibility — ties in pending task #47
Route into deep research → synthetic: `linkedin_plausibly_absent` verdicts, contact-only
people, and user detaches where research found no LinkedIn. Reuse existing
`eligible_subset` + cost gate; add a `--synthetic` mode (or just let the same run produce
both retargets and synthetics from one research pass — preferred).

### 6. Wiring
- `bin/deep-context`: add `assemble-synthetic` subcommand (or fold into
  `reconcile-deep-research` post-step).
- `common.py`: `SYNTHETIC_PEOPLE_CSV` path.
- `SKILL.md`: document the synthetic branch + changelog.
- Tests in `tests/test_deep_context.py`: assembly from a stubbed research JSON (no network),
  keep-filter keeps synthetic rows, merge ingests them, approval gating.

## Resolved decisions

1. **Index injection / trust — gated behind review.** Synthetic rows carry the same
   `approved` column as retargets: confident ones `auto` (flow into the index), the rest
   **pending in `synthetic-people.csv`** until approved in the review UI. The merge keep-rule
   only ingests `enrichment_provider=synthetic` rows where `approved ∈ {auto,yes}` — so search
   is never polluted by un-reviewed hallucinated profiles.
2. **One research pass per person, branching.** Each eligible person is researched **once**;
   the output becomes a retarget if a real LinkedIn was found, else a synthetic row. No
   double-paying Parallel for the same person.

## Verification
- `unittest tests.test_deep_context` — assembly, keep-filter, merge ingest, approval gate;
  no new baseline failures.
- Dry stub (no network): a canned `01_research_parallel.json` → valid synthetic people-row.
- One live person (cache/cost-gated, your go): a contact-only person → synthetic row →
  appears in `.powerpacks/network-import/merged/people.csv` → searchable.

## Notes / risks
- Privacy: synthetic profiles are built from message-derived facts — already inside
  `deep-context`'s scoped body-reading exception; metadata-only contract elsewhere holds.
- Keep-filter relaxation is the main integration risk — verify real (non-synthetic) rows
  still require a valid LinkedIn+rapidapi payload.
- Confirm the search index tolerates `synth-…` identifiers / empty `linkedin_url`.

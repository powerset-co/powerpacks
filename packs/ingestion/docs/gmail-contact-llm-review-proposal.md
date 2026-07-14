# Proposal: LLM verification + review UI for Gmail-resolved contacts

> **Status: proposed, not wired into `$import-gmail`.** Use the
> [Gmail import pipeline](gmail-import-pipeline.md) for shipped behavior. Parallel
> matches currently proceed to hydration without this judge or human-review gate.

_Created 2026-06-18. Changelog: 2026-06-18 initial draft._

## Problem

Gmail contact resolution (setup **Step 9**) turns an email into a LinkedIn
profile via Parallel, then we hydrate it via RapidAPI. But **nothing verifies
the resolved profile is actually the right person.** Same-name collisions slip
through: the A/B run showed cases like `Alan Chen`, `Albert Ding`, `Amit Vyas`
where Parallel confidently returned a profile that is plausibly a *different*
person with the same name and no real connection to the contact. These get
merged into `people.csv` and pollute the network.

We have rich evidence to catch this — the **email markers/context** from
`$enrich-email-markers` (employer, title, school, location, handles) — but it's
never checked against the hydrated profile, and there's no human review gate.

## Insight: this is exactly the messages-pack review pattern

The messages pack already solved the analogous problem (match an iMessage/
WhatsApp contact to a LinkedIn person, then let a human confirm). We should
**mirror that architecture**, not invent a new one:

| Messages pack | Gmail analog (proposed) |
|---|---|
| `llm_review_contacts` (LLM judge) | `verify_gmail_resolution` (LLM judge) |
| `build_research_review_csv` (bucket yes/maybe/no, `network_match_confidence`, `network_match_reason`) | same review-CSV shape, reused |
| `LocalMessagesReviewPage.tsx` (web review UI, yes/no tabs) | `LocalGmailReviewPage.tsx` (or reuse the same component) |
| `upload_research_review` (decisions → upload) | decisions → gate into `people.csv` |

## Where it slots (setup skill)

Today: **Step 8** sync → **Step 9** resolve→people.csv → **Step 11** merge.

Proposed — expand the resolution stage (Step 8.5 / 9) into a verified pipeline:

```
Step 8   Sync Gmail archives (msgvault)                       [unchanged]
Step 8.5 Enrich markers ($enrich-email-markers)               [NEW — context]
Step 9a  Resolve emails → LinkedIn candidates (Parallel)       [+ markers context]
Step 9b  Hydrate top candidate (RapidAPI / enrich_people)      [existing]
Step 9c  LLM VERIFY: email evidence vs hydrated profile        [NEW — judge]
Step 9d  Human REVIEW UI: confirm / reject / needs-review      [NEW — UI, opt-in]
Step 9e  Only CONFIRMED contacts → people.csv                  [gate]
Step 11  Merge                                                 [unchanged]
```

The markers from `$enrich-email-markers` feed both 9a (as Parallel `context`)
and 9c (as the evidence the judge checks against). This is why we slot the
markers step right after sync.

## The LLM judge (`verify_gmail_resolution`)

**Input per contact:**
- Email evidence: the markers + `linkedin_query` + a few recent subjects/snippets.
- The resolved + hydrated LinkedIn profile (name, headline, current/past
  employers, education, location) and Parallel's `candidates` + `match_confidence`.

**Output (deterministic structured, like `evaluate_profile_candidates`):**
- `verdict`: `confirmed` | `wrong_person` | `needs_review`
- `confidence`: 0..1
- `agreement`: which evidence matched (employer? school? location?) and which
  contradicted (e.g. profile is a student in India, contact emails about LA real
  estate) — the contradiction is the wrong-person signal.
- `reason`: one-line human-readable rationale (shown in the review UI).

**Decision rule:** strong evidence agreement → `confirmed`; clear contradiction
→ `wrong_person` (drop); thin/ambiguous → `needs_review` (goes to the UI). The
verdict/bucket is computed in code from the judge's structured agreement
fields, not free-texted — same pattern as the profile evaluator.

## Review CSV (reuse the messages shape)

One row per resolved contact, mirroring `build_research_review_csv`:
`email, name, linkedin_url, headline, current_company, source_channel,
bucket (yes|maybe|no), match_confidence, match_method, match_reason,
evidence_agreed, evidence_contradicted`. Buckets map: `confirmed→yes`,
`needs_review→maybe`, `wrong_person→no`.

## Review UI (mirror `LocalMessagesReviewPage`)

Same three-tab layout (yes / maybe / no). Each card shows: contact email +
recent-subject evidence on the left, the matched LinkedIn profile (photo,
headline, company, location, URL) on the right, the judge's reason, and
accept / reject toggles. Default selection = the judge's bucket; the human
overrides the `maybe`s and any wrong-looking `yes`. Reuse the existing review
component/route rather than building a new one where possible.

## Gate into people.csv

Only `confirmed` (post-review) contacts flow into the Gmail `people.csv` that
Step 11 merges. `wrong_person` are dropped; `needs_review` left out unless the
human accepts. This keeps same-name false positives out of the network.

## Cost

- Markers: ~$0.003/contact (already in `$enrich-email-markers`).
- Judge: 1 LLM call/contact, similar size → ~$0.003–0.005/contact. For ~500
  contacts ≈ **$1.50–2.50**. Concurrent + checkpointed like the markers step.
- No extra Parallel/RapidAPI beyond what Step 9 already spends.

## Open questions / decisions

1. **Auto-drop threshold:** do we auto-drop `wrong_person` without review, or
   route everything through the UI? (Lean: auto-drop high-confidence
   contradictions, review the rest.)
2. **Reuse vs. fork the review UI:** extend `LocalMessagesReviewPage` to a
   generic contact-review page, or fork a Gmail-specific one?
3. **Headless mode:** for non-interactive setup runs, do we skip the UI and just
   apply the judge's verdicts (confirmed + needs_review in, wrong_person out)?
4. **Where the gate lives:** new `gate_verified_contacts` primitive vs. a flag on
   the existing apply-resolutions step.

## Phasing

- **P1 (no UI):** `verify_gmail_resolution` judge + review CSV + auto-gate
  (confirmed in, wrong_person out). Immediately removes same-name false
  positives; reviewable as a CSV. Lowest lift, highest value.
- **P2:** web review UI (mirror messages) for the `needs_review` bucket.
- **P3:** wire both into setup Step 9 + `$enrich-email-markers`.

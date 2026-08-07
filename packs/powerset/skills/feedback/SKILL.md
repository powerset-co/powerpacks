---
name: feedback
description: File product feedback to Powerset from inside this session. The agent inspects the current session, composes one identifiers-only feedback row (query, commands, run artifacts, affected names/LinkedIn URLs — never message content), previews it with --dry-run, asks whether to include person identifiers, then posts it through the send_feedback primitive.
---

# Feedback

Created 2026-07-31. Changelog: 2026-07-31 — initial version; 2026-07-31 —
inline artifact attachments (`--artifact`); 2026-07-31 — `$deep-context`
surface notes.

Use for `$feedback`, "report this", "file/send feedback", "flag this result",
or any "that was wrong — report it" moment, usually right after another skill
produced a bad result (wrong person, bad LinkedIn, bad ranking, a crash).

One row goes to the existing `POST /v2/feedback` endpoint (the `user_feedback`
table) using the stored `$powerset login` bearer, via the send_feedback
primitive. The primitive validates and ships; this skill's job is composition —
mine THIS session for everything that helps an engineer repro the issue and
put it in `--comment` + `--metadata`.

## Privacy contract — identifiers only

- OK to include: the user's own words (their query, their typed complaint),
  names, LinkedIn slugs/URLs, emails/phones the report is about, local
  parent/dossier slugs, prod person UUIDs, run-dir paths, command lines, JSON
  payload statuses/counts/confidences, error text from our own tooling, and
  version/harness info.
- NEVER include: message bodies, email subjects/snippets, dossier prose,
  machine free-text reasons (`llm_worth_reason`, judge reasons), logbook text,
  or anything synthesized from message content. Decisions and confidences
  travel; their prose does not.
- Person identifiers (names / LinkedIn URLs / emails / phones) ride only with
  the user's explicit OK from the Step 3 preview question.

## Step 1 — inspect the session

Work from what already happened in this conversation; do not re-run failing
commands to harvest context. Collect whatever exists and skip what doesn't:

- What the user asked for, verbatim, and which skill/surface was active
  (`$search`, `$deep-context`, `$import-gmail`, ...).
- The misbehaving step: its exact command line, the JSON payload `status` /
  error it returned, its exit code.
- Run artifacts this session already wrote: the search run dir and its
  `decision.json` / task-state file, stage `manifest.json` paths, counts.
  Reference paths plus small excerpts (statuses, counts) — never dump whole
  artifacts into metadata.
- Affected people, as identifiers only: name, LinkedIn slug/URL, local slug,
  and the prod person UUID when a Powerset search result carried one.
- Environment: powerpacks version (`git describe --tags --always` in the
  repo, or the `.powerpacks-install.json` stamp next to installed skills),
  harness (codex / claude-code / pi), and the active set id
  (`POWERPACKS_DEFAULT_SET_ID`).

## Step 2 — compose the row

- `--comment`: 1–3 sentences — what was wrong and what was expected, leading
  with the user's own framing.
- `--feedback-type`: keep the default `data_inconsistency` when in doubt (the
  admin queue and LLM triage batch only pick up that type). Use `bad_search` /
  `bad_rerank` only when the complaint is clearly retrieval/ranking quality.
- `--category`: the field family (`linkedin`, `name`, `title`, `company`,
  `search`, ...). `--field-value`: the wrong value currently shown.
- `--person-id`: PROD person UUID only, and only if one is actually in hand;
  local slugs and ids go in `--metadata` instead.
- `--set-id`: `POWERPACKS_DEFAULT_SET_ID` when it is a real UUID; else omit.
- `--metadata`: one JSON object carrying the Step-1 haul. Skeleton — drop
  empty keys rather than sending blanks:

```json
{
  "source": "powerpacks-agent",
  "harness": "claude-code",
  "skill": "search",
  "user_query": "vp eng at fintech startups",
  "run_dir": ".powerpacks/search-runs/<run>/",
  "decision": {"surface": "people", "backend": "local", "depth": "fast"},
  "failing_command": "uv run --project . python packs/search/primitives/search_network_pipeline.py prepare ...",
  "error": {"status": "failed", "detail": "last ~500 chars of the error"},
  "counts": {"candidates": 120, "returned": 0},
  "people": [{"name": "Jordan Bravo", "linkedin_url": "https://linkedin.com/in/jordan-bravo", "parent_slug": "jordan-bravo"}],
  "powerpacks_version": "powerpacks-v1.6.0"
}
```

- `--artifact <path>` (repeatable): attach small run artifacts inline — each
  file is gzip+base64 packed under `metadata.artifacts` by the primitive.
  Attach only files that are identifiers/decisions by construction:
  `decision.json`, the task-state JSON and its `.events.jsonl`, stage
  `manifest.json` files. Never attach dossier files, logbook files, retrieval
  profile dumps, or anything carrying message-derived or profile prose — the
  same privacy contract applies to attachments.

Keep the whole body small (target under ~50 KB; the primitive refuses at
900 KB and the server rejects 1 MB). JSON artifacts compress ~5-10x, so a few
MB of raw artifact still fit — the primitive errors with per-artifact packed
sizes if the total goes over.

### `$deep-context` sessions

The review UI already files pane-level worth/retarget feedback automatically
(`review/feedback.py`); use `$feedback` for session-level issues the panes
cannot express (a bad merge run, a stuck stage, systematic wrong-LinkedIn
patterns). Deep-context specifics:

- Identifiers: parent slug + `person_ids`, candidate LinkedIn slugs/URLs with
  their confidences, machine/human worth DECISIONS and confidences.
- Artifacts: canonical SQLite identifiers and the stage `manifest.json` files
  (facts / parents / reconcile) — counts, statuses, and slugs by construction.
- Still never: dossier markdown, facts text, `llm_worth_reason` / judge reason
  prose — all synthesized from message bodies.

## Step 3 — preview and one consent question

Always dry-run first and show the user the exact body that would be sent:

```bash
uv run --project . python packs/powerset/primitives/send_feedback/send_feedback.py \
  --comment "..." --category linkedin --field-value "..." \
  --metadata '{"source":"powerpacks-agent", ...}' \
  --artifact .powerpacks/search-runs/<run>/decision.json --dry-run
```

When artifacts are attached, name them and their raw sizes in the preview
(the packed `data` blobs are noise — summarize, don't paste them).

Then ask exactly one question: **send with person identifiers (names /
LinkedIn URLs) to help repro, send redacted, or don't send?** The question
covers attachments too. On "redacted", drop the `people` array, drop
`--field-value`/`--person-id` when they identify a person, strip names from
the comment, drop any artifact whose contents name people, and send the rest.

## Step 4 — send

Re-run the same command without `--dry-run` and report the payload in one line:

- `submitted` → report the returned `feedback_id`. Done.
- `needs_auth` (exit 3) → the stored Powerset login is missing or expired;
  route to `$powerset setup`, then retry once.
- `failed` → surface `http_status` + error text; do not loop retries.

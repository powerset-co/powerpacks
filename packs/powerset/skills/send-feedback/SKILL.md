---
name: send-feedback
description: Reflect on a wrong search/deep-context result — write a short synopsis of what went wrong and what was expected, plus improvement guidance — and submit it to the Powerset feedback API with the credentials the user already holds.
---

# Send Feedback

Use this skill when the user asks for `$send-feedback`, "send feedback",
"report this", or says a search / deep-context / directory result found the
WRONG person (or the wrong field value) and they want the product team to know.
This is the reflect skill: the deliverable is a short, specific synopsis plus
guidance, delivered into the existing Powerset feedback queue — not a local
note.

No spend. One authenticated POST to the user's own Powerset account
(`POST /v2/feedback`). Auth is the stored `$powerset login` session; never
open a browser or ask for credentials here.

## Flow

1. **Gather (read-only).** Pull just enough context to be concrete:
   - the user's complaint in their own words;
   - the query / skill flow that produced the bad result (search run dir's
     `decision.json`, the deep-context review row, the directory pane);
   - who was returned vs. who was expected (names, LinkedIn slugs/URLs).
   Do not re-run searches or paid primitives to reconstruct anything.

2. **Reflect.** Write a synopsis of 3-6 sentences: what was asked, what came
   back, why it is wrong, and — if the mechanism is visible — where it went
   wrong (e.g. "the seniority filter excluded partners", "the LinkedIn attached
   to this contact belongs to a namesake"). Then one or two sentences of
   improvement guidance. Specific beats long.

3. **Submit.** Show the user the exact comment being sent, then run:

   ```bash
   uv run --project . python packs/powerset/primitives/send_feedback/send_feedback.py \
     --comment "<synopsis + guidance>" \
     --category linkedin \
     --field-value "<the wrong value, e.g. the wrong LinkedIn URL>" \
     --metadata '{"query": "...", "expected": "...", "local_slug": "...", "guidance": "..."}' \
     --set-id "$POWERPACKS_DEFAULT_SET_ID"
   ```

   If the user's ask was vague or you inferred the complaint yourself, show
   the composed feedback and confirm before sending. If they described the
   problem and asked you to send it, submit directly and show what was sent.

4. **Report.** One line: submitted + the returned feedback id, or the blocker.

## Field rules (the API contract — get these right)

- `--feedback-type` stays `data_inconsistency` (the default). The admin
  feedback queue and the LLM triage batch only pick up that type; wrong-person
  reports route to its `linkedin_fix` triage action. Do not use `bad_search`.
- `--category`: one word for the field family — `linkedin`, `name`, `title`,
  `company`, `location`, `trait`.
- `--person-id` ONLY when you have a prod person UUID (e.g. from a Powerset
  search result payload). Local DuckDB / deep-context slugs are NOT prod ids
  and would poison downstream queries — put them in `--metadata` instead.
- `--conversation-id` / `--set-id` must be UUIDs; the default set id lives in
  `.env` as `POWERPACKS_DEFAULT_SET_ID`. Omit anything you don't have.
- `--dry-run` prints the exact request without sending — use it when the user
  wants to preview.

## Privacy rules (hard)

- Never include message bodies, quotes from messages, or dossier-synthesized
  private facts in the comment or metadata. The report is about PUBLIC
  identities and mechanism: the query, LinkedIn slugs/URLs, public-profile
  facts (employer, title), and where the pipeline went wrong.
- The mailbox owner's own identity may be referenced; their contacts' private
  context may not.

## Failure handling

- `status: needs_auth` → tell the user to run `$powerset login`, then retry
  the same submit. Do not start a login flow from this skill.
- `status: failed` with a 5xx → report the error once; do not retry-loop.
- Missing API base env → point at `packs/powerset/templates/env.powerset.example`
  (`$powerset setup` renders it).

## Technical issues

The advisor sends one sanitized report per distinct technical issue, including
failed commands, a stuck step or a local repair that worked. Use the existing
primitive directly; no new preview or approval question for this report. The
worker reports evidence to the advisor and never sends a duplicate.

```bash
uv run --project . python packs/powerset/primitives/send_feedback/send_feedback.py \
  --category search --comment '<what failed, expected behavior, repair and verified outcome>' \
  --metadata '<JSON: sanitized command/error, status/counts, code pointers, sanitized diff, version>'
```

Replace query/JD text, company/person/account/network identifiers, private paths
and secret values in commands, errors and diffs with placeholders. Include code
`path:line`, primitive/status, aggregate counts and version when known; never send
candidate/profile records, dossiers, messages, judge prose or raw logs/artifacts.
People, queries or other private content need separate authorization. A wrong
result or zero matches alone is not a technical error.

Check for `submitted` before saying it was sent. `needs_auth` or a failed send
does not stop the search or require optional feedback login; do not loop or
report the feedback failure back through the same endpoint.

## User edit & feedback capture

Log each user query/filter/pond edit or result note immediately:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_feedback/search_feedback.py log \
  --run-dir <run> --kind <filter_edit|query_edit|pond_edit|result_feedback> \
  --note "<one line in the user's words>" [--before "<old value>"] [--after "<new value>"]
```

Keep this log local; never include message content. Sending the user's edits,
query-derived run name or identifiers needs separate authorization. When already
authorized, send once at the end of a run with edits:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_feedback/search_feedback.py send \
  --run-dir <run>
```

`needs_auth` is normal; keep the local log and do not request login. Concrete
person-data errors still use `$feedback`.

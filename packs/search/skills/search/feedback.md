## User edit & feedback capture

Log each user query/filter/pond edit or result note immediately:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_feedback/search_feedback.py log \
  --run-dir <run> --kind <filter_edit|query_edit|pond_edit|result_feedback> \
  --note "<one line in the user's words>" [--before "<old value>"] [--after "<new value>"]
```

Use identifiers only, never message content. At the end of a run with edits,
send once:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_feedback/search_feedback.py send \
  --run-dir <run>
```

`needs_auth` is normal; keep the local log and do not request login. Concrete
person-data errors still use `$feedback`.


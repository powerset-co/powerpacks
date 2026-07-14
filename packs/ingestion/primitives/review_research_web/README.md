# review_research_web

Local-only web reviewer for `.powerpacks/messages/research_review.csv`.

```bash
python packs/ingestion/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --open
```

This is the canonical browser reviewer for the local Messages workflow:

- review tabs based on the effective approved state
- card rows with phone signal, location, title/company, education, reason, and
  profile links pulled from `01_research_parallel.json` when available
- click a card to toggle approved/unapproved
- add optional `retarget_hint` notes (LinkedIn URL, company, title, location, etc.)
  for targeted reruns
- every click/hint edit immediately writes the CSV, so refresh/quit does not
  lose progress

The browser presents blank decisions using the review bucket as a visual
default:

- `exclude=no` means explicitly approved
- `exclude=yes` means explicitly unapproved
- blank appears approved for `confident|yes` and unapproved for
  `medium|review|maybe|no`

The local materializer is intentionally more permissive than that visual
default: a researched row with a LinkedIn URL and blank `exclude` can still be
retained. Reviewers must explicitly set `exclude=yes` for every unwanted row.

The `In Network` tab shows rows matched to people already imported from Gmail or
LinkedIn. They are separate from the LLM `yes | maybe | no` review tabs.

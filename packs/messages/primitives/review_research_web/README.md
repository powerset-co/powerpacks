# review_research_web

Local-only web reviewer for `.powerpacks/messages/research_review.csv`.

```bash
python packs/messages/primitives/review_research_web/review_research_web.py serve \
  --csv .powerpacks/messages/research_review.csv \
  --research-dir .powerpacks/messages/research \
  --open
```

This ports the `contact-exporter review --file research_review.csv` TUI into a
browser surface:

- review tabs based on the effective approved state
- card rows with phone signal, location, title/company, education, reason, and
  profile links pulled from `01_research_parallel.json` when available
- click a card to toggle approved/unapproved
- add optional `retarget_hint` notes (LinkedIn URL, company, title, location, etc.)
  for targeted reruns
- every click/hint edit immediately writes the CSV, so refresh/quit does not
  lose progress

Decision encoding matches approved upload semantics:

- `exclude=no` means explicitly approved
- `exclude=yes` means explicitly unapproved
- blank falls back to the review bucket default (`confident|yes` is approved;
  `medium|review|maybe` and `no` are unapproved)

The `In Network` tab shows rows matched to existing Powerset contacts. They are
separate from the LLM `yes | maybe | no` review tabs.

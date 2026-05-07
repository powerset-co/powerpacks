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

- yes / maybe / no tabs based on the effective upload decision
- card rows with phone signal, location, title/company, education, reason, and
  profile links pulled from `01_research_parallel.json` when available
- click a card to toggle enrich yes/no
- add optional `retarget_hint` notes (LinkedIn URL, company, title, location, etc.)
  for targeted reruns
- every click/hint edit immediately writes the CSV, so refresh/quit does not
  lose progress

Decision encoding matches upload semantics:

- `exclude=no` means explicit include / enrich yes
- `exclude=yes` means explicit exclude / enrich no
- blank falls back to bucket default (`confident|yes`, `medium|maybe`, `review|no`)

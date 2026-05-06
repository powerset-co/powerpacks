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

- yes / maybe / no tabs based on the research `bucket`
- card rows with phone signal, location, title/company, education, reason, and
  profile links pulled from `01_research_parallel.json` when available
- click a card to toggle enrich yes/no
- every click immediately writes the CSV `exclude` column, so refresh/quit does
  not lose progress

Decision encoding matches `contact-exporter` upload semantics:

- `exclude=no` means explicit include / enrich yes
- `exclude=yes` means explicit exclude / enrich no
- blank falls back to bucket default (`confident|yes` selected, others not)

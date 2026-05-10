# review_contacts_web

Local-only enrichment reviewer for `.powerpacks/messages/contacts.csv`.

```bash
python packs/messages/primitives/review_contacts_web/review_contacts_web.py serve \
  --contacts .powerpacks/messages/contacts.csv \
  --open
```

The server binds to `127.0.0.1`, edits the CSV in place, and never uploads
data. Click a contact card to toggle whether that row should be enriched. Each
click immediately writes the CSV `skip` column, so refresh/quit does not lose
progress.

The UI has tabs for:

- `Matched`: rows already linked to a Powerset person
- `Suggested`: local matcher suggestions that need confirmation
- `Unmatched`: unresolved contacts with searchable names, using the same
  `looks_like_real_name` rule ported from `aleph-mvp`
- `Low signal`: no-name or weak-name rows that should not become paid research
  work by default
- `Skipped`: rows marked out of scope

Default `YES` selection uses the same local drop rules as the research queue:
no-name rows, phone-number names, weak single-token names, and names whose
last-name tokens contain `hinge`, `raya`, `tinder`, or `bumble` default to
`NO` unless explicitly toggled.

Decision encoding:

- selected / `YES` writes `skip=false`
- unselected / `NO` writes `skip=true`

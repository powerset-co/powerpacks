# migrate_review_schema

One-off migration for older messages import handoff artifacts.

It rewrites `research_review.csv` into the final review schema:

- buckets become `yes | maybe | no`
- legacy `confident -> yes`
- legacy `medium | review -> maybe`
- existing Powerset matches from `contacts.csv` are added as `in_network`
- matched contacts that were not researched are added as `yes` rows

The orchestrator calls this after building the review CSV. Once older handoffs
are migrated and the normal builders emit the final schema directly, this module
can be removed with its orchestrator call.

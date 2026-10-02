# Local person lookup

If the query is a bare person identifier with no role/filter intent — a
name ("John Doe", "who is John Doe"), an email, a phone number, a Twitter/X
handle, or a LinkedIn profile URL — do **not** run the pipeline. Names and
identifiers are not indexed by any retrieval stage; run one direct lookup
instead:

```bash
uv run --project . python packs/search/primitives/local_duckdb_query/local_duckdb_query.py query \
  --sql "SELECT person_id, full_name, headline, current_title, current_company, city, linkedin_url FROM local_person_profiles WHERE full_name ILIKE '%john doe%'"
```

Match emails against `primary_email`/`all_emails`, phones against
`primary_phone`/`all_phones`, handles against
`twitter_handle`/`x_twitter_handle`, LinkedIn URLs against
`linkedin_url`/`public_identifier` (normalize to the slug). Show the
matches compactly; if several people match, list them all. If zero match,
say so and offer a normal search. Skip extraction, task state, retrieval,
hydration, and all LLM stages — this is a deterministic lookup, not a
search. If the query combines a person with anything else ("engineers who
worked with John Doe"), it is not this fast path; follow the Step 1 route.


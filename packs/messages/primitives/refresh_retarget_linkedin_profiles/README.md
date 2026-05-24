# refresh_retarget_linkedin_profiles

Refreshes retarget feedback that contains an exact LinkedIn `/in/` URL through
RapidAPI and writes the result into the retarget research artifact shape:

```text
.powerpacks/messages/research_retarget/<handle>__retarget_<hash>/01_research_parallel.json
```

This lets the existing retarget merge step update `research_review.csv` from the
new profile data without sending exact-URL rows back through broad Parallel
research.

The primitive only fetches rows whose `retarget_hint` contains a LinkedIn URL.
Non-URL feedback such as company/title/location hints stays on the existing
retarget research path.

`run` fetches profiles concurrently. Default worker count is 10 and can be
overridden with `--max-workers`.

The key comes from the repo-local Powerpacks runtime env:
`RAPIDAPI_LINKEDIN_KEY`. Provision it with `$powerset setup` or
`$powerset env pull`.

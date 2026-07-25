"""Enrich-stage primitives: shared LinkedIn profile enrichment.

`enrich_people` holds the three declared step nodes (`enrich_prepare_queue` ->
`enrich_linkedin_profiles` -> `enrich_merge_people`), the `EnrichPeople` store
that owns the artifact dir + spend gate + the one manifest, and the CLI;
`models`, `rapidapi_client`, `profile_cache`, and `profile_transforms` hold the
config/manifest/row/payload types, the RapidAPI client, the profile cache, and
the pure row transforms respectively.
"""

# Add Storage Sync

Install and maintain storage synchronization primitives.

## Intent

- upsert structured search and enrichment outputs
- support Supabase and TurboPuffer targets
- keep writes idempotent and environment-aware

## Expected Primitives

- `upsert_supabase`
- `upsert_turbopuffer`
- `read_sync_checkpoint`
- `write_sync_checkpoint`

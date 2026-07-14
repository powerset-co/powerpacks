# Search entry points

> **Compatibility page.** The old `search-surface.md` filename is retained for
> external links and layout checks. The canonical routing and execution contract
> is the [`$search` architecture](search-architecture.md).

`$search` is the single people-search door. It records the requested result
surface, candidate backend, and search depth before dispatching.

| Requested result | Current entry point |
| --- | --- |
| People | `$search <query-or-JD>` |
| Companies or company lookup | `$search-company <query>` |
| Relationships or aggregate questions | `$search-sql <query>` |
| Known contacts | `$search-contacts <query>` |

Older traces and integrations may use `/search-network <query>` or
`/search-company <query>`. Those strings are historical aliases, not the
canonical product vocabulary. The internal filename
`search_network_pipeline.py` is also intentionally retained for compatibility.

For people searches, `depth: fast` means the original one-pass
`$search-network` pipeline: prepare one query, confirm its preview, retrieve,
filter, and rank. It is standard search, not a different data source or a
lower-quality shortcut. `depth: deep` adds the recruiter contract, diversified
probes, evidence judge, deterministic gates, and anchor expansion described in
the architecture guide.

Slice planning, per-slice approvals, frontier assessment, and the V1 task
harness are retired and are not current execution stages.

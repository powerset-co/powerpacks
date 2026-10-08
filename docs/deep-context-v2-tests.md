# Deep context v2 unit tests

Run from the checkout:

```sh
scripts/test-deep-context-v2
```

The command runs only `tests/deep_context_v2/`. It does not run the general suite.
Python 3.12 and the checkout's development dependencies are required.

Tests use synthetic CSV files, small source databases and the production SQLite
store. Paid providers and process boundaries use test doubles. Tests check stage
results, persisted rows, counts, rollback, reruns and actual consumer round trips.
They do not read user archives or call live providers.

The runner blocks network connections and DNS. A hard deadline stops execution
after 290 seconds. Dependency installation occurs before this deadline. The
separate CI job has an eight-minute limit, including setup.

Coverage includes the entire v2 Python package, including unimported modules.
The command prints statement/branch coverage without a pass/fail threshold.
Coverage shows reachability; assertions establish the expected behavior.

The suite runs against main's product code. It does not introduce runtime type
guards, change ingestion modes or depend on the separate product-fix PR.
The planned typing pass and external-boundary validation stay separate.

## Later verification

- Compare pair-blocking sizes, generated pair counts, runtime and peak memory.
  Include dense common-name buckets and the bucket cap.
- Check large transitive parent merges with contradictory verdicts and distinct
  LinkedIn parents.
- Hand-count alias/group evidence against source history.
- Compare archive rebuild output and fingerprints after physical row IDs change.
- Interrupt paid stages around request completion and durable writes. Check
  retained answers and duplicate spend.
- Verify provider schemas, research quality and synthesis quality with bounded
  live requests and labeled histories.
- Verify desktop/mobile review, installation, indexing and upload separately.

These checks do not belong in the fast unit suite. Passing unit tests does not
establish live-provider accuracy or large-input performance.

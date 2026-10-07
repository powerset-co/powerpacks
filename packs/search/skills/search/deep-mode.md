# Search from a job or role brief

Use the existing result-driven pond loop. One pond searches one broad candidate
population through the ordinary pipeline. Review queries and compiled filters
yourself against the user's request/JD; this is a correctness check, not a routine
execution question. Preserve scope and corrections across every pond.

Default: run one round per pond, then continue if fewer than five unique people
across the search score at least 4 overall (including when nobody scores at least 3).
Tell the user you're expanding to find more people. Stop at five qualifying people,
no supported new pond, or the existing four-pond ceiling. Never widen explicit
geography or network to reach a target. User stop/limit requests win. Explicit
step-by-step mode pauses after each pond. Human labels remain separate from scores.

## Prepare

Write `decision.json` with `surface: people`, `depth: deep`, the chosen backend and
mode in `.powerpacks/deep-search/<slug>`. Supply exactly one of `--jd-file`/`--jd-url`:

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/deep_search_loop.py \
  --jd-url '<posting URL>' --run-dir <run> --set-id <selected-network-id>
```

For a pasted JD or role brief, save it verbatim as `<run>/jd.txt` and use
`--jd-file <run>/jd.txt`. For local search replace `--set-id` with
`--backend local --db <db>` throughout. Local retrieval may use hosted models;
explicit offline requests use the main skill's read-only SQL path instead.

URL intake writes `jd.txt` and source metadata. If thin or failed, try the official
board/API or available browser first; only then ask for pasted text. Ashby postings
are resolved through the public posting API. Do not replace a missing JD with a
snippet or inferred requirements.

The first call returns `awaiting_query_review` and writes `queries.json`. Read it;
keep the query broad and positive, without adding exclusions. Preserve explicit
constraints and all allowed locations as OR alternatives. Do not add workplace
restrictions. Correct omitted/narrowed geography against JD and source metadata.
State the query briefly, then repeat the same initialization with `--query-approved`.
Pause only if the user requested review or a material ambiguity needs their answer.

This initializes `results.json` and `manifest.json`; the same files hold each pond,
its payload, retrieval scope and result delta. No second orchestration system.

## Compile, review, run

The harness summary gives actual state and next action. Follow them rather than
loading the full results artifact. If correcting a pending query before compilation:

```bash
uv run --project . python packs/search/primitives/deep_search/search_harness.py set-query \
  --run-dir <run> --query '<corrected broad query>'
```

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/search_harness.py compile-pond \
  --run-dir <run>
```

Default retrieval is capped at 1,000. Preserve a smaller user-requested limit via
`--limit N`; run-pond reuses it. Read only the pending payload path returned by the
harness. Correct missing/narrowed location filters and extractor failures before
retrieval; an empty location result never authorizes a global search. Check:

- role keywords and stated seniority (not inferred from years of experience);
- geography and current/past/all temporal requirements;
- concise traits preserving JD breadth, preferences and OR alternatives;
- only explicitly requested exclusions.

Apply supported payload edits and mark that exact file reviewed:

```bash
uv run --project . python packs/search/primitives/deep_search/search_harness.py review-payload \
  --run-dir <run>
```

Use `--human-reviewed` only for actual human payload edits. Use `--rerank-exclusion`
only for a named specialty the user asked to penalize. Then execute:

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/search_harness.py run-pond \
  --run-dir <run>
```

Keep the configured judges consistent across ponds. Use authoritative overall
ratings, not rerank similarity or a capability screen's native score, for continuation.
No ranking-model changes or human-label overwrites are part of this workflow.

## Present and continue

After the first pond start the existing viewer in the background:

```bash
bin/deep-context review searches --run "$(basename <run>)"
```

It prints the local viewer URL. Show count, up to five useful people from the bounded
summary preview with evidence/profile links, and that URL. Never dump full candidate
records. Later ponds update the same viewer; tell the user to refresh. Missing fit
explanations stay unknown. Saved human scores/notes remain in the viewer.

For default automatic continuation:

```bash
uv run --env-file .env --project . python packs/search/primitives/deep_search/search_harness.py decide \
  --run-dir <run> --autonomous
```

If another pond is needed, say “I’m expanding the search to find more people,” state
its query, and repeat compile → review → run. Diagnose and propose the new pond
using existing results; the user need not invent queries. Do not repeat an exhausted
query or relax requirements to manufacture matches. Respect terminal state/reason.

For explicit step-by-step review, ask “Another round, or done?” after showing
results. Another round uses `decide --choice 2`, which can reopen a stopped run for
one more pond; done uses `decide --choice 3`. Only an explicit additional-round request
overrides the four-pond cap. Apply user corrections before the next execution.

At completion, present the useful results and viewer, with `shortlist.csv` as the
optional export. If the target was not met, state what was found and why the search
stopped. Preparation, an unrun pending query, or an internal acknowledgment is not a
completed search. For corrections/result comments follow [feedback.md](feedback.md).

## Hosted viewer

After each completed pond, upload unless the user requested offline/local-only results:

```bash
uv run --project . python packs/search/primitives/upload_search_results/upload_search_results.py \
  --run-dir <run> --env-file .env
```

`uploaded` returns a private URL. `needs_auth`: keep the local viewer quietly; don't
force login for optional hosting. Upload failure: keep local results and explain
hosting failed. Refresh the snapshot after labeling with the same command. Never
enable sharing automatically. Hosted reviewer labels stay separate from local labels.

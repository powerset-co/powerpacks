# Deep Context — technical spec

Created: 2026-08-06

Changelog:
- 2026-10-01: `enrich` runs the shared resumable chain from the agent CLI before
  LinkedIn review, with one progress manifest.
- 2026-10-01: synthesis advances to enrichment without worth review; settlement
  detaches empty lookup profiles and decides insufficient identity evidence No.
- 2026-09-28: `realize/export_people.py` writes the final roster into SQLite
  and exports people.csv from it; the directory.csv / retarget-people.csv
  round trip is gone.
- 2026-09-25: `migration/seed.py` carries a legacy install's decisions onto
  the cold parents by identifier after ensure-parents; the whole-graph
  legacy import is unrouted.
- 2026-09-25: the merge pair judge runs on JEV (p(yes) ≥ 0.5 merges); the
  OpenAI pair judge and its model/effort/retry knobs are gone.
- 2026-08-06: initial spec (post-SQLite-rewrite architecture; parents
  get-or-create contract landing on `parents-get-or-create`).
- 2026-08-06: contract 3 landed — `ensure_parents/assignment.py` owns stable
  get-or-create-or-absorb parent identity; `tools/parent_identity_proof.py`
  replays it against a copied real install.
- 2026-08-06: parent maintenance became incremental; accepted verdicts call
  one `Db.merge_parents` transaction and only changed dossiers are rendered.
- 2026-08-07: every stage moved under its owning package; public stage receipts
  moved to `manifests/`, and the dated pre-SQLite path moved to `migration/`.
- 2026-09-25: the standalone attached-link judge (`bin/deep-context reconcile`)
  is cut for now; attached links are judged by the review app's heal pass and
  research judge.
- 2026-09-25: the review app's heal pass is cut; attached links are no longer
  machine-judged, only research-proposed links are.

This is the engineering spec for the `deep_context` package: the data flow, the
contracts every stage obeys, and a per-file map. The product/UX guide is
[`docs/deep-context-pipeline.md`](../../docs/deep-context-pipeline.md); the
agent contract is the [`deep-context` skill](../../skills/deep-context/SKILL.md).

## Data flow

```mermaid
flowchart TD
  subgraph stores["Local message stores (read-only)"]
    chatdb[("chat.db (iMessage)")]
    waclidb[("wacli.db (WhatsApp)")]
    msgvaultdb[("msgvault.db (Gmail)")]
  end

  stores --> readers["collection/context_sources.py — configured per-channel readers\n(email scoring/dedup; chat recency caps)"]
  readers --> collect["collect_person_context\nper-parent crawl, caps + privacy policy"]
  collect --> bundles["raw/&lt;parent_id&gt;.json bundles\n(ephemeral, gitignored)"]

  bundles --> select["synthesis/selection\nfingerprint skip via SQLite artifacts"]
  sqlite[("deep-context.sqlite\nTHE record")] --> select
  select --> runner["synthesis/runner\nbatched OpenAI calls, adaptive stop"]
  runner --> factsf["facts/&lt;parent_id&gt;.jsonl\n(facts + worth verdict + fingerprint)"]
  factsf --> pfacts["db/projectors.project_parent_fact"]
  pfacts --> sqlite

  sqlite --> cluster["merge_candidates\nparent blocking + one JEV pair judge\nSQLite verdict cache"]
  cluster --> parents["parents — apply accepted merges\none transaction per absorbed family"]
  parents --> sqlite
  sqlite --> dossier["compose_dossier → dossiers/&lt;slug&gt;.md"]

  sqlite --> views["db/views — named SQL policy\n(worth queue, identity scope, progress)"]
  views --> web["review/ — Check LinkedIn; optional worth overrides"]
  web --> decide["db/store.decide_worth / decide_identity"]
  decide --> sqlite

  sqlite --> research["bin/deep-context enrich\nresearch → profiles → identity → relationships"]
  research --> receipt["manifest.json\nstatus + phase + errors"]
  research --> settle["local settlement — empty lookup profiles, worth No below 25 messages"]
  settle --> sqlite

  sqlite --> realize["realize/export_people\nfinal roster in SQLite → people.csv → index build"]
```

## Data repairs

`common/legacy.scrub_deep_context(db)` runs data repairs in order before
`EnsureParents` reads imports. Any future self-heal stage calls this same entry
before its own work. Each repair commits its changes and
`meta.data_migration_version` together; failure stops the sequence, and a rerun
skips completed repairs. Add new repairs after existing versions rather than
changing a version that users may already have completed. Ambiguous ownership
remains unchanged; a completed repair does not mean every contact was resolved.

Stages read the preceding stage's SQLite outputs. Paid stages save completed
results as they arrive and select remaining work on rerun; manifests report
progress but do not decide whether a task is complete.

## Contracts

1. **SQLite is the record.** `deep-context.sqlite` (schema in `db/schema.py`)
   holds canonical people, parents, identifiers, facts, identity candidates,
   decisions, research state, and artifact registry. Every file under
   `.powerpacks/deep-context/` is a cache, receipt, or re-derivable export.
   No stage reads a CSV/JSON baton to make a decision. The one live import
   boundary is `ensure_parents/imported_people.py`; old Deep Context artifacts
   cross only `migration/seed.py`.
2. **`manifest.json` is a write-only receipt** — counts, timing, error text
   for humans and agents to read after a run. Nothing derives control flow
   from it; pending-ness is always computed from named SQLite reads under
   `db/*_views.py`.
3. **Parent identity** (`ensure_parents/assignment.py`):
   `parent_id` is opaque and immutable once minted; it is never re-derived
   from membership. Assignment is get-or-create-or-absorb — a cluster whose
   members have 0 existing parents mints one (sha1 of the *founding* child
   set, stored forever); exactly 1 existing parent absorbs the cluster; >1
   merges into a deterministic survivor (newest human decision, otherwise
   most members then lexicographic) by repointing every `parent_id` column in one
   SQLite transaction. No alias tables. Clustering strategy (blocking, pair
   judging) is a separate concern and does not change ids. When parents merge,
   the newest human worth decision is the only worth row carried forward.
4. **Paid work is fingerprint-keyed, never name-keyed.** Synthesis caches on
   `input_evidence_fingerprint` (PINNED serialization: sha256 over the exact
   rendered prompts + system prompt — see `synthesis/prompting.py`); merge
   verdicts cache per unordered parent pair plus evidence fingerprint;
   research results key on their canonical dossier plus optional guidance;
   profile fetches cache per public identifier. A rename or
   re-cluster must never re-bill work whose evidence didn't change.
5. **Spend follows the skill's cost rules**, not state machines. Every paid stage
   has a free estimate path. The agent runs `enrich --dry-run`, then `enrich`
   without asking when the total is at most $100 and asks first above $100. The command sets
   research's budget to the plan's Parallel estimate; it has no approval flag.
6. **Human decisions are machine-untouchable.** Machine writers use
   `project_identity`/machine columns only; `decision_*` and `human_worth*`
   columns are written solely through `db/store.decide_identity` /
   `decide_worth`. Re-runs may overwrite any machine column, never a human one.
7. **Privacy.** Message bodies are read for synthesis (DMs + small iMessage
   groups by standing authorization; WhatsApp groups never). Raw bundles are
   ephemeral and gitignored; dossiers/facts store synthesized claims, not
   verbatim text. Anything committed (tests, fixtures, docs) uses synthetic
   identities only.

## Synthesis, in detail

```mermaid
flowchart LR
  import[imported people] --> ensure["ensure parents\nget-or-create, stable ids"]
  ensure --> collect["collect messages\nper parent"]
  collect --> changed{"new messages?\nbundle fingerprint"}
  changed -- no --> skip["skip — cached facts, $0"]
  changed -- yes --> llm["synthesize facts\n1–3 calls normally"]
  llm --> facts[("SQLite facts\n+ worth verdict")]
  facts --> pair{"same human,\nno shared identifier?"}
  pair -- judged or confirmed --> merge["merge parents\none transaction, id survives"]
```

**Unit of work:** one canonical parent and the union of all child identifiers.

1. **Collect** (`collection/collect_person_context.py` +
   `collection/context_sources.py` + `collection/email_context.py`): construct
   one source set per run. Gmail uses the shared
   msgvault store with signal scoring, near-duplicate removal, and
   breadth-before-depth thread windowing; iMessage/WhatsApp deliberately use
   recency caps only. Each parent receives the union of its children's
   identifiers and one bounded bundle. True totals are recorded so capping is
   honest. Output: `raw/<parent_id>.json` bundle (messages + identity).
2. **Select** (`synthesis/selection.py`): a parent is pending iff its cached
   facts artifact in SQLite has a different `input_evidence_fingerprint` or an
   older `SYNTHESIS_VERSION` (a hash of the prompt/schema/policy constants —
   bumping any of those re-opens everyone deliberately). Unchanged parents are
   skipped: no tokens spent.
3. **Run** (`synthesis/runner.py`): messages are chunked into batches of
   `--chunk-chars` (default 9,000 chars, capped at `--max-batches` = 20 per
   person). Every batch renders independently (no prior-profile context) and
   runs concurrently — one OpenAI Responses call each (default model
   `gpt-5.2`, strict JSON schema `synthesis/fact_schema.json`, system prompt
   carries the owner identity block); on the real install 86.5% of people have
   exactly one batch, so most parents cost 1 call. A person with more than one
   batch merges their independent results deterministically
   (`synthesis/facts.py:merge_fact_records`) — no extra LLM call. Retries:
   exponential backoff, 6 attempts on retryable errors. A person whose every
   batch errors or comes back empty is not persisted, so it retries on the
   next run instead of caching as done.
4. **Output:** appended extraction records in `facts/<parent_id>.jsonl` — merged facts (employers,
   title, school, topics, identifiers, relationship_category, `is_owner`),
   the `network_worth` verdict (yes/maybe/no + reason), `final_confidence`, usage
   tokens, stop reason, and the fingerprint.
   Each successful extraction records body-free message hashes, dates, channels,
   directions, and model/effort. Collection excludes consumed hashes before caps;
   tied dates, backfill, and capped overflow stay eligible. The first collection
   retains bounded cold sampling; later collections drain unseen old history too.
   Failed extraction advances nothing. Force re-extracts only the current bounded
   bundle while retaining every earlier extraction. Parent merges union records
   and coverage. JEV judges accumulated facts and interaction metadata.
   `db/projectors.project_parent_fact` projects exact uncapped unions into parent-owned `facts` + `artifacts` rows;
   downstream reads SQLite, not the JSONL.
5. **Estimate** (`--dry-run`): tiktoken-counted cost in USD, no spend — every
   person's batches all run (no adaptive stop), so there's one real number,
   not a floor/ceiling range.

## Per-file map

| File / package | Role | Reads | Writes |
|---|---|---|---|
| `ensure_parents/` | stage-1 imported-person projection and stable parent assignment | merged `people.csv`, SQLite | SQLite parents/people |
| `collection/` | source readiness, per-channel reads, collection planning, bundle assembly | msgvault, chat.db, wacli, SQLite | `raw/*.json`, receipt |
| `synthesis/` | synthesis selection/runner plus dossier composition and validation | SQLite artifacts, `raw/` | `facts/*.jsonl`, dossiers, receipts |
| `merge_candidates/` | same-person blocking/judging, accepted merge application, parent rendering | facts, SQLite | merge proposals, cached verdicts, `parents/*.md` |
| `enrich/` | research, profiles, identity and relationship judging, local settlement, synthetic profiles | SQLite queue, provider caches | research artifacts, SQLite identities and parent worth |
| `review/` | worth and identity web review, guided retarget, and restart | named SQLite views | human decisions via `db/store` |
| `realize/` | free, local: reviewed identities → final SQLite roster → people.csv | SQLite (roster, decisions, projected profiles) | SQLite roster, merged/people.csv |
| `migration/` | `seed.py`: identifier-keyed carry-over of legacy merges, raw bundles, facts, human decisions and research onto cold parents; `legacy.py`: the retired whole-graph import | legacy artifacts, SQLite | SQLite merges/bundles/facts/decisions/research, `raw/`, `facts/`, `reconcile/deep-research/` |
| `shared/` | common paths, readiness, owner, lookup, and dossier evidence | varies | owner cache where applicable |
| `manifests/` | one public receipt model per stage contract | — | serialized stage receipts |
| `db/` | THE record, typed reads, policy views, and transactional writes | — | `deep-context.sqlite` |
| `prompts/`, `tools/` | pinned prompt assets and migration proof tooling | varies | counts-only proof JSON |

## Enrichment, in detail

```mermaid
flowchart LR
  research[research] --> profiles[profiles]
  profiles --> judge[identity]
  judge --> relationships[relationships]
  relationships --> settle[settle]
  settle --> synthetic[synthetic]
```

`EnrichmentPipeline.run` executes the named sequence directly; the review server
wraps the same sequence in its thread. `bin/deep-context enrich --dry-run` uses
shared estimate code without writes. `enrich` writes the one enrichment receipt
before each step (`status: running`, `phase`), at completion (`completed`,
non-fatal errors), or on a raised exception (`failed`, phase and error).

Reruns start from research and reuse completed SQLite/provider artifacts. Running after a later
import processes pending work without separate checkpoints.

Batch research runs only on effective-Yes parents, gated in SQL before paid work.
Mapped links without a decision enter the identity judge. Human decisions are
kept. Batch-research and guided entry points share the evidence packet, prompt,
thresholds, async judge pool, and strict SQLite settlement. Cleared machine
decisions are recorded and hydrated at judge time; a settlement without the
exact judge-input fingerprint is rejected.

The local step after relationship review detaches machine-accepted lookup links
whose fetched profile is missing, errored, or has no experience and no education.
Own `linkedin_csv` connections and human link decisions are preserved.

Without human worth, a Yes/Maybe parent with no real LinkedIn profile and fewer
than `REVIEW_MESSAGE_BAR = 25` messages becomes machine No. A real profile is an
own connection, a LinkedIn a human kept, or an accepted, present profile with
experience or education.
Messages sum non-owner imported people's `interaction_counts`. The reason is
`not enough to know who this is: no LinkedIn profile and N messages`.

An own LinkedIn connection with no human worth decision is always worth Yes
(`own LinkedIn connection`), whatever the worth pass said.

The decision uses `parents.machine_worth` / `machine_worth_reason`, above facts
and below human worth, so JEV cannot overwrite it. Reruns clear its No when a
real profile arrives or messages reach 25. Worth-No parents leave LinkedIn and
research queues and receive no synthetic profile.

## Paid surfaces

| Surface | Provider | Cache key | Gate |
|---|---|---|---|
| Fact synthesis | OpenAI (`gpt-5.2`) | `input_evidence_fingerprint` + `SYNTHESIS_VERSION` | estimate → run |
| Merge pair judge | JEV (`jev-1.13.0`, TypeSafe) | judged pair + evidence, then the two names alone for a pair judged the same person; exact request under `jev/` | dry-run estimate before cluster |
| Deep research | Parallel.ai | selection fingerprint, per-parent result reuse | enrich plan → skill $100 rule → run |
| Profile hydration | RapidAPI | public identifier | cache-first everywhere |
| LinkedIn evidence judge | OpenAI | `judgment_fingerprint` | sticky verdicts, re-judge only on new evidence |

## Workflow state

`next_action` follows `synthesize` → `enrich` → `review_linkedin` → `realize`,
derived only from queue predicates. Worth review remains optional and Maybe
does not block enrichment. `enrich` is an agent action, so `review-status --wait`
returns immediately when it is pending. After `parents`, the agent previews and
runs enrichment, then opens Check LinkedIn. The fixed enrichment manifest
reports progress; SQLite and saved provider results determine pending work.

## Conventions

- Internal records are frozen dataclasses. Dictionaries exist only at true
  provider-input and JSON/HTML-output edges.
- Annotate non-obvious locals, including every optional local.
- Each stage package has one `models.py`; persisted SQLite row types remain in
  `db/models.py`.
- Names describe current behavior, never the mechanism's history.
- Missing values are `None`, never empty-string sentinels.
- Values above a row boundary use `bool`, not `0`/`1` integers.
- ISO timestamp strings use the `IsoTimestamp` alias. SQLite stores them as
  `TEXT` deliberately: their normalized lexicographic and chronological order
  are the same.

# Deep-setup pipeline: the post-import processing layer

> **Created:** 2026-07-16
>
> Changelog:
> - 2026-07-16: Initial guide — covers the import→process split (#205/#206) and
>   the context-informed network-worth triage + review filters (#207).

`$deep-setup` is where identity happens. The import skills (`$setup`,
`$import-gmail`, `$import-messages`) only **sync contacts down**: they are
free/local after OAuth, they never call an LLM, and they end by *offering*
processing — they never run it. Everything that thinks or spends lives here.

```txt
imports (contact sync only)                deep-setup (this guide)
  $setup ......... linkedin people.csv       collect  people ∪ candidates (free, reads bodies)
  $import-gmail .. people.csv + candidates   synthesize dossiers + network_worth (OpenAI, gated)
  $import-msgs ... people.csv + candidates   cluster/parents duplicate merge (OpenAI, gated)
        |                                    reconcile attached-LinkedIn self-heal (OpenAI, gated)
        v                                    reverse lookup ONCE (Parallel.ai, budget-gated)
  fan-in -> merged/people.csv                assemble synthetic profiles (free)
  + "enrich your contacts?" ask              review UI (hard stop: human decides)
                                             realize: fan-in -> Modal index -> validate
```

## Inputs

- `.powerpacks/network-import/merged/people.csv` — the fan-in output every
  import produces.
- `.powerpacks/network-import/import/{gmail,messages}/candidates.csv` — the
  imports' **research-candidate pools**: contacts worth researching that could
  not be resolved for free (gmail: post-directory unresolved + cached-negative
  queues; messages: unmatched contacts passing the deterministic pre-LLM floor).
  Candidates are NOT people rows — they become searchable only through this
  pipeline's approved outputs.
- The local message stores (msgvault.db, chat.db, wacli.db) for body reading —
  same scoped body-reading exception as `$deep-context`.

## Network-worth triage (yes / maybe / no)

The successor to the old messages-flow buckets, now judged from **actual
message context**. During synthesis the model outputs, per profiled contact,
`network_worth {decision: yes|maybe|no, reason}`:

- `yes` — high-signal (founders/investors/operators/builders, elite tracks,
  credible influence) OR a genuine two-way relationship with real depth.
- `maybe` — some real signal but thin/one-sided/identity-uncertain.
- `no` — transactional/service contacts, automated senders, group-only
  acquaintances, cold outreach never engaged with.

Resolution order: **user mark > LLM judgment > default `maybe`**. The user
mark lives in a sticky, user-owned `network_worth` column in
`overrides/review.csv` (the machine never writes it); the machine's judgment
is mirrored into machine-owned `llm_worth`/`llm_worth_reason` columns by
reconcile/re-review, with the spam screen folded in as just another way the
LLM says `no`.

**Effective `no` = Rejected = out of the network.** One concept, whoever said
it: the row moves to the Rejected tab, a candidate is excluded from the paid
reverse lookup (`candidates_skipped_worth_no`) and synthetic minting
(`skipped_worth_no`), and an already-imported person is dropped from
`merged/people.csv` at the next fan-in merge (`worth_dropped` in the merge
manifest). A user **Yes** (or keep-ish approve) rescues anyone from a machine
`no`; a user **No** drops regardless. Nothing is destructive — flipping the
mark restores the person at the next merge.

## Review surface

`bin/deep-context review` — one row per person. Deep-setup additions:

- Dossier-bearing **candidates appear pre-research** with a
  "candidate — no LinkedIn" badge.
- **Yes / Maybe / No** buttons (+ ↺ reset-to-LLM) on **every row type** —
  candidates, synthetic rows, and plain verdict rows alike; the LLM's decision
  + reason shows as secondary text (a spam-sourced `no` shows the spam
  reason). Marking No moves the row to the **Rejected** tab; Yes/Keep rescues
  it back out.
- Filter chips: **worth** (yes/maybe/no) and **source** (gmail / imessage /
  whatsapp — shown only when more than one source is imported).

## Spend map (every paid stage previews first; nothing chains)

| Stage | Provider | Gate |
| --- | --- | --- |
| collect | none (local body read) | group-body double opt-in only |
| synthesize | OpenAI | `dry` floor/ceiling → explicit OK |
| cluster | OpenAI | `cluster --dry-run` → explicit OK |
| reconcile | OpenAI | `reconcile --dry-run` → explicit OK |
| reverse lookup | Parallel.ai | estimate → `--approve --budget <estimate>` (default 0) |
| owner / apply-retargets | RapidAPI on cache miss | disclose + explicit OK |
| review | none | **hard stop — wait for the human** |
| realize index | Modal | disclose upload + explicit OK |

## Outputs

Same artifact layout as `$deep-context` (`.powerpacks/deep-context/` +
`.powerpacks/network-import/overrides/`). Candidates become people rows only
as approved **retargets** (real LinkedIn found) or approved/auto **synthetic**
rows; the fan-in merge auto-ingests both, and the Modal rebuild makes them
searchable.

## Does it run automatically after imports?

No — deliberately. Each import skill's final step runs the source-status
primitive, suggests any missing source, and **asks** in product words grounded
in what it found — e.g. "I see iMessage and WhatsApp are imported — do you
want to enrich your contacts?" (the skill name never appears in the ask).
A yes routes into `$deep-setup` in the same session; a no leaves the
candidates staged (nothing is lost — the pools are durable). It can never run
silently because every paid stage requires a fresh, explicit confirmation and
the review step is a hard stop.

## Relationship to `$deep-context`

Same primitives package (`packs/ingestion/primitives/deep_context/`), two
skills: `$deep-setup` is the orchestrated post-import flow over people ∪
candidates; `$deep-context` remains the ad-hoc surface — dossier lookups
("who is <name/phone>?"), re-reviews, and the review UI on existing artifacts.

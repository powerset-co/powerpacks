# Block 03 Synthesize: report

Created: 2026-10-06. Change log: 2026-10-06 first pass.

## Files
- `packs/ingestion/primitives/deep_context_v2/synthesize/synthesize.py`: 199 lines (`Synthesize(Node)`, `main`)
- `packs/ingestion/primitives/deep_context_v2/synthesize/__init__.py`: 1 line

## From v1 (imported)
- `synthesis/prompting.py`: SYSTEM_PROMPT, OWNER_PROMPT_SUFFIX, owner_identity_block, SYNTHESIS_VERSION, FACT_SCHEMA, batches, render_batch. It pulls in `deep_context/db/models.py` (dataclasses only) itself; block 03 does not import it directly. owner_identity_block gets v2's OwnerProfile (it reads only `.name` and `.emails`).
- `synthesis/facts.py`: collapse_fact_records. `synthesis/models.py`: SynthesizedFacts, FactRecord.
- `collection/models.py`: CollectionBundle. `shared/openai_responses.py`: OpenAIResponsesCaller, OpenAIResponsesConfig, estimate_cost_usd. `packs/indexing/lib/llm_config.py`: DEFAULT_SYNTHESIS_MODEL.

## Copied (the source module imports the v1 store)
- `selection.py:151-153`: assembling the system prompt
- `runner.py:142-152,177`: running all batches at once, then using the one answer or collapsing several
- `runner.py:213-247,266-272`: the tiktoken estimate (o200k_base, 750 output tokens assumed per call)

## Dropped
- `synthesize_person_context.py:32,159`: scrub_retired_message_linkedin_facts. Spec action A deletes legacy.py.
- `synthesize_person_context.py:130-139,176`: normalize_parent_cache. Spec 03: no parent facts files; a parent's facts are a read.
- `synthesize_person_context.py:165-174`: nobody_finished, SynthesisRun, record_synthesis_run. No run-status plumbing: the CLI either estimates or runs.
- `synthesize_person_context.py:168`, `runner.py:383-521`: the JEV worth tagging (tag_saved_facts, _tagging_*, person_headlines, _write_worth). Worth moves to stage 06.
- `synthesize_person_context.py:175`: apply_linkedin_name_matches. Spec 04 and enrich own the name match.
- `synthesize_person_context.py:179-189`: parent_fact_counts, WorthSyncResult. These count parents and worth, neither of which is here.
- `synthesize_person_context.py:222-236`: --raw-dir, --out-dir, --db, --model, --reasoning-effort, --chunk-chars, --max-batches, --concurrency, --timeout, --max-retries and --force. Spec pins model, batch size and cap; the rule is no --force.
- `runner.py:86-90`: coerce_relationship_category. The strict schema enum already limits the value, so the coercion never fires.
- `runner.py:93-125,330-359`: per-call and per-person except blocks, SynthesisFailure, and the tally. A provider error stops the run, and the next run resumes from the facts rows.
- `runner.py:187-209`: SynthesisRecord envelope (messages, usage, stop_reason, system_prompt_hash). facts_json holds only the 16 fields.
- `runner.py:218-233,249-263`: JEV cost in the estimate (worth is not here).
- `runner.py:293-307`: facts/<id>.jsonl, .bkup copy, project_person_fact. SQLite is the record.
- `selection.py:26-61`: effective_parent_bundles and effective_person_bundles. One bundles row per candidate; no union over parents and no singleton mapping.
- `selection.py:64-74,107-123`: pending_messages, seed fingerprint, history-coverage partial reruns. These carry results forward.
- `selection.py:127-141`: the version check against the v1 artifact plus input_evidence_fingerprint (`prompting.py:165-211`). Replaced by `facts.input_fingerprint` = sha256 over bundle content_fingerprint, SYNTHESIS_VERSION, sha256(system prompt incl. owner), model, effort, chunk_chars, max_batches.
- `selection.py:100,104-105`: owner skip. The work list is every bundle, per the brief. 0 of the 591 bundles belong to an is_owner candidate (block 02 does not collect for them).
- `prompting.py:149-160`: the prior-profile branch of render_batch. prior is always None.

## Run
`uv run --no-sync --project . python -m packs.ingestion.primitives.deep_context_v2.synthesize.synthesize --data-root /Users/arthur/workspace/powerpacks/.powerpacks --dry-run` (OPENAI_API_KEY unset)
- bundles 591, fresh 0, work 591, work_without_messages 0, calls 982, input_tokens 3,990,284, assumed output tokens 736,500, gpt-6-luna medium, synthesis_version b2374192ab73, **estimated_cost_usd 0.383639**. This is flex pricing (x0.5, the `POWERPACKS_OPENAI_SERVICE_TIER` default; the dry run does not load `.env`). The standard tier is about $0.77.
- Batches per candidate: 506 have 1 (85.6%; v1 recorded 86.5%), 4 hit the 20-batch cap.
- Dry run writes no facts and no manifest. facts = 0 afterwards; v2-manifests holds only collect.json and import_load.json.
- Real data: collect manifest 595 candidates and 591 bundles (4 no_messages); import manifest 602 candidates, 7 owner_flagged.
- Paid path with a fake OpenAI client in a scratch root (5 v1 raw bundles, no network): 16 calls, 5 rows, each with exactly the 16 keys and gpt-6-luna/medium; a rerun did 0 calls with work 0; the manifest said completed.

## Prompt fidelity
- **System prompt is NOT byte-equal to v1.** I parsed owner.json the way v1 does (`db/queries.py:113-140`, which keeps ints) and built v1's prompt (`selection.py:151-153` + v1 `shared/common.owner_background_block`). Its hash is `72b9333d5aa7…`, exactly the system_prompt_hash on all 502 latest v1 facts records. v2's hash is `c348c1330ae0…`. Only lines 20 and 21 of 28 differ: the two current `- Work:` lines. v1 renders `[<start>-present]`, v2 renders `[<start>-0]`.
- Cause: owner.json stores `end: 0` for a current job. v1 keeps the int 0, which is falsy, so it prints `-present`. v2 `db/owner.py:_text(0)` returns `"0"`, which is truthy, so `_span` prints `-0`. That breaks the "byte-identical to v1" claim in owner.py's docstring.
- User prompts: both sides call v1 `prompting.render_batch`/`batches` with 9000/20, so they are identical by construction.
- Bundle fingerprint (block 02, `collect/collect.py:121`): sha256 of the indent-2 sorted-key payload, with no timestamp. An unchanged bundle therefore keeps its fingerprint and a rerun costs $0.

## Spec or DDL I could not satisfy
- The brief and the spec block contract say `reads=("bundles","owner","facts")`. The DDL's `facts.candidate_id REFERENCES candidates(candidate_id)` with `PRAGMA foreign_keys = ON` makes every facts insert read `candidates.candidate_id`. The Node authorizer denied it: `sqlite3.DatabaseError: access to candidates.candidate_id is prohibited`. Fix applied: `reads` includes `"candidates"`, with a comment. Either the contract table lists candidates for 03, or the authorizer allows reads that come only from foreign-key checks.
- Spec 03 says "prompt unchanged from v1". Not met until `db/owner.py` (a foundation file, which I did not touch) renders a 0 end date as v1 does. One-line fix: treat 0 as blank in `_text`, or keep v1's int handling. As it stands every v2 facts row is billed on a prompt that tells the model the owner's current jobs ended in year 0.
- The same FK denial will hit any writer of an FK table whose contract `reads` omit `candidates` (for example 07 writing `candidate_linkedins`).
- Not run: the paid path on real data (it costs money; the orchestrator runs it).

## Fixed after Astra review (2026-10-06, orchestrator)
- db/owner.py `_text(0)` → "" so a current job prints "present"; the v2 system prompt now hashes 72b9333d5aa7, equal to every v1 facts row.
- node.py allows reads of foreign-key parents of declared writes; `candidates` is out of `reads`.
- Effort resolved once (`OpenAIResponsesConfig.resolve`) and used in the request, the fingerprint and the row.
- Fingerprint is over the rendered batch prompts + system prompt + model + effort, not the whole bundle.
- A failed candidate writes nothing and the others finish; the run then fails with the count.

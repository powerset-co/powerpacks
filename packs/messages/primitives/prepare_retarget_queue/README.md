# prepare_retarget_queue

Build a targeted re-research queue from `research_review.csv` feedback.

Use this when the reviewer knows the right identity clue for a row, e.g. a
LinkedIn URL, company name, title, location, or exact person note. The primitive
only queues rows with non-empty `retarget_hint` and skips `(handle, hint_hash)`
combinations already recorded in `.powerpacks/messages/retarget_attempts.json`.
If the feedback text changes, it becomes a new attempt.

```bash
python packs/messages/primitives/prepare_retarget_queue/prepare_retarget_queue.py prepare \
  --review-csv .powerpacks/messages/research_review.csv \
  --base-queue .powerpacks/messages/research_queue.csv \
  --output .powerpacks/messages/retarget_queue.csv \
  --retarget-output-dir .powerpacks/messages/research_retarget
```

Then estimate/run Parallel after explicit approval:

```bash
python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py estimate \
  --input .powerpacks/messages/retarget_queue.csv \
  --output-dir .powerpacks/messages/research_retarget \
  --processor core2x

python packs/messages/primitives/deep_research_contacts/deep_research_contacts.py run \
  --input .powerpacks/messages/retarget_queue.csv \
  --output-dir .powerpacks/messages/research_retarget \
  --processor core2x
```

If Parallel fails, is unavailable, or returns no plausible person for a feedback
row, the agent should automatically run a small Codex/web-search fallback for
only those feedback rows. Keep that fallback separate from the main research dir
and do not upload until the user reviews the result.

After a successful run, mark completed attempts:

```bash
python packs/messages/primitives/prepare_retarget_queue/prepare_retarget_queue.py mark-completed \
  --retarget-output-dir .powerpacks/messages/research_retarget
```

The generated queue uses unique retarget handles like
`<original_handle>__retarget_<hint_hash>` so it does not collide with the first
research pass or get skipped as already done.

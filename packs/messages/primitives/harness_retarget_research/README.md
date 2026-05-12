# harness_retarget_research

Low-latency retarget research for small feedback batches.

Use this when Parallel.ai is slow for a few corrected contacts. It reads
`retarget_queue.csv`, writes per-row prompts, and can invoke a local CLI harness
(Codex or Claude) to do web research. It writes the same
`01_research_parallel.json` profile artifact shape consumed by:

```bash
python packs/messages/primitives/prepare_retarget_queue/prepare_retarget_queue.py mark-completed
```

No message bodies are read. Prompts only include contact/research metadata and
user feedback hints.

## Prepare prompts only

```bash
uv run --project . python packs/messages/primitives/harness_retarget_research/harness_retarget_research.py prepare \
  --input .powerpacks/messages/retarget_queue.csv \
  --output-dir .powerpacks/messages/research_retarget
```

## Run with auto-detected harness

```bash
uv run --project . python packs/messages/primitives/harness_retarget_research/harness_retarget_research.py run \
  --input .powerpacks/messages/retarget_queue.csv \
  --output-dir .powerpacks/messages/research_retarget
```

Auto chooses `codex` if available, then `claude`, otherwise falls back to prompt
preparation.

## Custom command

```bash
uv run --project . python packs/messages/primitives/harness_retarget_research/harness_retarget_research.py run \
  --command-template 'claude -p {prompt_instruction}'
```

Placeholders:

- `{prompt_path}`
- `{output_path}`
- `{prompt_instruction}`

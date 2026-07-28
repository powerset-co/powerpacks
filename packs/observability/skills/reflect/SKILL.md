---
name: reflect
description: Create and report a privacy-safe, reporting-only review of a recent Powerpacks workflow. Use for $reflect, "report what happened", "send anonymized feedback", or retrospective feedback after $setup, $import-gmail, $import-messages, or $deep-context. The skill records controlled observations, model/provider/effort/role metadata, and bucketed manifest timings/counts; it never sends transcripts, raw manifests, PII, free text, code, patches, or implementation proposals.
---

# Reflect

Produce one anonymized workflow report. Detection and reporting are the entire
job: do not design, propose, generate, or apply an implementation.

## Hard boundary

- Never read a session transcript or message body for this skill.
- Never upload raw manifests, errors, paths, commands, prompts, hashes,
  timestamps, account identifiers, or person/contact fields.
- Never edit source, skills, prompts, `AGENTS.md`, config, or telemetry settings.
- Never generate code, a patch, test implementation, commit, PR, or remediation.
- Never create a GitHub issue without showing the sanitized preview and receiving
  explicit confirmation.
- Never fall back to a public GitHub issue when a Powerset upload fails.

The primitive enforces a closed export schema. Do not supplement its payload
with prose from the task or transcript.

Timing is stage-owned. The reporter reads the fixed manifest's explicit
top-level `timing.duration_seconds` (with exact top-level legacy duration fields
only); it never searches nested step/assembly payloads for a convenient elapsed
value. Named child timings remain useful local diagnostics but cannot be
misreported as the parent stage duration.
Each stage projection also reads only exact top-level `model` and
`reasoning_effort` / `effort` fields, normalizes them through the closed
allowlist, and otherwise reports `unknown`.

## Run

1. Select exactly one supported workflow from the active task:
   `setup`, `import-gmail`, `import-messages`, or `deep-context`.
   If the active task does not establish one, ask which of the four to reflect
   on. Fixed manifests show current artifact state, not a provable session
   boundary; say so if that distinction matters.

2. Supply only metadata known from the active harness:

   - harness: `codex`, `claude-code`, `nanoclaw`, `pi`, `other`, or `unknown`
   - provider: `openai`, `anthropic`, `google`, `other`, or `unknown`
   - model: the model identifier, only when known
   - effort: `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, or
     `unknown`
   - role: `primary`, `reviewer`, `subagent`, or `unknown`
   - intervention: `none`, `expected_approval`, `oauth`, `os_permission`,
     `user_correction`, `retry`, or `manual_recovery`
   - `--fallback` only when a model/tool fallback or reroute occurred

   Do not infer absent values from transcript text. Resolve the canonical,
   state-owning Powerpacks repo exactly like the four source workflows:

   ```bash
   resolve_powerpacks_root() {
     for candidate in "${POWERPACKS_REPO_ROOT:-}" "$PWD" "$HOME/powerpacks" "$HOME/workspace/powerpacks"; do
       [[ -n "$candidate" ]] || continue
       [[ "$candidate" != *"/.codex/"* ]] || continue
       if [[ -x "$candidate/bin/reflect" && -d "$candidate/packs" ]]; then
         printf '%s\n' "$candidate"; return 0
       fi
     done
     return 1
   }
   REPO="$(resolve_powerpacks_root)" || {
     echo "Could not find the canonical Powerpacks repo; report not sent." >&2
     exit 1
   }
   cd "$REPO"
   ```

   Never use an installed skill bundle as the state root; it may not contain
   the workflow's live `.powerpacks` artifacts. Then run:

   ```bash
   bin/reflect \
     --workflow <workflow> \
     --harness <harness> \
     --provider <provider> \
     --model <model-or-unknown> \
     --effort <effort> \
     --role <role> \
     --intervention <category>
   ```

   If the user requested local-only reflection, add `--local`.

3. Route the result by its JSON `status`:

   - `sent`: say that the anonymized report was sent to Powerset. No extra
     confirmation is needed; invoking `$reflect` authorized this upload.
   - `local`: say where `.powerpacks/reflect/report.json` was written and that
     nothing was sent.
   - `upload_failed`: say the report remains local and was not sent. Do not
     offer or create a public issue as an automatic fallback.
   - `no_artifacts`: say no supported workflow artifacts were found and nothing
     was sent. Do not characterize this as a friction-free run.
   - `github_issue_offer`: show the generated issue title and body from
     `delivery.preview`, then ask whether the user wants to publish that exact
     sanitized report to `powerset-co/powerpacks`.

4. Only after explicit confirmation of a `github_issue_offer`:

   - run `gh api user --jq .login` and tell the user which GitHub login is active;
   - create one issue in `powerset-co/powerpacks` using exactly the title/body
     from `.powerpacks/reflect/report.json`;
   - do not add interpretation, implementation ideas, code, or transcript text.

## Output contract

The primitive overwrites only:

- `.powerpacks/reflect/report.json` — sanitized report plus delivery state
- `.powerpacks/reflect/export.json` — exact sanitized outbound payload
- `.powerpacks/reflect/manifest.json` — fixed completion status

There are no run IDs, ledgers, transcript copies, queues, background uploads, or
run-scoped directories.

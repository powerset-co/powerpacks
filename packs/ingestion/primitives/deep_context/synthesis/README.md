# deep_context/synthesis — `deep_synthesize`, `deep_compose`

Two declared nodes share this package (one directory, two modules and two
manifests): `deep_synthesize` (`synthesize_person_context.py`) builds per-parent
facts with paid, fingerprint-keyed OpenAI Responses calls and then labels them
with JEV; `deep_compose` (`compose_dossier.py`) renders the dossier artifacts
and projects the downstream payload from SQLite.

Pipeline-wide context: [deep-context-pipeline.md](../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../skills/deep-context/SKILL.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `deep_synthesize` | `deep-context/raw/{parent_id}.json` (optional), `deep-context/owner.json` (optional) | `deep-context/facts/{parent_id}.jsonl` (optional), `deep-context/jev/{request_sha256}.json` (optional) | `deep-context/facts/manifest.json` (`SynthesizePersonContextManifest`) |
| `deep_compose` | — (reads SQLite) | `deep-context/dossiers/{slug}.md` (optional) | `deep-context/dossiers/manifest.json` (`ComposeDossierManifest`) |

## Manifest / status

- `SynthesizePersonContextManifest`: status `completed`; carries `people`,
  `batches_run`, `stop_reasons`, `errors`, `tokens`, and the `jev` usage block.
- `ComposeDossierManifest`: status `completed` even when parents were skipped —
  skipped parents are a visible count (`skipped`, `skip_reasons`), not a
  distinct status.

Both inherit the template `not_ready` / `failed`.

## Control

- `deep_synthesize`: paid OpenAI. No in-code `needs_approval` gate —
  `--dry-run` (`estimate()`) is the only free path; the spend convention is a
  dry-run first.
- `deep_compose`: free.

## Invariant

Synthesis is fingerprint-keyed (prompt + system prompt), so a rename or
re-cluster never re-bills unchanged evidence; a person whose every batch errors
is not persisted, so it retries next run. `deep_compose` raises without an owner
profile ("run build-owner first"); only parent-owned facts are dossier sources.

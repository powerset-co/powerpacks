# Ingestion skills

Skills are harness-facing execution contracts. They choose deterministic
primitives, enforce user/provider gates, and write fixed `.powerpacks` artifacts;
the runtime behavior lives in the referenced scripts.

Start with the [ingestion documentation index](../docs/README.md) for product
architecture and current-versus-historical document status.

## Primary product workflows

| User command | Skill | Purpose | Product guide |
| --- | --- | --- | --- |
| `$setup` | [`setup`](setup/SKILL.md) | LinkedIn-only setup, fan-in, Modal build, and local validation. | [LinkedIn and Modal indexing](../../indexing/docs/linkedin-modal-pipeline.md) |
| `$import-gmail` | [`import-gmail`](import-gmail/SKILL.md) | Gmail/msgvault sync, metadata import, identity resolution, fan-in, and index rebuild. | [Gmail import pipeline](../docs/gmail-import-pipeline.md) |
| `$deep-context` | [`deep-context`](deep-context/SKILL.md) | Message-body dossiers, duplicate grouping, LinkedIn self-heal, and reviewed overrides. | [Deep-context pipeline](../docs/deep-context-pipeline.md) |
| `$import-twitter` | [`import-twitter`](import-twitter/SKILL.md) | Twitter/X network import and LinkedIn validation. | Skill is the current guide. |
| `$discover-contacts` | [`discover-contacts`](discover-contacts/SKILL.md) | Lower-level multi-source discovery/orchestration. | Skill and primitive docs. |

`$import-email` and `$import-contacts` are retired names. Use `$import-gmail` and
[`$import-messages`](../../messages/skills/import-messages/SKILL.md).

## Specialist workflows

The remaining skill folders expose narrower onboarding, source import, marker,
logbook, and compatibility surfaces. They are useful for explicit debugging or
specialized requests but should not replace the primary workflow when the route
above is clear.

The runtime ownership chain is:

```text
user command -> SKILL.md -> primitive/orchestrator -> .powerpacks artifacts
```

Source-specific imports write `network-import/import/<source>/people.csv`.
`index_contacts_pipeline.py fan-in` owns canonical source merging, and the Modal
or local indexing drivers own search-index construction.

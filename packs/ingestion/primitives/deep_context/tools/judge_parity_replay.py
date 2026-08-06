"""Estimate and execute the paid unified-judge parity replay."""
from __future__ import annotations

from typing import Any

import tiktoken

from packs.indexing.lib.openai_responses import estimate_cost_usd
from packs.ingestion.primitives.deep_context.db.models import IdentityOrigin
from packs.ingestion.primitives.deep_context.identity_evidence import (
    SYSTEM_PROMPT,
    identity_judge_prompt,
    judge_batch,
)
from packs.ingestion.primitives.deep_context.tools.judge_parity_data import (
    BINARY_VERDICTS,
    InstallEvaluation,
)

ESTIMATED_OUTPUT_TOKENS = 750


def estimate(
    installs: list[InstallEvaluation], model: str, effort: str,
) -> dict[str, Any]:
    encoder = tiktoken.get_encoding("o200k_base")
    input_tokens = 0
    replayable = 0
    for install in installs:
        for case in install.replay_cases:
            evidence = case.task["evidence"]
            profile = case.task["linkedin"]
            origin = (
                IdentityOrigin.RESEARCH
                if case.task.get("research_proposal")
                else IdentityOrigin.ATTACHED
            )
            prompt = identity_judge_prompt(evidence, profile, origin, install.owner_block)
            input_tokens += len(encoder.encode(SYSTEM_PROMPT + prompt))
            replayable += 1
    output_tokens = replayable * ESTIMATED_OUTPUT_TOKENS
    return {
        "status": "dry_run",
        "provider": "OpenAI",
        "model": model,
        "reasoning_effort": effort,
        "replayable": replayable,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": estimate_cost_usd(input_tokens, output_tokens, model),
        "note": "approximate; output and reasoning token use varies",
    }


def replay(
    installs: list[InstallEvaluation],
    *,
    model: str,
    effort: str,
    concurrency: int,
    timeout: int,
    max_retries: int,
) -> dict[str, Any]:
    reports = []
    flips = []
    for install in installs:
        cases = list(install.replay_cases)
        results = judge_batch(
            [case.task for case in cases],
            use_llm=True,
            owner_block=install.owner_block,
            model=model,
            effort=effort,
            concurrency=concurrency,
            timeout=timeout,
            max_retries=max_retries,
        )
        counts = {
            "replayed": len(cases),
            "errors": 0,
            "new_vs_old_agree": 0,
            "new_vs_old_flip": 0,
            "new_vs_human_agree": 0,
            "new_vs_human_disagree": 0,
            "new_vs_human_abstain": 0,
            "human_replayed": 0,
        }
        for case, result in zip(cases, results):
            if result.get("error"):
                counts["errors"] += 1
                continue
            replayed = str((result.get("verdict") or {}).get("verdict") or "").lower()
            if replayed == case.historical:
                counts["new_vs_old_agree"] += 1
            else:
                counts["new_vs_old_flip"] += 1
                flips.append(
                    {
                        "install": install.label,
                        "identifier": case.identifier,
                        "historical": case.historical,
                        "replay": replayed,
                        "human": case.human,
                    }
                )
            if case.human is None:
                continue
            counts["human_replayed"] += 1
            if replayed not in BINARY_VERDICTS:
                counts["new_vs_human_abstain"] += 1
            elif replayed == case.human:
                counts["new_vs_human_agree"] += 1
            else:
                counts["new_vs_human_disagree"] += 1
        reports.append({"install": install.label, **counts})
    return {"status": "completed", "replay": reports, "flips": flips}

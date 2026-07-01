"""Free, portable candidate judge that spawns the local `codex exec` CLI as a subprocess.

A drop-in, ~$0 replacement for `evaluate_profile_candidates` (which calls the paid gpt-5.4 API).
It is a THIN SHIM: it reuses the canonical module's rubric (`SYSTEM_PROMPT`), prompt builder
(`build_user_prompt`), frontier/profile loaders, AND deterministic scorer (`normalize_evaluation`)
— only the inference call is swapped for a `codex exec` subprocess. So the BAR is byte-identical to
the paid judge; only the engine differs. Output is the same `candidate_evaluations.raw.jsonl`
record shape, so `judge_consensus` ingests it unchanged.

The model returns the rubric's RICH judgment (per-trait statuses, excellence, seniority, caveats);
the deterministic scorer computes verdict + jd_score in code. We do NOT pass `--output-schema`
(it would conflict with the rubric's own output contract — that conflict made an earlier version
return all-"out"); instead we rely on the rubric's "Return ONLY a JSON object" + robust extraction.

Why subprocess: `codex exec` runs on the user's ChatGPT-subscription auth (no per-token spend) and
is portable — any harness with the `codex` CLI runs it. A Claude-CLI variant is the same shape.
See packs/search/skills/recruit/SKILL.md.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
EVAL_SRC = ROOT / "packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py"


def _tail(text: str | bytes | None, limit: int = 2000) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def _load_eval_module():
    spec = importlib.util.spec_from_file_location("evaluate_profile_candidates", EVAL_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # runs the module's sys.path setup + defs (no main)
    return mod


EV = _load_eval_module()


def extract_json(text: str) -> dict[str, Any]:
    """Pull the rubric JSON object out of the codex final message (handles fences/prose)."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Largest brace-balanced span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def judge_one(prompt: str, model: str | None, effort: str, timeout: int) -> tuple[dict[str, Any], str | None]:
    """Spawn one `codex exec`; return (parsed rich judgment, error-or-None)."""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=True) as out:
        cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
               "-o", out.name, "-c", f'model_reasoning_effort="{effort}"']
        if model:
            cmd += ["-m", model]
        try:
            cp = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=timeout, check=False)
            if cp.returncode != 0:
                detail = _tail(cp.stderr) or _tail(cp.stdout)
                return ({}, f"codex_exit_{cp.returncode}" + (f": {detail}" if detail else ""))
            out.seek(0)
            parsed = extract_json(out.read())
            return (parsed, None if parsed else "empty_or_unparsable")
        except subprocess.TimeoutExpired:
            return ({}, "timeout")
        except OSError as e:
            return ({}, str(e))


def main() -> None:
    ap = argparse.ArgumentParser(description="Free portable judge over a recruit run dir via `codex exec` subprocesses (same rubric+scorer as the paid judge).")
    ap.add_argument("--run-dir", required=True, help="Dir with plan.json + candidate_frontier.jsonl + probe_summaries.json")
    ap.add_argument("--max-candidates", type=int, default=0, help="Top-N frontier by best probe score (0 = all)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--model", default=None, help="codex model (default: codex config default)")
    ap.add_argument("--reasoning-effort", default="low", help="low|medium|high (default low — cheap/fast)")
    ap.add_argument("--timeout", type=int, default=120, help="Per-candidate subprocess timeout (s)")
    ap.add_argument("--out-name", default="candidate_evaluations.raw.jsonl")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    plan = EV.read_json(run_dir / "plan.json")
    frontier = EV.load_frontier(run_dir)
    frontier.sort(key=lambda c: (-EV.best_source_score(c), -len(c.get("matched_probe_ids", []) or [])))
    selected = frontier[: args.max_candidates] if args.max_candidates else frontier
    profiles = EV.collect_profiles(selected, run_dir)

    def judge(candidate: dict[str, Any]) -> dict[str, Any]:
        pid = candidate.get("person_id") or candidate.get("candidate_id")
        prof = profiles.get(pid)
        prompt = (EV.SYSTEM_PROMPT +
                  "\n\nIMPORTANT: do NOT use tools or read files; reply with ONLY the JSON object "
                  "specified in the OUTPUT section.\n\n" + EV.build_user_prompt(plan, prof or {}))
        parsed, err = judge_one(prompt, args.model, args.reasoning_effort, args.timeout)
        rec = EV.normalize_evaluation(parsed, plan) if parsed else {
            "jd_score": 0.0, "verdict": "out", "seniority_fit": "unknown", "rationale": ""}
        rec["candidate_id"] = pid
        rec["error"] = err
        return rec

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = list(ex.map(judge, selected))

    out_path = run_dir / args.out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, sort_keys=True) + "\n")

    counts = {v: sum(1 for r in results if r.get("verdict") == v) for v in ("top_tier", "high_potential", "out")}
    errors = sum(1 for r in results if r.get("error"))
    if selected and errors == len(selected):
        print(json.dumps({"primitive": "codex_judge", "status": "failed", "judged": len(results),
                          "errors": errors, "error": "all codex subprocess judgments failed",
                          "out": str(out_path)}, indent=2))
        raise SystemExit(1)
    print(json.dumps({"primitive": "codex_judge", "status": "completed", "judged": len(results),
                      "verdicts": counts, "errors": errors, "model": args.model or "codex-default",
                      "reasoning_effort": args.reasoning_effort, "out": str(out_path)}, indent=2))


if __name__ == "__main__":
    main()

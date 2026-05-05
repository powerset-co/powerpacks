#!/usr/bin/env bash
# Drive the llm_rerank_candidates primitive against REAL OpenAI with the
# canonical "ai or software engineer at open ai" example query. This
# spends OpenAI credits — only run manually.
#
# Default: 200 synthetic candidates, concurrency 50.
# Override via env vars:
#   COUNT=400        N_synthetic_candidates=400
#   CONCURRENCY=200  asyncio.Semaphore(N)
#   MODEL=gpt-4o-mini
#
# The synthetic candidates mix:
#   - 50% genuinely match the query (AI eng at OpenAI)
#   - 25% partial match (software engineer at other AI lab)
#   - 25% irrelevant (bakers, accountants, etc.)
#
# After the run, prints a histogram of verdicts so you can sanity-check
# that the rerank actually works and didn't just include everything.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=python3
PRIM="$ROOT/packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "error: OPENAI_API_KEY not set. Either:"
  echo "  export OPENAI_API_KEY=sk-..."
  echo "  # or pull from your env via:"
  echo "  source .env"
  exit 2
fi

COUNT="${COUNT:-200}"
CONCURRENCY="${CONCURRENCY:-50}"
MODEL="${MODEL:-gpt-4o-mini}"

TMP="$(mktemp -d -t powerpacks-rerank-XXXX)"
trap 'echo; echo "[rerank] artifacts: $TMP"' EXIT

echo "==generating $COUNT synthetic candidates =="
$PY - <<EOF >"$TMP/candidates.jsonl"
import json, random
random.seed(42)
N = $COUNT

match_titles = [
    "AI Engineer at OpenAI",
    "Software Engineer at OpenAI",
    "Senior ML Engineer at OpenAI",
    "Research Engineer at OpenAI",
    "Member of Technical Staff at OpenAI",
    "Applied AI Engineer at OpenAI",
]
partial_titles = [
    "AI Engineer at Anthropic",
    "ML Engineer at Google DeepMind",
    "Software Engineer at Meta AI",
    "Research Engineer at Mistral",
    "AI Infrastructure Engineer at Cohere",
]
irrelevant_titles = [
    "Pastry Chef at Tartine Bakery",
    "Accountant at Smith & Co",
    "Marketing Director at LVMH",
    "Plumber at SF Plumbing Co-op",
    "Wine Sommelier at Chez Panisse",
    "High School History Teacher",
]

def pick():
    r = random.random()
    if r < 0.50:
        return random.choice(match_titles), "match"
    elif r < 0.75:
        return random.choice(partial_titles), "partial"
    else:
        return random.choice(irrelevant_titles), "irrelevant"

for i in range(N):
    headline, label = pick()
    person = {
        "id": f"p{i:04d}",
        "name": f"Person {i}",
        "headline": headline,
        "location": random.choice(["San Francisco", "New York", "London", "Berlin"]),
        "_synthetic_label": label,
    }
    print(json.dumps(person))
EOF

count=$(wc -l <"$TMP/candidates.jsonl" | tr -d ' ')
echo "  generated $count candidates → $TMP/candidates.jsonl"

echo
echo "==running rerank: model=$MODEL concurrency=$CONCURRENCY =="
time $PY "$PRIM" \
  --in "$TMP/candidates.jsonl" \
  --out "$TMP/reranked.jsonl" \
  --query "ai or software engineer at open ai" \
  --traits "ai or software engineer" \
  --traits "at openai" \
  --concurrency "$CONCURRENCY" \
  --model "$MODEL"

echo
echo "==verdict histogram by synthetic label =="
$PY - <<EOF
import json
from collections import Counter
results = [json.loads(l) for l in open("$TMP/reranked.jsonl")]
print(f"  total: {len(results)}")
print(f"  ok:    {sum(1 for r in results if r['error'] is None)}")
print(f"  failed:{sum(1 for r in results if r['error'] is not None)}")
print()
groups = {}
for r in results:
    label = r["input"].get("_synthetic_label", "?")
    groups.setdefault(label, []).append(r)
for label in ("match", "partial", "irrelevant"):
    grp = groups.get(label, [])
    if not grp:
        continue
    avg = sum(r["score"] for r in grp) / len(grp)
    incl = sum(1 for r in grp if r["verdict"] == "include")
    print(f"  {label:>10}  n={len(grp):>3}  avg_score={avg:.2f}  include={incl}/{len(grp)}")
EOF

echo
echo "[rerank] artifacts saved to: $TMP"

#!/usr/bin/env python3
"""Run bucketed aleph recall cases through Powerpacks primitives.

This harness intentionally bypasses aleph's /expand and /execute endpoints. It
uses deterministic Powerpacks-style decomposition heuristics so failures point
at primitive coverage, contracts, or missing expansion rules.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "primitives"
TASK_STATE = PRIMITIVES / "task_state" / "task_state.py"
REPORT_PATH = ROOT / "evals" / "recall_parity.md"
DEFAULT_APP_DIR = Path("/Users/arthur/workspace/aleph-mvp")
DEFAULT_RECALL_DIR = DEFAULT_APP_DIR / "tests" / "recall"
RESULT_LIMIT_CAP = 1000

ROLE_SPECS: dict[str, dict[str, Any]] = {
    "software_engineer": {
        "semantic": (
            "People who build, debug, and maintain production software systems. "
            "They write application, backend, frontend, mobile, platform, infrastructure, or systems code and have "
            "hands-on responsibility for shipping software in a professional engineering environment."
        ),
        "bm25": ["software engineer", "software developer", "SWE", "backend engineer", "frontend engineer"],
        "role_tracks": ["engineering"],
    },
    "ai_engineer": {
        "semantic": (
            "People who build, train, evaluate, or deploy artificial intelligence and machine learning systems. "
            "Their profiles may mention machine learning engineering, applied AI, model infrastructure, research "
            "engineering, computer vision, NLP, data science, or production AI systems."
        ),
        "bm25": ["AI engineer", "machine learning engineer", "ML engineer", "applied scientist", "research scientist"],
        "role_tracks": ["engineering"],
    },
    "data_scientist": {
        "semantic": (
            "People who analyze data, build statistical or machine learning models, run experiments, and turn data "
            "into product or business decisions. Their profiles should show data science, analytics, experimentation, "
            "machine learning, or quantitative modeling work."
        ),
        "bm25": ["data scientist", "machine learning scientist", "analytics scientist", "data science"],
        "role_tracks": ["data"],
    },
    "devops": {
        "semantic": (
            "People who operate infrastructure, reliability, cloud platforms, Kubernetes, CI/CD, deployment systems, "
            "and developer operations. Their profiles should show DevOps, site reliability, infrastructure, platform, "
            "or production operations engineering work."
        ),
        "bm25": ["devops engineer", "site reliability engineer", "SRE", "infrastructure engineer", "platform engineer"],
        "role_tracks": ["engineering"],
    },
    "founder": {
        "semantic": (
            "People who founded or co-founded companies and hold founder, cofounder, founding CEO, founding CTO, or "
            "similar founding operator roles. Their profiles should show responsibility for starting, building, "
            "fundraising for, or leading a startup or technology company."
        ),
        "bm25": ["founder", "co-founder", "cofounder", "founding CEO", "founding CTO", "founder CEO"],
        "role_ids": ["founder"],
    },
    "product": {
        "semantic": (
            "People who define product strategy, prioritize roadmaps, work with engineering and design, and ship "
            "software or technical products. Their profiles should show product management, product leadership, "
            "technical product ownership, or product operations responsibilities."
        ),
        "bm25": ["product manager", "PM", "product lead", "technical product manager"],
        "role_tracks": ["product"],
    },
    "operations": {
        "semantic": (
            "People who run business operations, strategy and operations, internal processes, marketplace operations, "
            "or startup operating functions. Their profiles should show operational ownership, cross-functional "
            "execution, process design, or business operations leadership."
        ),
        "bm25": ["operations", "business operations", "strategy and operations", "ops"],
        "role_tracks": ["operations"],
    },
    "finance": {
        "semantic": (
            "People who work in finance, accounting, strategic finance, FP&A, investment analysis, banking, or "
            "financial operations. Their profiles should show responsibility for financial planning, accounting, "
            "capital markets, investment work, or finance team execution."
        ),
        "bm25": ["finance", "strategic finance", "accounting", "FP&A", "financial analyst"],
        "role_tracks": ["finance"],
    },
    "sales": {
        "semantic": (
            "People who sell products, lead revenue teams, manage sales pipelines, run business development, or own "
            "go-to-market execution. Their profiles should show sales, account executive, partnerships, business "
            "development, revenue, or GTM responsibilities."
        ),
        "bm25": ["sales", "account executive", "business development", "GTM", "revenue"],
        "role_tracks": ["sales"],
    },
    "marketing": {
        "semantic": (
            "People who lead or execute marketing, growth, demand generation, brand, content, product marketing, or "
            "go-to-market programs. Their profiles should show ownership of customer acquisition, market positioning, "
            "campaigns, growth strategy, or marketing leadership."
        ),
        "bm25": ["marketing", "growth marketing", "product marketing", "demand generation"],
        "role_tracks": ["marketing"],
    },
    "investor": {
        "semantic": (
            "People who invest in startups, source deals, advise portfolio companies, or work in venture capital, "
            "angel investing, private equity, or funds. Their profiles should show investment, partner, scout, "
            "venture, or capital allocation experience."
        ),
        "bm25": ["investor", "venture capitalist", "general partner", "angel investor", "scout investor"],
        "role_tracks": ["finance"],
    },
    "executive": {
        "semantic": (
            "People who hold senior leadership roles and own company, function, or business-unit outcomes. Their "
            "profiles should show executive, C-level, VP, head of, director, general manager, or leadership scope."
        ),
        "bm25": ["executive", "CEO", "COO", "CTO", "VP", "head of", "director"],
        "seniority_bands": ["director", "vice_president", "c_suite", "partner", "owner"],
    },
    "generic": {
        "semantic": (
            "People with professional work experience matching the requested companies, locations, education, dates, "
            "or other hard constraints. Their profiles should contain relevant roles, company affiliations, and "
            "career history for the user's search request."
        ),
        "bm25": ["founder", "engineer", "product manager", "investor", "operator", "executive"],
    },
}

DOMAIN_SPECS: list[tuple[str, dict[str, Any]]] = [
    ("database", {
        "sector_types": ["data"],
        "company_semantic_queries": [
            "Companies building database systems, hosted databases, data storage engines, SQL or NoSQL databases, data warehouses, or developer platforms for managing application data."
        ],
    }),
    ("fintech", {
        "sector_types": ["fintech"],
        "company_semantic_queries": [
            "Financial technology companies building payments, lending, banking, credit, investing, insurance, payroll, accounting, crypto finance, or software for financial services."
        ],
    }),
    ("semiconductor", {
        "sector_types": ["semiconductors"],
        "company_semantic_queries": [
            "Companies building semiconductors, chips, processors, semiconductor equipment, electronic design automation, silicon systems, or hardware for compute infrastructure."
        ],
    }),
    ("climate", {
        "sector_types": ["climate"],
        "company_semantic_queries": [
            "Climate technology companies working on clean energy, carbon, electrification, batteries, grid infrastructure, sustainability, or decarbonization."
        ],
    }),
    ("crypto", {
        "sector_types": ["crypto_web3"],
        "company_semantic_queries": [
            "Crypto and web3 companies building blockchain infrastructure, digital assets, wallets, exchanges, protocols, or decentralized finance products."
        ],
    }),
    ("cybersecurity", {
        "sector_types": ["security"],
        "company_semantic_queries": [
            "Cybersecurity companies building security products, identity, threat detection, cloud security, application security, compliance, or security infrastructure."
        ],
    }),
    ("healthcare", {
        "sector_types": ["healthcare"],
        "company_semantic_queries": [
            "Healthcare companies building clinical, provider, payer, digital health, biotech, medical, patient care, or healthcare operations products and services."
        ],
    }),
    ("mental health", {
        "sector_types": ["healthcare"],
        "company_semantic_queries": [
            "Mental health companies building therapy, behavioral health, psychiatry, wellness, care delivery, or digital mental health products and services."
        ],
    }),
    ("logistics", {
        "sector_types": ["logistics"],
        "company_semantic_queries": [
            "Logistics and supply chain companies working on freight, shipping, warehousing, transportation, delivery, procurement, or supply chain operations."
        ],
    }),
    ("saas", {
        "sector_types": ["enterprise_saas"],
        "company_semantic_queries": [
            "Software as a service companies selling cloud software, workflow tools, business applications, or subscription software products to organizations."
        ],
    }),
    ("developer", {
        "sector_types": ["infra_devtools"],
        "company_semantic_queries": [
            "Companies building developer tooling, infrastructure software, cloud infrastructure, observability, CI/CD, APIs, databases, or technical platforms used by engineering teams."
        ],
    }),
    ("infrastructure", {
        "sector_types": ["infra_devtools"],
        "company_semantic_queries": [
            "Infrastructure software companies building cloud platforms, developer tools, data infrastructure, observability, APIs, networking, compute, or systems used by technical teams."
        ],
    }),
    ("ai", {
        "sector_types": ["ai_ml"],
        "company_semantic_queries": [
            "Artificial intelligence and machine learning companies building model infrastructure, AI applications, data platforms, developer tools, agents, research systems, or applied ML products."
        ],
    }),
]

COMPANY_ALIASES = {
    "facebook": ["Facebook", "Meta"],
    "meta": ["Meta", "Facebook"],
    "twitter": ["Twitter", "X"],
    "x": ["X", "Twitter"],
    "google": ["Google", "Alphabet"],
    "alphabet": ["Alphabet", "Google"],
    "airbnb": ["Airbnb"],
    "thumbtack": ["Thumbtack"],
    "vercel": ["Vercel"],
    "box": ["Box"],
    "insight partners": ["Insight Partners"],
    "jpmorgan chase": ["JPMorgan Chase"],
    "bank of america": ["Bank of America"],
}

INVESTOR_ALIASES = {
    "sequoia": "Sequoia Capital",
    "amplify": "Amplify Partners",
    "elad gil": "Elad Gil",
    "naval ravikant": "Naval Ravikant",
    "peter thiel": "Peter Thiel",
    "sam altman": "Sam Altman",
}

DOMAIN_PATTERNS = {
    "ai": re.compile(r"\b(ai|artificial intelligence|machine learning|ml)\b"),
    "developer": re.compile(r"\b(developer|devtools?|dev tooling)\b"),
}


@dataclass
class CaseMeta:
    path: Path
    relpath: str
    bucket: str
    query: str
    limit: int
    expected_count: int
    min_recall: float
    expected_ids: list[str]
    ignored_v4_ids: list[str]
    data: dict[str, Any]


def sh(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    log_path.write_text(
        "$ " + " ".join(args) + "\n\n"
        + "STDOUT:\n" + completed.stdout
        + "\nSTDERR:\n" + completed.stderr
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(args)}\nsee {log_path}")
    return completed


def now_slug() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def base_uuid(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 6 and parts[5].isdigit():
        return "-".join(parts[:5])
    return str(value)


def uuid_version(value: str) -> str | None:
    raw = base_uuid(value).lower()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw):
        return None
    return raw.split("-")[2][0]


def bucket_for(relpath: str) -> str:
    if relpath.startswith("staging/"):
        return "staging"
    stem = Path(relpath).stem
    for prefix in ["date_range", "company", "education", "founders", "funding", "industry", "investor", "leaders", "location", "mixed", "role", "skills", "social"]:
        if stem.startswith(prefix + "_") or stem == prefix:
            return prefix
    return stem.split("_", 1)[0]


def load_case(path: Path, recall_dir: Path) -> CaseMeta:
    data = yaml.safe_load(path.read_text()) or {}
    relpath = str(path.relative_to(recall_dir))
    expected_raw = [str(value) for value in data.get("expected_person_ids") or []]
    expected_ids = [base_uuid(value) for value in expected_raw if uuid_version(value) != "4"]
    ignored_v4_ids = [base_uuid(value) for value in expected_raw if uuid_version(value) == "4"]
    return CaseMeta(
        path=path,
        relpath=relpath,
        bucket=bucket_for(relpath),
        query=str(data.get("query") or Path(relpath).stem.replace("_", " ")),
        limit=int(data.get("limit") or RESULT_LIMIT_CAP),
        expected_count=int(data.get("expected_count") or 0),
        min_recall=float(data.get("min_recall") or 0.5),
        expected_ids=list(dict.fromkeys(expected_ids)),
        ignored_v4_ids=list(dict.fromkeys(ignored_v4_ids)),
        data=data,
    )


def role_spec_for(query: str) -> dict[str, Any]:
    q = query.lower()
    if "founder" in q or "cofounder" in q or "co-founder" in q:
        return ROLE_SPECS["founder"]
    if "devops" in q or "kubernetes" in q:
        return ROLE_SPECS["devops"]
    if "ai researcher" in q or "ai engineer" in q or "machine learning" in q or re.search(r"\bml\b", q):
        return ROLE_SPECS["ai_engineer"]
    if "data scientist" in q or "data science" in q:
        return ROLE_SPECS["data_scientist"]
    if "software engineer" in q or "engineer" in q or "developer" in q:
        return ROLE_SPECS["software_engineer"]
    if "product" in q or re.search(r"\bpms?\b", q):
        return ROLE_SPECS["product"]
    if "operations" in q or re.search(r"\bops\b", q):
        return ROLE_SPECS["operations"]
    if "finance" in q or "accounting" in q or "banking" in q:
        return ROLE_SPECS["finance"]
    if "sales" in q or "gtm" in q or "business development" in q:
        return ROLE_SPECS["sales"]
    if "marketing" in q:
        return ROLE_SPECS["marketing"]
    if "investor" in q or "venture capitalist" in q:
        return ROLE_SPECS["investor"]
    if "leader" in q or "executive" in q or "cto" in q or "ceo" in q:
        return ROLE_SPECS["executive"]
    return ROLE_SPECS["generic"]


def add_unique(payload: dict[str, Any], key: str, values: list[Any]) -> None:
    active = [value for value in payload.get(key, []) if value]
    active.extend(value for value in values if value)
    if active:
        payload[key] = list(dict.fromkeys(active))


def apply_location(query: str, payload: dict[str, Any]) -> None:
    q = query.lower()
    if "argentina" in q:
        add_unique(payload, "countries", ["Argentina"])
    if "san francisco" in q or re.search(r"\bsf\b", q):
        add_unique(payload, "cities", ["San Francisco"])
        add_unique(payload, "states", ["California"])
    if "california" in q:
        add_unique(payload, "states", ["California"])
    if "new york city" in q or "nyc" in q or "new york" in q:
        add_unique(payload, "cities", ["New York"])
        add_unique(payload, "states", ["New York"])
    if "usa" in q or "united states" in q:
        add_unique(payload, "countries", ["United States"])
    if "middle east" in q:
        add_unique(payload, "macro_regions", ["Middle East"])
    if "europe" in q:
        add_unique(payload, "company_macro_regions" if "headquarter" in q else "macro_regions", ["Europe"])


def apply_companies(query: str, payload: dict[str, Any]) -> None:
    q = query.lower()
    for needle, names in COMPANY_ALIASES.items():
        if needle in q:
            add_unique(payload, "company_names", names)
    for needle, investor in INVESTOR_ALIASES.items():
        if needle in q and ("backed" in q or "funded" in q or "investor" in q):
            add_unique(payload, "investor_names", [investor])

    if "startup" in q:
        add_unique(payload, "entity_types", ["venture_backed_startup"])
    if "public compan" in q:
        add_unique(payload, "entity_types", ["public_company"])
    if "series a" in q:
        payload["funding_stage_min"] = "series_a"
        payload["funding_stage_max"] = "series_a"
    if "series b or later" in q or "series b plus" in q:
        payload["funding_stage_min"] = "series_b"
    elif "series b" in q:
        payload["funding_stage_min"] = "series_b"
        payload["funding_stage_max"] = "series_b"
    if "seed or earlier" in q or "early stage" in q:
        payload["funding_stage_max"] = "seed"
    if "max 50 headcount" in q or "50 headcount" in q:
        payload["headcount_max"] = 50
    if "over 2mm" in q or "over 2m" in q:
        payload["funding_amount_min"] = 2_000_000

    for needle, spec in DOMAIN_SPECS:
        pattern = DOMAIN_PATTERNS.get(needle)
        matched = bool(pattern.search(q)) if pattern else bool(re.search(rf"\b{re.escape(needle)}\b", q))
        if matched:
            add_unique(payload, "sector_types", spec.get("sector_types", []))
            add_unique(payload, "company_semantic_queries", spec.get("company_semantic_queries", []))
            payload.setdefault("company_sector_strategy", "staged")


def apply_education(query: str, payload: dict[str, Any]) -> None:
    q = query.lower()
    if "stanford" in q:
        add_unique(payload, "education_names", ["Stanford University"])
    if "berkeley" in q or "cal" in q:
        add_unique(payload, "education_names", ["University of California, Berkeley"])
    if "wharton" in q:
        add_unique(payload, "education_names", ["Wharton School"])
    if re.search(r"\bmit\b", q):
        add_unique(payload, "education_names", ["Massachusetts Institute of Technology"])
    if "harvard" in q:
        add_unique(payload, "education_names", ["Harvard University"])
    if "both stanford and berkeley" in q or "both stanford and cal" in q:
        payload["education_op"] = "and"
    if "phd" in q or "phds" in q:
        add_unique(payload, "degree_levels", ["phd"])
    if "psychology" in q:
        add_unique(payload, "fields_of_study", ["psychology"])
    if "recent stanford graduates" in q:
        payload["graduation_year_min"] = 2023
        payload["graduation_year_max"] = 2026
    elif "recent grads" in q or "last 5 years" in q:
        payload["graduation_year_min"] = 2021
        payload["graduation_year_max"] = 2026
    match = re.search(r"(?:graduates?|grads?).*?between\s+(20\d{2})\s+(?:and|to|-)\s+(20\d{2})", q)
    if match:
        payload["graduation_year_min"] = int(match.group(1))
        payload["graduation_year_max"] = int(match.group(2))


def apply_temporal(query: str, payload: dict[str, Any]) -> None:
    q = query.lower()
    if "graduat" in q or "grads" in q:
        return
    around = re.search(r"around\s+(20\d{2})", q)
    if around:
        year = int(around.group(1))
        payload["position_after_date"] = str(year - 1)
        payload["position_before_date"] = str(year + 1)
    between = re.search(r"between\s+(20\d{2})\s+(?:and|to|-)\s+(20\d{2})", q) or re.search(r"(20\d{2})\s*-\s*(20\d{2})", q)
    if between:
        payload["position_after_date"] = between.group(1)
        payload["position_before_date"] = between.group(2)
    in_year = re.search(r"\bin\s+(20\d{2})\b", q)
    if in_year and "graduat" not in q:
        payload["position_after_date"] = in_year.group(1)
        payload["position_before_date"] = in_year.group(1)
    after = re.search(r"\bafter\s+(20\d{2})\b", q)
    if after:
        payload["position_after_date"] = after.group(1)
    since = re.search(r"\bsince\s+(20\d{2})\b", q)
    if since:
        payload["position_after_date"] = since.group(1)
    if "at least 10 years" in q:
        payload["years_experience_min"] = 10
    elif "at least 2 years" in q:
        payload["years_experience_min"] = 2


def apply_skills(query: str, payload: dict[str, Any]) -> None:
    q = query.lower()
    skills = []
    for needle, skill in [
        ("kubernetes", "kubernetes"),
        ("python", "python"),
        ("machine learning", "machine_learning"),
        ("credit risk", "credit_risk"),
        ("blockchain", "blockchain"),
    ]:
        if needle in q:
            skills.append(skill)
    add_unique(payload, "tech_skills", skills)


def apply_social(query: str, payload: dict[str, Any]) -> None:
    q = query.lower()
    if "100k linkedin" in q:
        payload["li_followers_min"] = 100_000
    elif "50k linkedin" in q:
        payload["li_followers_min"] = 50_000
    if "10k+ twitter" in q or "over 10k x" in q or "10k x" in q:
        payload["x_followers_min"] = 10_000


def apply_yaml_overrides(meta: CaseMeta, payload: dict[str, Any]) -> None:
    data = meta.data
    for key in [
        "degree_levels",
        "fields_of_study",
        "graduation_year_min",
        "graduation_year_max",
        "seniority_bands",
        "sector_types",
        "is_current",
        "headcount_min",
        "headcount_max",
        "funding_stage_min",
        "funding_stage_max",
    ]:
        if key in data:
            payload[key] = data[key]
    if data.get("use_expand_seniority") and not payload.get("seniority_bands"):
        payload["seniority_bands"] = ["director", "vice_president", "c_suite", "partner", "owner"]


def decompose_case(meta: CaseMeta) -> dict[str, Any]:
    query = meta.query
    spec = role_spec_for(query)
    payload: dict[str, Any] = {
        "semantic_query": spec["semantic"],
        "bm25_queries": list(spec["bm25"]),
        "has_domain_intent": bool(meta.data.get("has_domain_intent", False)),
        "adjacency_mode": "off",
    }
    for key in ["role_ids", "role_tracks", "seniority_bands"]:
        if spec.get(key):
            payload[key] = list(spec[key])

    apply_location(query, payload)
    apply_companies(query, payload)
    apply_education(query, payload)
    apply_temporal(query, payload)
    apply_skills(query, payload)
    apply_social(query, payload)
    apply_yaml_overrides(meta, payload)

    if "current" in query.lower() or "currently" in query.lower():
        payload.setdefault("is_current", True)
    if payload.get("company_semantic_queries") and not payload.get("company_sector_strategy"):
        payload["company_sector_strategy"] = "staged"
    if payload.get("company_names") or payload.get("company_semantic_queries") or payload.get("investor_names"):
        payload["prefilters"] = {
            "stages": [
                {
                    "stage": "large_company_intersection",
                    "mode": "intersect_base_ids",
                    "output": "base_candidate_ids",
                    "reason": "Resolve company constraints to company IDs before people retrieval when the set is large.",
                }
            ]
        }
    return payload


def latest_step(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def record_step(app_dir: Path, state: Path, step_id: str, output: dict[str, Any], env: dict[str, str], log_dir: Path) -> None:
    sh(
        [
            sys.executable,
            str(TASK_STATE),
            "record-step",
            "--state",
            str(state),
            "--step-id",
            step_id,
            "--output-json",
            json.dumps(output, separators=(",", ":")),
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{state.stem}-{step_id}.log",
    )


def needs_company_resolution(payload: dict[str, Any]) -> bool:
    return any(payload.get(key) for key in [
        "company_names",
        "company_semantic_queries",
        "sector_types",
        "entity_types",
        "funding_stage_min",
        "funding_stage_max",
        "funding_amount_min",
        "funding_amount_max",
        "headcount_min",
        "headcount_max",
        "investor_names",
        "investors",
    ])


def needs_education_resolution(payload: dict[str, Any]) -> bool:
    return bool(payload.get("education_names") or payload.get("school_names"))


def needs_prefilters(payload: dict[str, Any]) -> bool:
    return any(payload.get(key) for key in [
        "education_names",
        "education_ids",
        "degree_levels",
        "fields_of_study",
        "graduation_year_min",
        "graduation_year_max",
        "tech_skills",
        "company_names",
        "company_semantic_queries",
        "sector_types",
        "investor_names",
        "investors",
    ])


def run_case(
    app_dir: Path,
    meta: CaseMeta,
    env: dict[str, str],
    run_dir: Path,
    log_dir: Path,
    limit_cap: int,
    env_file: str,
    *,
    decomposition: dict[str, Any] | None = None,
    decomposition_reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": Path(meta.relpath).with_suffix("").as_posix().replace("/", "__"),
        "source": meta.relpath,
        "bucket": meta.bucket,
        "query": meta.query,
        "expected_id_count": len(meta.expected_ids),
        "ignored_v4_count": len(meta.ignored_v4_ids),
        "expected_count": meta.expected_count,
        "status": "pending",
    }
    if not meta.expected_ids and not meta.expected_count:
        result.update({"status": "ignored", "reason": "no comparable v5 expected IDs or expected_count after ignoring v4 IDs"})
        return result

    result_limit = min(meta.limit, limit_cap)
    init = sh(
        [
            sys.executable,
            str(TASK_STATE),
            "init",
            "--query",
            meta.query,
            "--out-dir",
            str(run_dir),
            "--task-id",
            f"recall-{result['id']}",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{result['id']}-init.log",
    )
    state_path = Path(json.loads(init.stdout)["state"])
    result["state"] = str(state_path)

    if decomposition is None:
        decomposition = {
            "role_search_filters": decompose_case(meta),
            "extraction_source": "deterministic_recall_harness",
        }
    payload = decomposition.get("role_search_filters") or decomposition
    result["payload"] = payload
    record_step(
        app_dir,
        state_path,
        "expand_search_request",
        {
            "role_search_filters": payload,
            "decomposition": decomposition,
        },
        env,
        log_dir,
    )
    record_step(
        app_dir,
        state_path,
        "decide_search_strategy",
        {
            "strategy": "count_then_execute",
            "reason": decomposition_reason or "Recall parity harness uses deterministic decomposition, primitive count, retrieval, hydration, and export.",
            "candidate_limit": result_limit,
            "hydrate_limit": result_limit,
        },
        env,
        log_dir,
    )
    sh(
        [
            sys.executable,
            str(TASK_STATE),
            "approve",
            "--state",
            str(state_path),
            "--execution-mode",
            "search_only",
            "--note",
            "Recall parity harness.",
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{result['id']}-approve.log",
    )

    if payload.get("investor_names") or payload.get("investors"):
        sh(
            [
                sys.executable,
                str(PRIMITIVES / "resolve_investors" / "resolve_investors.py"),
                "--state",
                str(state_path),
                "--env-file",
                env_file,
                "--write-state",
            ],
            cwd=app_dir,
            env=env,
            log_path=log_dir / f"{result['id']}-resolve-investors.log",
        )
    if needs_company_resolution(payload):
        sh(
            [
                sys.executable,
                str(PRIMITIVES / "resolve_companies" / "resolve_companies.py"),
                "--state",
                str(state_path),
                "--env-file",
                env_file,
                "--write-state",
            ],
            cwd=app_dir,
            env=env,
            log_path=log_dir / f"{result['id']}-resolve-companies.log",
        )
    if needs_education_resolution(payload):
        sh(
            [
                sys.executable,
                str(PRIMITIVES / "resolve_education" / "resolve_education.py"),
                "--state",
                str(state_path),
                "--env-file",
                env_file,
                "--write-state",
            ],
            cwd=app_dir,
            env=env,
            log_path=log_dir / f"{result['id']}-resolve-education.log",
        )
    if needs_prefilters(payload):
        sh(
            [
                sys.executable,
                str(PRIMITIVES / "apply_prefilters" / "apply_prefilters.py"),
                "--state",
                str(state_path),
                "--env-file",
                env_file,
                "--write-state",
            ],
            cwd=app_dir,
            env=env,
            log_path=log_dir / f"{result['id']}-apply-prefilters.log",
        )

    for primitive, log_suffix in [
        ("count_candidates/count_candidates.py", "count"),
        ("execute_role_search/execute_role_search.py", "execute"),
        ("hydrate_people/hydrate_people.py", "hydrate"),
    ]:
        args = [
            sys.executable,
            str(PRIMITIVES / primitive),
            "--state",
            str(state_path),
            "--env-file",
            env_file,
            "--write-state",
        ]
        if log_suffix == "execute":
            args.extend(["--limit", str(result_limit), "--top-k", str(max(1000, result_limit))])
        sh(args, cwd=app_dir, env=env, log_path=log_dir / f"{result['id']}-{log_suffix}.log")

    export = sh(
        [
            sys.executable,
            str(PRIMITIVES / "persist_search_results" / "results_io.py"),
            "export",
            "--state",
            str(state_path),
        ],
        cwd=app_dir,
        env=env,
        log_path=log_dir / f"{result['id']}-export.log",
    )

    state = json.loads(state_path.read_text())
    count = latest_step(state, "count_candidates")
    retrieval = latest_step(state, "execute_role_search")
    hydration = latest_step(state, "hydrate_people")
    artifact = json.loads(export.stdout)
    candidate_ids = {base_uuid(pid) for pid in retrieval.get("candidate_ids") or []}
    expected_ids = set(meta.expected_ids)
    hits = sorted(pid for pid in expected_ids if pid in candidate_ids)
    recall = (len(hits) / len(expected_ids)) if expected_ids else None
    if expected_ids:
        passed = bool(recall is not None and recall >= meta.min_recall)
    else:
        passed = int(retrieval.get("returned_people") or 0) >= meta.expected_count

    result.update({
        "status": "pass" if passed else "fail",
        "unique_people_count": count.get("unique_people"),
        "position_rows_count": count.get("position_rows"),
        "returned_people": retrieval.get("returned_people"),
        "hydrated": hydration.get("hydrated"),
        "hit_count": len(hits),
        "recall": recall,
        "missed_ids": sorted(expected_ids - set(hits))[:20],
        "csv": artifact.get("csv"),
        "jsonl": artifact.get("jsonl"),
    })
    return result


def select_cases(recall_dir: Path, bucket: str | None, case_glob: str | None, include_staging: bool) -> list[CaseMeta]:
    paths = sorted(recall_dir.rglob("*.yaml"))
    cases = [load_case(path, recall_dir) for path in paths]
    if not include_staging:
        cases = [case for case in cases if case.bucket != "staging"]
    if bucket:
        cases = [case for case in cases if case.bucket == bucket]
    if case_glob:
        regex = re.compile(case_glob)
        cases = [case for case in cases if regex.search(case.relpath)]
    return cases


def write_report(results: list[dict[str, Any]], app_dir: Path, run_dir: Path, log_dir: Path) -> None:
    now = now_slug()
    buckets = sorted({row["bucket"] for row in results})
    lines = [
        "# Recall Parity",
        "",
        f"Last run: `{now}`",
        "",
        "Scope: aleph recall YAMLs executed through Powerpacks primitives with deterministic decomposition.",
        "",
        f"App dir: `{app_dir}`",
        f"Run dir: `{run_dir}`",
        f"Log dir: `{log_dir}`",
        "",
        "Execution notes:",
        "",
        "- Does not call aleph `/expand` or `/execute`.",
        "- Ignores UUIDv4 expected IDs because those are staging/non-comparable to current UUIDv5 person IDs.",
        "- Uses Powerpacks primitives for company, investor, education, prefilter, count, retrieval, hydration, and persistence.",
        "- Failures are primitive/decomposition parity gaps, not LLM reranker failures.",
        "",
        "| Bucket | Pass | Fail | Ignored | Cases |",
        "|---|---:|---:|---:|---:|",
    ]
    for bucket in buckets:
        rows = [row for row in results if row["bucket"] == bucket]
        lines.append(
            f"| {bucket} | "
            f"{sum(1 for row in rows if row['status'] == 'pass')} | "
            f"{sum(1 for row in rows if row['status'] == 'fail')} | "
            f"{sum(1 for row in rows if row['status'] == 'ignored')} | "
            f"{len(rows)} |"
        )
    lines.extend([
        "",
        "| Case | Bucket | Status | Count | Returned | Hydrated | Expected Hits | Recall | Ignored v4 | Artifact | Notes |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ])
    for row in results:
        recall = row.get("recall")
        recall_text = "" if recall is None else f"{recall:.0%}"
        expected = row.get("expected_id_count") or 0
        hits = row.get("hit_count") or 0
        note = row.get("reason") or ""
        if row.get("missed_ids"):
            note = f"missed {len(row['missed_ids'])}+ expected ids"
        lines.append(
            "| {case} | {bucket} | {status} | {count} | {returned} | {hydrated} | {hits}/{expected} | {recall} | {ignored} | `{artifact}` | {note} |".format(
                case=row["source"].replace("|", "\\|"),
                bucket=row["bucket"],
                status=row["status"],
                count=row.get("unique_people_count", ""),
                returned=row.get("returned_people", ""),
                hydrated=row.get("hydrated", ""),
                hits=hits,
                expected=expected,
                recall=recall_text,
                ignored=row.get("ignored_v4_count", 0),
                artifact=row.get("csv") or row.get("state") or "",
                note=str(note).replace("|", "\\|"),
            )
        )
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Powerpacks recall parity harness")
    parser.add_argument("--app-dir", default=str(DEFAULT_APP_DIR))
    parser.add_argument("--recall-dir", default=str(DEFAULT_RECALL_DIR))
    parser.add_argument("--bucket")
    parser.add_argument("--case-glob")
    parser.add_argument("--include-staging", action="store_true")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--limit-cap", type=int, default=RESULT_LIMIT_CAP)
    parser.add_argument("--env-file", default=".env", help="Env file for retrieval primitives, relative to app-dir unless absolute.")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    app_dir = Path(args.app_dir)
    recall_dir = Path(args.recall_dir)
    cases = select_cases(recall_dir, args.bucket, args.case_glob, args.include_staging)
    if args.max_cases:
        cases = cases[: args.max_cases]
    if args.list:
        print(json.dumps([case.__dict__ | {"path": str(case.path)} for case in cases], indent=2, default=str))
        return

    run_dir = app_dir / ".powerpacks" / "runs" / "recall-parity"
    log_dir = app_dir / ".powerpacks" / "runs" / "recall-parity-logs"
    env = os.environ.copy()
    results: list[dict[str, Any]] = []
    for case in cases:
        print(f"running {case.relpath}...", flush=True)
        try:
            results.append(run_case(app_dir, case, env, run_dir, log_dir, args.limit_cap, args.env_file))
        except Exception as exc:
            results.append({
                "id": Path(case.relpath).with_suffix("").as_posix().replace("/", "__"),
                "source": case.relpath,
                "bucket": case.bucket,
                "query": case.query,
                "expected_id_count": len(case.expected_ids),
                "ignored_v4_count": len(case.ignored_v4_ids),
                "expected_count": case.expected_count,
                "status": "fail",
                "reason": str(exc),
            })
    write_report(results, app_dir, run_dir, log_dir)
    print(json.dumps({"report": str(REPORT_PATH), "results": results}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

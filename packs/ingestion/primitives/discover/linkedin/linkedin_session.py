"""LinkedIn as the signed-in user, from this Mac: mutual connections and Sales Navigator.

Both run requests inside the saved Chrome profile (session_browser.js), the one the
connections read signs in to, on a clone of it made for the run and deleted after: a
browser that cannot decrypt the profile's cookies deletes them, so the real profile is
never opened here. Every request counts against one daily budget (USAGE, DAY_CAP);
LinkedIn restricts accounts that fetch too much, and the count is the one guard.

`mutuals`: for up to 25 profile URLs, how many of the user's connections each person
shares, and the first ten of them, from the people search "1st degree, connections of X"
(one request per person, two when the profile id is not cached; nothing shows in the
person's profile views). First-degree connections (Connections.csv) are skipped. Answers
are kept CACHE_DAYS in MUTUALS.

`sales-nav companies|titles <query>`: Sales Navigator's typeahead ids for a filter.
`sales-nav search`: salesApiLeadSearch with the cloud's filters (relationship, current
company, current title, region, seniority, function, keywords), a page of up to 25 leads
in the cloud's lead shape (member_id, profile_id, total_mutual_count, mutual_member_ids...)
so packs/sales-nav reads them unchanged.

A port of network-search-api shared/sales_nav_client.py (URL builders, _parse_lead,
decode_member_id) and shared/sales_nav_filters.py (enum tables); the cloud sends the same
URLs through Unipile, this sends them through the user's own session.

Created: 2026-11-11
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, write_json
from packs.ingestion.primitives.setup.automations.oauth_browser import ensure_chromium
from packs.ingestion.primitives.setup.automations.shell import command_error, parse_json_fragment, run_streaming_command
from packs.ingestion.schemas.people_schema import extract_public_identifier

BROWSER_PROFILE = Path("~/.powerpacks/browser-profiles/linkedin")
BROWSER_SCRIPT = Path(__file__).with_name("session_browser.js")
FOLDER = Path(".powerpacks/network-import/discover/linkedin")
CONNECTIONS_CSV = FOLDER / "Connections.csv"
USAGE = FOLDER / "usage.json"
MUTUALS = FOLDER / "mutuals.json"
PROFILE_IDS = FOLDER / "profile-ids.json"
DAY_CAP = 100
BATCH_CAP = 25
CACHE_DAYS = 30
PAGE_SIZE = 25
TIMEOUT_SECONDS = 240

ORIGIN = "https://www.linkedin.com"
# Voyager: the search cluster decoration the site's own people search uses.
CLUSTERS = "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-175"
# Sales Navigator: shared/sales_nav_client.py DECORATION_ID.
LEAD_DECORATION = "com.linkedin.sales.deco.desktop.searchv2.LeadSearchResult-14"
DEGREES = {"1st": "F", "2nd": "S", "3rd": "O"}
DEGREE_LABELS = {"F": "1st degree connections", "S": "2nd degree connections", "O": "3rd degree connections"}
# shared/sales_nav_filters.py
SENIORITY_LEVELS = {"100": "In Training", "110": "Entry Level", "120": "Senior", "130": "Strategic",
                    "200": "Entry Level Manager", "210": "Experienced Manager", "220": "Director",
                    "300": "Vice President", "310": "CXO", "320": "Owner / Partner"}
FUNCTIONS = {"1": "Accounting", "2": "Administrative", "3": "Arts and Design", "4": "Business Development",
             "5": "Community and Social Services", "6": "Consulting", "7": "Education", "8": "Engineering",
             "9": "Entrepreneurship", "10": "Finance", "11": "Healthcare Services", "12": "Human Resources",
             "13": "Information Technology", "14": "Legal", "15": "Marketing", "16": "Media and Communication",
             "17": "Military and Protective Services", "18": "Operations", "19": "Product Management",
             "20": "Program and Project Management", "21": "Purchasing", "22": "Quality Assurance",
             "23": "Real Estate", "24": "Research", "25": "Sales", "26": "Support"}


class SlowDown(Exception):
    """LinkedIn answered 429 or 999: the batch stops and the day is over for this tool. The
    answers that did come back ride along, so a caller keeps them."""

    def __init__(self, message: str, answers: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.answers = answers


# ── the one budget ──────────────────────────────────────────────────────────────

def spent_today(usage_path: Path = USAGE) -> int:
    return int((read_json(usage_path, {}) or {}).get(date.today().isoformat(), 0))


def spend(count: int, usage_path: Path = USAGE) -> None:
    usage = read_json(usage_path, {}) or {}
    usage[date.today().isoformat()] = int(usage.get(date.today().isoformat(), 0)) + count
    write_json(usage_path, usage)


def budget(wanted: int, usage_path: Path = USAGE) -> int:
    """How many of `wanted` requests today's budget allows."""
    return max(0, min(wanted, DAY_CAP - spent_today(usage_path)))


# ── the browser ────────────────────────────────────────────────────────────────

def run_plan(plan: list[dict[str, str]], *, profile_dir: Path = BROWSER_PROFILE, usage_path: Path = USAGE,
             runner=run_streaming_command) -> list[dict[str, Any]]:
    """Fetch every request of `plan` in the signed-in session; the answers, in order. Spends the
    budget for the requests made, even when LinkedIn says slow down part way."""
    if not plan:
        return []
    profile = profile_dir.expanduser()
    if (profile / "SingletonLock").exists():
        raise RuntimeError("the LinkedIn browser profile is open (a connections read is running); try again when it finishes")
    deps = ensure_chromium()
    if deps["status"] != "ok":
        raise RuntimeError(deps["message"])
    with tempfile.TemporaryDirectory(prefix="linkedin-session-", dir=profile.parent) as scratch:
        clone = Path(scratch) / "profile"
        if sys.platform == "darwin":
            subprocess.run(["cp", "-Rc", str(profile), str(clone)], check=True)  # an APFS clone: instant, no extra disk
        else:
            shutil.copytree(profile, clone, symlinks=True)
        plan_path, out_path = Path(scratch) / "plan.json", Path(scratch) / "out.json"
        plan_path.write_text(json.dumps(plan))
        result = runner(["node", str(BROWSER_SCRIPT), "--profile-dir", str(clone), "--plan", str(plan_path), "--out", str(out_path)],
                        timeout=TIMEOUT_SECONDS, env={**os.environ, "NODE_PATH": deps["node_path"]})
        summary = parse_json_fragment(result.stdout) if result.stdout.strip() else {"status": "error", "message": command_error(result)}
        answers = json.loads(out_path.read_text()) if out_path.exists() else []
    spend(int(summary.get("requests", len(answers))), usage_path)
    if summary["status"] == "signed_out":
        raise RuntimeError("LinkedIn is signed out; run the LinkedIn connections read to sign in again")
    if summary["status"] == "error":
        raise RuntimeError(summary.get("message") or "the browser run failed")
    if summary["status"] == "slow_down":
        raise SlowDown(f"LinkedIn asked to slow down at {summary['stopped_at']}; {len(answers)} answers kept", answers)
    return answers


# ── mutual connections ─────────────────────────────────────────────────────────

def slug_of(url: str) -> str:
    return extract_public_identifier(url) or ""


def first_degree_slugs(csv_path: Path = CONNECTIONS_CSV) -> set[str]:
    """The user's connections, and the user (connections.json keeps the signed-in profile as owner_url)."""
    record = read_json(csv_path.with_name("connections.json"), {}) or {}
    slugs = {slug_of(str(record.get("owner_url") or ""))}
    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as handle:
            slugs |= {slug_of(row.get("URL") or "") for row in csv.DictReader(handle)}
    return slugs - {""}


def profile_request(slug: str) -> dict[str, str]:
    return {"name": f"profile {slug}", "url": f"{ORIGIN}/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={quote(slug, safe='')}"}


def mutuals_request(slug: str, profile_id: str) -> dict[str, str]:
    query = (f"(flagshipSearchIntent:SEARCH_SRP,queryParameters:(connectionOf:List({profile_id}),"
             f"network:List(F),resultType:List(PEOPLE)),includeFiltersInResponse:false)")
    return {"name": f"mutuals {slug}", "url": f"{ORIGIN}/voyager/api/search/dash/clusters?decorationId={CLUSTERS}"
                                              f"&origin=FACETED_SEARCH&q=all&query={query}&start=0&count=10"}


def profile_id_from(answer: dict[str, Any]) -> str | None:
    match = re.search(r"urn:li:fsd_profile:([A-Za-z0-9_-]+)", answer.get("text") or "")
    return match.group(1) if match else None


def mutuals_from(answer: dict[str, Any]) -> dict[str, Any]:
    """The count and the first page of people from a clusters answer."""
    payload = json.loads(answer["text"])
    people = []
    for item in payload.get("included") or []:
        if item.get("$type") != "com.linkedin.voyager.dash.search.EntityResultViewModel":
            continue
        people.append({"name": ((item.get("title") or {}).get("text") or "").strip(),
                       "headline": ((item.get("primarySubtitle") or {}).get("text") or "").strip(),
                       "location": ((item.get("secondarySubtitle") or {}).get("text") or "").strip(),
                       "url": (item.get("navigationUrl") or "").split("?")[0]})
    paging = (payload.get("data") or payload).get("paging") or {}
    return {"count": int(paging.get("total") or 0), "people": people}


def mutuals(urls: list[str], *, folder: Path = FOLDER, profile_dir: Path = BROWSER_PROFILE, runner=run_streaming_command) -> dict[str, Any]:
    """Mutual counts for up to BATCH_CAP profile URLs: cached answers first, then the budget."""
    cache_path, ids_path, usage_path = folder / MUTUALS.name, folder / PROFILE_IDS.name, folder / USAGE.name
    cache, ids = read_json(cache_path, {}) or {}, read_json(ids_path, {}) or {}
    fresh_after = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    connected = first_degree_slugs(folder / CONNECTIONS_CSV.name)
    rows: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for url in urls[:BATCH_CAP]:
        slug = slug_of(url)
        if not slug:
            rows[url] = {"url": url, "status": "no_slug"}
        elif slug in connected:
            rows[url] = {"url": url, "slug": slug, "status": "connected"}
        elif slug in cache and cache[slug]["at"] >= fresh_after:
            rows[url] = {"url": url, "slug": slug, "status": "cached", **cache[slug]}
        else:
            pending.append(slug)
            rows[url] = {"url": url, "slug": slug, "status": "pending"}
    need = sum(1 if slug in ids else 2 for slug in pending)
    allowed = budget(need, usage_path)
    plan, planned = [], []
    for slug in pending:
        cost = 1 if slug in ids else 2
        if sum(1 if s in ids else 2 for s in planned) + cost > allowed:
            break
        planned.append(slug)
        if slug not in ids:
            plan.append(profile_request(slug))
    def record(answer: dict[str, Any]) -> None:
        kind, slug = answer["name"].split(" ", 1)
        if kind == "profile" and profile_id_from(answer):
            ids[slug] = profile_id_from(answer)
        elif kind == "mutuals" and answer["status"] == 200:
            cache[slug] = {**mutuals_from(answer), "at": now_iso()}
        else:
            cache[slug] = {"count": None, "people": [], "at": now_iso(), "error": f"{kind} HTTP {answer['status']}"}

    outcome = "completed"
    try:
        # Ids first, so each mutuals request can be built; two short batches beat one blind one.
        for answer in run_plan(plan, profile_dir=profile_dir, usage_path=usage_path, runner=runner):
            record(answer)
        plan = [mutuals_request(slug, ids[slug]) for slug in planned if slug in ids]
        for answer in run_plan(plan, profile_dir=profile_dir, usage_path=usage_path, runner=runner):
            record(answer)
    except SlowDown as stop:
        for answer in stop.answers:
            record(answer)
        outcome = f"slow_down: {stop}"
    write_json(ids_path, ids)
    write_json(cache_path, cache)
    for row in rows.values():
        slug = row.get("slug")
        if row["status"] == "pending" and slug in cache:
            row.update(status="looked_up", **cache[slug])
        elif row["status"] == "pending":
            row["status"] = "over_budget" if slug not in planned else "not_answered"
    return {"primitive": "linkedin_session", "command": "mutuals", "status": outcome, "rows": list(rows.values()),
            "requests_today": spent_today(usage_path), "day_cap": DAY_CAP}


# ── Sales Navigator (port of shared/sales_nav_client.py) ───────────────────────

def _q(value: Any) -> str:
    """URL-encode a RestLi scalar with no safe characters; commas, colons, parentheses and
    slashes all mean something in the query."""
    return quote(str(value), safe="")


def relationship_filter(degree: str) -> str:
    return f"(type:RELATIONSHIP,values:List((id:{degree},text:{_q(DEGREE_LABELS[degree])},selectionType:INCLUDED)))"


def company_filter(companies: list[tuple[int, str]]) -> str:
    """One CURRENT_COMPANY filter for every company; separate filters per company is a 400."""
    values = ",".join(f"(id:{_q(f'urn:li:organization:{company_id}')},text:{_q(name or str(company_id))},selectionType:INCLUDED,parent:(id:0))"
                      for company_id, name in companies)
    return f"(type:CURRENT_COMPANY,values:List({values}))"


def title_filter(titles: list[tuple[int, str]]) -> str:
    values = ",".join(f"(id:{title_id},text:{_q(name or str(title_id))},selectionType:INCLUDED)" for title_id, name in titles)
    return f"(type:CURRENT_TITLE,values:List({values}))"


def region_filter(regions: list[tuple[str, str]]) -> str:
    values = ",".join(f"(id:{_q(geo_id)},text:{_q(name or geo_id)},selectionType:INCLUDED)" for geo_id, name in regions)
    return f"(type:REGION,values:List({values}))"


def enum_filter(kind: str, table: dict[str, str], ids: list[str]) -> str:
    unknown = [item for item in ids if item not in table]
    if unknown:
        raise ValueError(f"unknown {kind.lower()} ids {unknown}; one of {sorted(table)}")
    values = ",".join(f"(id:{item},text:{_q(table[item])},selectionType:INCLUDED)" for item in ids)
    return f"(type:{kind},values:List({values}))"


def lead_search_url(filters: list[str], *, start: int = 0, count: int = PAGE_SIZE, keywords: str | None = None) -> str:
    keyword_part = f",keywords:{_q(keywords)}" if keywords else ""
    return (f"{ORIGIN}/sales-api/salesApiLeadSearch?q=searchQuery&query=(filters:List({','.join(filters)}){keyword_part})"
            f"&start={max(0, start)}&count={max(1, min(count, PAGE_SIZE))}&decorationId={LEAD_DECORATION}")


def typeahead_url(kind: str, query: str) -> str:
    return f"{ORIGIN}/sales-api/salesApiFacetTypeahead?q=query&start=0&count=10&type={kind}&query={quote(query)}"


def decode_member_id(profile_id: str) -> int:
    """The member id inside a Sales Nav base64 profile id: 2 type bytes, 6 id bytes, the rest hash."""
    raw = base64.urlsafe_b64decode(profile_id + "=" * (-len(profile_id) % 4))
    if len(raw) < 8:
        raise ValueError(f"profile id too short: {profile_id}")
    return int.from_bytes(raw[2:8], "big")


def profile_id_in(urn: str) -> str | None:
    """'urn:li:fs_salesProfile:(ACwAAA...,authType,authToken)' → 'ACwAAA...'."""
    if "(" not in urn:
        return None
    return urn.split("(", 1)[1].rstrip(")").split(",")[0].strip() or None


def member_id_in(urn: str) -> int | None:
    profile_id = profile_id_in(urn)
    try:
        return decode_member_id(profile_id) if profile_id else None
    except ValueError:
        return None


def parse_lead(element: dict[str, Any]) -> dict[str, Any]:
    """One salesApiLeadSearch element as the cloud's SalesNavLead dict."""
    positions = element.get("currentPositions") or []
    first = positions[0] if positions else {}
    profile_id = profile_id_in(element.get("entityUrn") or "")
    member_id = member_id_in(element.get("entityUrn") or "") or 0
    object_urn = element.get("objectUrn") or ""
    if "member:" in object_urn and object_urn.rsplit(":", 1)[1].isdigit():
        member_id = int(object_urn.rsplit(":", 1)[1])
    picture = element.get("profilePictureDisplayImage") or {}
    artifacts = picture.get("artifacts") or []
    picture_url = (picture.get("rootUrl", "") + max(artifacts, key=lambda a: a.get("width", 0)).get("fileIdentifyingUrlPathSegment", "")) if artifacts else None
    mutual_ids, total_mutual_count, intro = [], 0, None
    for badge in element.get("spotlightBadges") or []:
        if badge.get("id") != "SECOND_DEGREE_CONNECTION":
            continue
        match = re.search(r"(\d[\d,]*)", badge.get("displayValue") or "")
        if match:
            total_mutual_count = int(match.group(1).replace(",", ""))
        intro = ((badge.get("popup") or {}).get("message") or {}).get("text")
        for assoc in badge.get("associatedEntityUrnsUnions") or []:
            mutual = member_id_in(assoc.get("profileUrn") or "")
            if mutual:
                mutual_ids.append(mutual)
    return {"member_id": member_id, "profile_id": profile_id, "first_name": element.get("firstName", ""),
            "last_name": element.get("lastName", ""), "title": first.get("title"), "company_name": first.get("companyName"),
            "company_urn": (first.get("companyUrnResolutionResult") or {}).get("entityUrn"), "location": element.get("geoRegion", ""),
            "degree": element.get("degree"), "total_mutual_count": total_mutual_count or len(mutual_ids),
            "mutual_member_ids": mutual_ids, "mutual_intro_text": intro, "profile_picture_url": picture_url}


def parse_typeahead(kind: str, answer: dict[str, Any]) -> list[dict[str, Any]]:
    payload = json.loads(answer["text"])
    elements = payload.get("elements") or []
    # COMPANY_WITH_LIST nests its matches under a "Companies" group; TITLE answers are flat.
    matches = [child for element in elements for child in (element.get("children") or [])] if kind == "COMPANY_WITH_LIST" else elements
    rows = []
    for match in matches:
        raw_id = str(match.get("id") or match.get("entityUrn") or "")
        rows.append({"id": int(raw_id.rsplit(":", 1)[-1]) if raw_id.rsplit(":", 1)[-1].isdigit() else raw_id,
                     "name": match.get("displayValue") or match.get("name") or "",
                     "headcount": match.get("employeeCountRange"), "location": (match.get("address") or {}).get("city")})
    return rows


def search_url_for(args: argparse.Namespace) -> tuple[str, list[str]]:
    """The salesApiLeadSearch URL for one `sales-nav search` argument set, and its filters."""
    filters = [relationship_filter(DEGREES[args.degree])]
    if args.company_id:
        filters.append(company_filter(list(zip(args.company_id, (args.company_name or []) + [""] * len(args.company_id)))))
    if args.title_id:
        filters.append(title_filter(list(zip(args.title_id, (args.title_name or []) + [""] * len(args.title_id)))))
    if args.region_id:
        filters.append(region_filter(list(zip(args.region_id, (args.region_name or []) + [""] * len(args.region_id)))))
    if args.seniority:
        filters.append(enum_filter("SENIORITY_LEVEL", SENIORITY_LEVELS, args.seniority))
    if args.function:
        filters.append(enum_filter("FUNCTION", FUNCTIONS, args.function))
    return lead_search_url(filters, start=args.start, count=args.count, keywords=args.keywords), filters


def sales_nav_searches(searches: list[argparse.Namespace], *, folder: Path = FOLDER, profile_dir: Path = BROWSER_PROFILE,
                       runner=run_streaming_command) -> list[dict[str, Any]]:
    """Several lead searches in one browser run: the home page once, then one request each."""
    usage_path = folder / USAGE.name
    if budget(len(searches) + 1, usage_path) < len(searches) + 1:
        raise RuntimeError(f"today's LinkedIn budget of {DAY_CAP} requests is spent")
    urls = [search_url_for(args) for args in searches]
    answers = run_plan([{"name": f"lead search {i}", "url": url} for i, (url, _) in enumerate(urls)],
                       profile_dir=profile_dir, usage_path=usage_path, runner=runner)
    results = []
    for args, (url, filters), answer in zip(searches, urls, answers):
        if answer["status"] != 200:
            raise RuntimeError(f"Sales Navigator search answered HTTP {answer['status']}: {answer['text'][:200]}")
        payload = json.loads(answer["text"])
        leads = [parse_lead(element) for element in payload.get("elements") or []]
        results.append({"primitive": "linkedin_session", "command": "sales-nav search",
                        "total": int((payload.get("paging") or {}).get("total") or 0),
                        "start": args.start, "count": len(leads), "leads": leads, "filters": filters, "keywords": args.keywords,
                        "requests_today": spent_today(usage_path), "day_cap": DAY_CAP})
    return results


def sales_nav(action: str, args: argparse.Namespace, *, folder: Path = FOLDER, profile_dir: Path = BROWSER_PROFILE,
              runner=run_streaming_command) -> dict[str, Any]:
    if action == "search":
        return sales_nav_searches([args], folder=folder, profile_dir=profile_dir, runner=runner)[0]
    usage_path = folder / USAGE.name
    # Every Sales Navigator batch opens its home page once (session_browser.js), so it costs two.
    if budget(2, usage_path) < 2:
        raise RuntimeError(f"today's LinkedIn budget of {DAY_CAP} requests is spent")
    kind = "COMPANY_WITH_LIST" if action == "companies" else "TITLE"
    answer, = run_plan([{"name": f"typeahead {action}", "url": typeahead_url(kind, args.query)}], profile_dir=profile_dir, usage_path=usage_path, runner=runner)
    if answer["status"] != 200:
        raise RuntimeError(f"Sales Navigator typeahead answered HTTP {answer['status']}")
    return {"primitive": "linkedin_session", "command": f"sales-nav {action}", "query": args.query,
            "matches": parse_typeahead(kind, answer), "requests_today": spent_today(usage_path), "day_cap": DAY_CAP}


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile-dir", type=Path, default=BROWSER_PROFILE)
    parser.add_argument("--folder", type=Path, default=FOLDER, help="the data folder (Connections.csv, usage.json, mutuals.json)")
    commands = parser.add_subparsers(dest="command", required=True)
    m = commands.add_parser("mutuals", help=f"mutual connections for up to {BATCH_CAP} LinkedIn profile URLs")
    m.add_argument("urls", nargs="+")
    s = commands.add_parser("sales-nav", help="Sales Navigator: companies, titles, search")
    actions = s.add_subparsers(dest="action", required=True)
    for action in ("companies", "titles"):
        actions.add_parser(action).add_argument("query")
    search = actions.add_parser("search")
    search.add_argument("--degree", choices=sorted(DEGREES), default="2nd")
    search.add_argument("--company-id", type=int, action="append", help="LinkedIn organization id (sales-nav companies)")
    search.add_argument("--company-name", action="append")
    search.add_argument("--title-id", type=int, action="append", help="Sales Navigator title id (sales-nav titles)")
    search.add_argument("--title-name", action="append")
    search.add_argument("--region-id", action="append", help="LinkedIn geo id")
    search.add_argument("--region-name", action="append")
    search.add_argument("--seniority", action="append", help=f"one of {', '.join(f'{k}={v}' for k, v in SENIORITY_LEVELS.items())}")
    search.add_argument("--function", action="append", help="Sales Navigator function id, 1-26 (8=Engineering, 25=Sales)")
    search.add_argument("--keywords")
    search.add_argument("--start", type=int, default=0)
    search.add_argument("--count", type=int, default=PAGE_SIZE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "mutuals":
            payload = mutuals(args.urls, folder=args.folder, profile_dir=args.profile_dir)
        else:
            payload = sales_nav(args.action, args, folder=args.folder, profile_dir=args.profile_dir)
    except (RuntimeError, ValueError, SlowDown) as error:
        emit({"primitive": "linkedin_session", "status": "failed", "message": str(error)})
        return 1
    emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

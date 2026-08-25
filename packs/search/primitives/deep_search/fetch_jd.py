"""Fetch a job-posting URL -> clean JD text. The URL->JD front-end for `$search` deep mode.

`$search` deep mode takes a `--jd-file`; this primitive closes the input-shape gap so deep
mode accepts a job-posting URL too — everything downstream (plan/traits, seniority gate, judge,
core-gate, export) is unchanged.

No LLM, no spend. Stdlib only (urllib + html.parser) — matches the repo's existing urllib fetch
idiom (e.g. enrich_people.py). Fetches the page, strips HTML to readable text, and writes:

  <out>              clean JD text (default: the job description we feed deep mode)
  <source-json>      URL, title, and hiring-company metadata extracted from the page
  <raw-html>         raw HTML (optional, --raw-html, for debug)

Fetch failure (HTTP/network) is fail-loud (exit 1). A page that fetches but yields little text
(JS-rendered careers pages) exits 0 with status "thin" so the caller can decide to paste the JD
instead. Prints a small JSON summary either way.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# Blocks whose text we drop entirely (nav chrome, scripts, styling, SVG icons).
_DROP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "template"}
# Block-level tags: emit a newline boundary so paragraphs/list items don't run together.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "header", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr", "dd", "dt", "blockquote", "pre",
}
# A page that renders to less than this many characters is almost certainly JS-rendered.
_THIN_CHARS = 400
JOB_BOARD_HOSTS = {
    "jobs.ashbyhq.com", "jobs.lever.co", "boards.greenhouse.io",
    "job-boards.greenhouse.io",
}
_NON_COMPANY_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "github.com",
    *JOB_BOARD_HOSTS,
}


class _TextExtractor(HTMLParser):
    """Collapse HTML to readable text; capture <title>; skip chrome/script blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._drop_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._drop_depth:
            self._drop_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._drop_depth:
            return
        if data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse intra-line whitespace, then squeeze blank-line runs.
        lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.splitlines()]
        out: list[str] = []
        blanks = 0
        for ln in lines:
            if ln:
                blanks = 0
                out.append(ln)
            else:
                blanks += 1
                if blanks <= 1:
                    out.append("")
        return "\n".join(out).strip()


class _CompanyExtractor(HTMLParser):
    """Capture structured hiring-organization data and external page links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._json_ld = False
        self._json_parts: list[str] = []
        self.json_documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(str(values["href"]))
        if tag == "script" and str(values.get("type") or "").casefold() == "application/ld+json":
            self._json_ld = True
            self._json_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json_ld:
            self.json_documents.append("".join(self._json_parts))
            self._json_ld = False

    def handle_data(self, data: str) -> None:
        if self._json_ld:
            self._json_parts.append(data)


def _company_site(url: str, source_url: str) -> str | None:
    absolute = urllib.parse.urljoin(source_url, url)
    parsed = urllib.parse.urlparse(absolute)
    source_host = str(urllib.parse.urlparse(source_url).hostname or "").casefold()
    host = str(parsed.hostname or "").casefold().removeprefix("www.")
    if parsed.scheme not in {"http", "https"} or not host or host == source_host.removeprefix("www."):
        return None
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in _NON_COMPANY_HOSTS):
        return None
    return absolute


def extract_company_metadata(raw_html: str, source_url: str) -> dict[str, object]:
    parser = _CompanyExtractor()
    parser.feed(raw_html)
    company_name = ""
    structured_urls: list[str] = []
    for raw in parser.json_documents:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = document if isinstance(document, list) else [document]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nodes.extend(item for item in (node.get("@graph") or []) if isinstance(item, dict))
            organization = node.get("hiringOrganization")
            if not isinstance(organization, dict):
                continue
            company_name = company_name or str(organization.get("name") or "").strip()
            values = organization.get("sameAs") or organization.get("url") or []
            for value in values if isinstance(values, list) else [values]:
                if site := _company_site(str(value), source_url):
                    structured_urls.append(site)
    link_urls = [site for value in parser.links if (site := _company_site(value, source_url))]
    parsed_source = urllib.parse.urlparse(source_url)
    source_host = str(parsed_source.hostname or "").casefold().removeprefix("www.")
    source_site = (f"{parsed_source.scheme}://{parsed_source.netloc}"
                   if parsed_source.scheme in {"http", "https"} and
                   source_host not in JOB_BOARD_HOSTS else None)
    candidates = list(dict.fromkeys([*([source_site] if source_site else []),
                                     *structured_urls, *link_urls]))
    return {
        "company_name": company_name or None,
        "company_website_url": candidates[0] if candidates else None,
        "company_website_urls": candidates,
    }


def extract_linkedin_company_slug(raw_html: str, source_url: str) -> str:
    parser = _CompanyExtractor()
    parser.feed(raw_html)
    for value in parser.links:
        parsed = urllib.parse.urlparse(urllib.parse.urljoin(source_url, value))
        host = str(parsed.hostname or "").casefold().removeprefix("www.")
        parts = [part for part in parsed.path.split("/") if part]
        if (host == "linkedin.com" or host.endswith(".linkedin.com")) and len(parts) >= 2:
            if parts[0].casefold() == "company":
                return parts[1].casefold()
    return ""


def fetch(url: str, timeout: int = 30) -> tuple[str, str]:
    """Return (raw_html, final_url). Fail-loud on HTTP/network error."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        final_url = resp.geturl()
    return raw, final_url


def extract(raw_html: str) -> tuple[str, str]:
    """Return (clean_text, title)."""
    parser = _TextExtractor()
    parser.feed(raw_html)
    return parser.text(), re.sub(r"\s+", " ", parser.title).strip()


_ASHBY_HOST = "jobs.ashbyhq.com"
_ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{org}"
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def fetch_ashby(url: str, timeout: int = 30) -> tuple[str, str] | None:
    """Ashby job pages are fully JS-rendered (the HTML extracts to 0 chars), but the
    board exposes a public posting API with descriptionHtml. Return (jd_text, title),
    or None when the URL isn't a resolvable Ashby posting so the caller falls back to
    the generic HTML fetch."""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != _ASHBY_HOST:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    job_id_match = _UUID_RE.search(parsed.path)
    if not parts or not job_id_match:
        return None
    org, job_id = parts[0], job_id_match.group(0).lower()
    req = urllib.request.Request(
        _ASHBY_API.format(org=urllib.parse.quote(org)),
        headers={
            "Accept": "application/json",
            # The API 403s urllib's default UA; the browser UA (same as fetch()) passes.
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            board = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    for job in board.get("jobs") or []:
        if str(job.get("id", "")).lower() == job_id:
            text, _ = extract(str(job.get("descriptionHtml") or ""))
            title = str(job.get("title") or "").strip()
            if text:
                return (f"{title}\n\n{text}" if title else text), title
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch a job-posting URL -> clean JD text (URL->JD front-end for $search deep mode).")
    ap.add_argument("--url", required=True, help="Job-posting URL to fetch")
    ap.add_argument("--out", required=True, help="Where to write the clean JD text (feeds deep mode --jd-file)")
    ap.add_argument("--source-json", default=None, help="Where to write source URL metadata (default: <out dir>/source.json)")
    ap.add_argument("--raw-html", default=None, help="Optional: also write the raw HTML here (debug)")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    source_json = Path(args.source_json) if args.source_json else out.parent / "source.json"

    ashby = fetch_ashby(args.url, timeout=args.timeout)
    try:
        raw_html, final_url = fetch(args.url, timeout=args.timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        if ashby is None:
            print(json.dumps({"primitive": "fetch_jd", "status": "failed", "url": args.url, "error": str(exc)}, indent=2))
            raise SystemExit(1)
        raw_html, final_url = "", args.url
    company = extract_company_metadata(raw_html, final_url)
    if ashby is not None:
        (text, title), via = ashby, "ashby_posting_api"
    else:
        text, title = extract(raw_html)
        via = "html"
    fetched_at = datetime.now(timezone.utc).isoformat()

    out.write_text(text + "\n", encoding="utf-8")
    source_json.write_text(json.dumps({
        "requested_url": args.url,
        "source_url": final_url,
        "source_title": title,
        "fetched_at": fetched_at,
        "via": via,
        **company,
    }, indent=2) + "\n", encoding="utf-8")
    if args.raw_html and raw_html:
        Path(args.raw_html).write_text(raw_html, encoding="utf-8")

    status = "thin" if len(text) < _THIN_CHARS else "ok"
    summary = {
        "primitive": "fetch_jd",
        "status": status,
        "url": final_url,
        "title": title,
        "via": via,
        "chars": len(text),
        "out": str(out),
        "source_json": str(source_json),
    }
    if status == "thin":
        summary["warning"] = f"extracted only {len(text)} chars — likely JS-rendered; paste the JD text instead"
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

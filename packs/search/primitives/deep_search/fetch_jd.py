"""Fetch a job-posting URL -> clean JD text. The URL->JD front-end for `$search` deep mode.

`$search` deep mode takes a `--jd-file`; this primitive closes the input-shape gap so deep
mode accepts a job-posting URL too — everything downstream (plan/traits, seniority gate, judge,
core-gate, export) is unchanged.

No LLM, no spend. Stdlib only (urllib + html.parser) — matches the repo's existing urllib fetch
idiom (e.g. enrich_people.py). Fetches the page, strips HTML to readable text, and writes:

  <out>              clean JD text (default: the job description we feed deep mode)
  <source-json>      {source_url, source_title, fetched_at}   (the source.json shape)
  <raw-html>         raw HTML (optional, --raw-html, for debug)

Fetch failure (HTTP/network) is fail-loud (exit 1). A page that fetches but yields little text
(JS-rendered careers pages) exits 0 with status "thin" so the caller can decide to paste the JD
instead. Prints a small JSON summary either way.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch a job-posting URL -> clean JD text (URL->JD front-end for $search deep mode).")
    ap.add_argument("--url", required=True, help="Job-posting URL to fetch")
    ap.add_argument("--out", required=True, help="Where to write the clean JD text (feeds deep mode --jd-file)")
    ap.add_argument("--source-json", default=None, help="Where to write {source_url, source_title, fetched_at} (default: <out dir>/source.json)")
    ap.add_argument("--raw-html", default=None, help="Optional: also write the raw HTML here (debug)")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    source_json = Path(args.source_json) if args.source_json else out.parent / "source.json"

    try:
        raw_html, final_url = fetch(args.url, timeout=args.timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        print(json.dumps({"primitive": "fetch_jd", "status": "failed", "url": args.url, "error": str(exc)}, indent=2))
        raise SystemExit(1)

    text, title = extract(raw_html)
    fetched_at = datetime.now(timezone.utc).isoformat()

    out.write_text(text + "\n", encoding="utf-8")
    source_json.write_text(json.dumps({"source_url": final_url, "source_title": title, "fetched_at": fetched_at}, indent=2) + "\n", encoding="utf-8")
    if args.raw_html:
        Path(args.raw_html).write_text(raw_html, encoding="utf-8")

    status = "thin" if len(text) < _THIN_CHARS else "ok"
    summary = {
        "primitive": "fetch_jd",
        "status": status,
        "url": final_url,
        "title": title,
        "chars": len(text),
        "out": str(out),
        "source_json": str(source_json),
    }
    if status == "thin":
        summary["warning"] = f"extracted only {len(text)} chars — likely JS-rendered; paste the JD text instead"
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

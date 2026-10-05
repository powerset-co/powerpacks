"""A dossier's markdown as the HTML fragment the Review and People pages inject.

Changelog:
  2026-10-01: moved out of rendering.py, which went with the Jinja review page.
  2026-10-01: a review card's dossier leaves out the Network worth line too, so the flag
    is `for_review_card` (was `skip_name_and_contact`).
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"(</?)h([1-6])>")
_HIDDEN_SECTIONS_RE = re.compile(
    r"^## (?:Confirmed children \(merged\)|Identifiers)\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL
)
# The composer's `**Network worth:** <decision> — <reason>` paragraph. The stored
# dossier keeps it: paid judges read that text.
_WORTH_LINE_RE = re.compile(r"^\*\*Network worth:\*\*[^\n]*$", re.MULTILINE)

# markdown-it-py is already a direct dependency (dossier validation). The
# fragment sits under a card's own <h2>, so dossier headings render two
# levels deeper: <h3>..<h6>.
_MD = MarkdownIt("commonmark").enable("table")
_HEADING_SHIFT = 2


def markdown_to_html(markdown: str, *, for_review_card: bool = False) -> str:
    """Render a dossier body — the full markdown vocabulary, headings clamped.

    YAML frontmatter is file metadata, never UI content; HTML comments are
    the composer's internal markers (e.g. parent-link) and stay stripped.
    ``for_review_card`` renders the body a review card or an opened pile row
    shows under the person: without the leading ``# Name`` heading and the
    ``## Contact`` section (the card shows both above it) and without the
    ``**Network worth:**`` line.
    """
    body = _FRONTMATTER_RE.sub("", _COMMENT_RE.sub("", markdown), count=1)
    body = _HIDDEN_SECTIONS_RE.sub("", body)
    if for_review_card:
        body = re.sub(r"\A\s*# [^\n]*\n?", "", body, count=1)
        body = re.sub(r"\n?## Contact\n(?:(?!#)[^\n]*\n?)*", "", body, count=1)
        body = _WORTH_LINE_RE.sub("", body)
    html = _MD.render(body)
    return _HEADING_RE.sub(
        lambda m: f"{m.group(1)}h{min(6, int(m.group(2)) + _HEADING_SHIFT)}>",
        html,
    )

"""The first look's questions, read from the search index. Its own module because the page server
imports the controller before the project environment exists (tests/test_bootstrap.py: standard library
alone), so duckdb is imported here and the controller reaches for this at call time."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import duckdb

SEARCH_DB = ".powerpacks/search-index/local-search.duckdb"
_PERSON = ("p.public_identifier, p.full_name, coalesce(p.current_title, ''), coalesce(p.current_company, ''), "
           "coalesce(p.headline, ''), coalesce(p.profile_picture_url, '')")
# The three questions the first look offers, each over one column, so each always answers with
# real names. Role questions match the primary title/company shown on each result card.
_QUESTIONS = {
    "company": ("Who do I know at {}?",
                "select current_company, count(*) from local_person_profiles "
                "where current_company <> '' group by 1 order by 2 desc, 1 limit 1",
                "select " + _PERSON + " from local_person_profiles p "
                "where p.current_company = ? order by p.full_name"),
    "city": ("Who do I know in {}?",
             "select city, count(*) from local_person_profiles where city <> '' group by 1 order by 2 desc, 1 limit 1",
             "select " + _PERSON + " from local_person_profiles p where p.city = ? order by p.full_name"),
    "founders": ("Which of my connections are founders?",
                 "select 'founder', count(*) from local_person_profiles "
                 "where lower(current_title) like '%founder%'",
                 "select " + _PERSON + " from local_person_profiles p "
                 "where lower(p.current_title) like ? order by p.full_name"),
}


def questions(root: Path) -> list[dict]:
    """The first look's questions, minted from the index: each names what the index holds most of,
    and is offered only when it has at least two people to show. None before the index exists."""
    db = root / SEARCH_DB
    if not db.is_file():
        return []
    found = []
    with duckdb.connect(str(db), read_only=True) as conn:
        for key, (text, mint, _) in _QUESTIONS.items():
            row = conn.execute(mint).fetchone()
            if row and row[1] >= 2:
                found.append({"key": key, "text": text.format(row[0]), "count": row[1]})
    return found


def ask(root: Path, key: str, limit: int, avatar: Callable[[str, str], str]) -> dict:
    """One question's answer from the index: the first `limit` people and how many there are."""
    text, mint, answer = _QUESTIONS[key]
    with duckdb.connect(str(root / SEARCH_DB), read_only=True) as conn:
        value, total = conn.execute(mint).fetchone()
        rows = conn.execute(answer, [f"%{value}%" if key == "founders" else value]).fetchall()
    people = [{"public_identifier": row[0], "name": row[1], "title": row[2], "company": row[3],
               "headline": row[4], "avatar_url": avatar(row[0], row[5])} for row in rows[:limit]]
    return {"question": text.format(value), "people": people, "total": total}

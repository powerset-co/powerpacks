"""Written-name comparison for the importers: can two written names be one person's?

The importers (Gmail, messages, the merge of imported people) ask whether every name written for
one address or number is compatible with every other, before they keep a row as one contact.
This is v1 deep-context's `merge_candidates/candidate_pairs.py` name logic, moved here whole when
that package was deleted, so the importers behave exactly as before. deep-context v2 has its own
`names.py` for the dedupe gate; the two are not interchangeable.

Created: 2026-10-07 (moved from deep_context/merge_candidates/candidate_pairs.py)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from functools import lru_cache
from itertools import combinations

from nameparser import Lexicon, Parser

# How alike two first names or two last names must spell to be one name's variants.
# A recall gate, not an acceptance threshold: changing it changes which pairs reach the judge.
GATE_NAME_SIM = 0.85
SAME_FULL_NAME = "same full name"
SAME_FIRST_AND_LAST_NAME = "same first and last name, middle names do not differ"
# A father and a son: a name carrying one of these is not the same name as one without it.
GENERATION_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
TITLES = frozenset({"dr", "mr", "mrs", "ms", "prof"})
PROFESSIONAL_CREDENTIALS = frozenset({
    "cfa", "cia", "cams", "cpa", "macc", "phd", "md", "mba", "caia", "cfe",
    "fca", "pe", "csp", "shrm-cp", "ma", "msed", "mphiled", "nacddc",
})
_NAME_PARSER = Parser(lexicon=replace(
    Lexicon.default(),
    titles=TITLES,
    given_name_titles=frozenset(),
    suffix_acronyms=PROFESSIONAL_CREDENTIALS,
    suffix_words=GENERATION_SUFFIXES | {"esq", "esquire"},
    suffix_acronyms_ambiguous=Lexicon.default().suffix_acronyms_ambiguous & PROFESSIONAL_CREDENTIALS,
    honorific_tails=frozenset(),
))
_OUTER_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}


def jaro(first: str, second: str) -> float:
    """Return the Jaro similarity from the Winkler Census specification."""
    if first == second:
        return 1.0
    if not first or not second:
        return 0.0
    match_distance = max(len(first), len(second)) // 2 - 1
    first_matches = [False] * len(first)
    second_matches = [False] * len(second)
    matches = 0
    for index, character in enumerate(first):
        lower = max(0, index - match_distance)
        upper = min(index + match_distance + 1, len(second))
        for other in range(lower, upper):
            if not second_matches[other] and second[other] == character:
                first_matches[index] = second_matches[other] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = other = 0
    for index, matched in enumerate(first_matches):
        if matched:
            while not second_matches[other]:
                other += 1
            if first[index] != second[other]:
                transpositions += 1
            other += 1
    transpositions //= 2
    return (
        matches / len(first)
        + matches / len(second)
        + (matches - transpositions) / matches
    ) / 3


def jaro_winkler(first: str, second: str, prefix_weight: float = 0.1) -> float:
    """Return Jaro-Winkler similarity with the standard four-character prefix cap."""
    base = jaro(first, second)
    prefix = 0
    for left, right in zip(first, second):
        if left == right and prefix < 4:
            prefix += 1
        else:
            break
    return base + prefix * prefix_weight * (1 - base)


@lru_cache(None)
def name_words(name_key: str) -> tuple[str, ...]:
    """The words of a name, given name first and titles left out: "bravo, dr jordan" reads as jordan bravo.

    An email address saved as the name has no words. An apostrophe does not
    split a word: o'bravo is one word, not an initial and a name.
    """
    if "@" in name_key:
        return ()
    name = unicodedata.normalize("NFC", name_key).strip()
    if len(name) >= 2 and _OUTER_QUOTES.get(name[0]) == name[-1]:
        name = name[1:-1]
    person = _NAME_PARSER.parse(name)
    ordered = " ".join((person.given, person.middle, person.family))
    ordered = re.sub(r"['’]", "", unicodedata.normalize("NFC", ordered))
    suffix = re.sub(r"['’]", "", unicodedata.normalize("NFC", person.suffix))
    return tuple(re.findall(r"[^\W\d_]+", ordered.casefold())) + tuple(
        word for word in re.findall(r"[^\W\d_]+", suffix.casefold()) if word in GENERATION_SUFFIXES
    )


def _is_full_name(words: tuple[str, ...]) -> bool:
    """A first and a last word that are both spelled out, not initials."""
    return len(words) > 1 and len(words[0]) > 1 and len(words[-1]) > 1


def _middle_names_agree(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Missing on one side, or word for word equal or an initial of the other."""
    if not first or not second:
        return True
    if len(first) != len(second):
        return False
    return all(
        left == right or (1 in (len(left), len(right)) and left[0] == right[0])
        for left, right in zip(first, second)
    )


def same_name_reason(first: tuple[str, ...], second: tuple[str, ...]) -> str | None:
    """Why two names' words are one contact's name, or None when the names do not settle it."""
    if not _is_full_name(first) or not _is_full_name(second):
        return None
    if GENERATION_SUFFIXES & set(first) != GENERATION_SUFFIXES & set(second):
        return None
    if sorted(first) == sorted(second):
        return SAME_FULL_NAME
    if (first[0], first[-1]) != (second[0], second[-1]):
        return None
    if not _middle_names_agree(first[1:-1], second[1:-1]):
        return None
    return SAME_FIRST_AND_LAST_NAME


def _word_forms_match(first: str, second: str) -> bool:
    """One word equals, begins or nearly spells the other: jordan/j, ben/benjamin, jon/john."""
    return first.startswith(second) or second.startswith(first) or jaro_winkler(first, second) >= GATE_NAME_SIM


def names_can_match(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Can one name be a form of the other? Decides which pairs are worth a judgment, not the judgment."""
    if not first or not second:
        return False
    if same_name_reason(first, second) or first == second:
        return True
    short, long = sorted((first, second), key=len)
    if len(short) == 1:
        return short[0] in (long[0], long[-1])
    return _word_forms_match(first[0], second[0]) and _word_forms_match(first[-1], second[-1])


def source_names_can_match(names: tuple[str, ...]) -> bool:
    """Every original name is present and compatible with every other member."""
    words = [name_words(name) for name in names]
    return bool(words) and all(words) and all(names_can_match(a, b) for a, b in combinations(words, 2))

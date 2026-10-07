"""The name functions: how a written name is read, and when two names can be one person's.

There are two name functions with two jobs. `same_person_name` is the precision match: it ties a
family to a LinkedIn connection (the pre-match in worth and enrich), so it must almost never be
wrong. `names_can_match` is the recall gate for the Sol judge in dedupe: it only decides which
pairs are worth a judgment, so it lets spelling variants and initials through and leaves the
identity decision to Sol. They are kept apart on purpose.

`same_person_name`: a name is read once into a `Name`: casefolded, NFC, titles and credentials
out, given name first. A parenthetical between two words is an alternate given name ("Wei (David)
Zhang" reads as wei zhang and as david zhang); any other parenthetical, such as a trailing "(PhD)"
or "(she/her)", is dropped. Two names are one person's when the surnames are identical, a given
name of one equals or is a spelled prefix (two letters or more) of a given name of the other, the
middle names agree when both have them, and the generation (Jr, Sr, II, III, IV) is identical. No
nickname table: Bob is not Robert. No accent folding: José is not Jose.

`names_can_match`: a name is read into its words, given name first. Two names can match when they
are the same full name in any word order, or their first words are equal, a prefix of the other,
or spelled alike (Jaro-Winkler 0.85 or more) and their last words are equal, a one-letter initial of
the other, or spelled alike. A one-word name is never paired by name.

Created: 2026-10-06
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace

from nameparser import Lexicon, Parser

# A father and a son: a name carrying one of these is not the same name as one without it.
GENERATION_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
TITLES = frozenset({"dr", "mr", "mrs", "ms", "prof"})
PROFESSIONAL_CREDENTIALS = frozenset({
    "cfa", "cia", "cams", "cpa", "macc", "phd", "md", "mba", "caia", "cfe",
    "fca", "pe", "csp", "shrm-cp", "ma", "msed", "mphiled", "nacddc",
})
# A given name shorter than this is an initial, and an initial matches nothing.
MIN_GIVEN_PREFIX = 2

_PARSER = Parser(lexicon=replace(
    Lexicon.default(),
    titles=TITLES,
    given_name_titles=frozenset(),
    suffix_acronyms=PROFESSIONAL_CREDENTIALS,
    suffix_words=GENERATION_SUFFIXES | {"esq", "esquire"},
    suffix_acronyms_ambiguous=Lexicon.default().suffix_acronyms_ambiguous & PROFESSIONAL_CREDENTIALS,
    honorific_tails=frozenset(),
))
_PARENTHETICAL = re.compile(r"\(([^()]*)\)")
_DROPPED_MARKS = re.compile(r"['’.]")
_APOSTROPHES = re.compile(r"['\u2019]")
_LETTER_WORDS = re.compile(r"[^\W\d_]+")
# How alike two first names or two last names must spell to be one name's variants. A recall
# gate, not an acceptance threshold: changing it changes which pairs reach the judge.
GATE_NAME_SIM = 0.85


@dataclass(frozen=True)
class Name:
    """One written name, read. Empty `given` or `family` means the text was not a full name
    (an email address, a single word); such a name matches nothing."""

    given: tuple[str, ...]   # the given name, then any alternate from a parenthetical
    middle: tuple[str, ...]
    family: str              # the whole surname, particles included: "van der berg"
    generation: str          # "jr", "iii", or "" when none


def _clean(text: str) -> str:
    """NFC, casefold, apostrophes and periods out, single spaces."""
    folded: str = unicodedata.normalize("NFC", text).casefold()
    return " ".join(_DROPPED_MARKS.sub("", folded).split())


def parse_name(text: str) -> Name:
    # An email address saved as the name is not a name.
    if "@" in text:
        return Name((), (), "", "")
    # Pull every parenthetical out first; the parser would read it as a nickname.
    alternates: list[str] = []
    remaining: str = text
    match: re.Match[str] | None = _PARENTHETICAL.search(remaining)
    while match is not None:
        before: str = remaining[: match.start()].strip()
        after: str = remaining[match.end():].strip()
        inner: str = _clean(match.group(1))
        # Between two words it is an alternate given name; at either end it is dropped.
        if before and after and inner:
            alternates.append(inner)
        remaining = before + " " + after
        match = _PARENTHETICAL.search(remaining)
    # The parser puts the given name first ("Smith, John" is john smith) and takes titles out.
    person = _PARSER.parse(unicodedata.normalize("NFC", remaining).strip())
    given: list[str] = []
    first_given: str = _clean(person.given)
    if first_given:
        given.append(first_given)
    for alternate in alternates:
        if alternate not in given:
            given.append(alternate)
    # Only a generation word counts from the suffix; credentials are dropped.
    generation: str = ""
    for word in _clean(person.suffix).split():
        if word in GENERATION_SUFFIXES:
            generation = word
    return Name(tuple(given), tuple(_clean(person.middle).split()), _clean(person.family), generation)


def _given_names_match(first: str, second: str) -> bool:
    """Equal, or the shorter is a spelled prefix of the longer: ben and benjamin, not b."""
    shorter: str = first
    longer: str = second
    if len(second) < len(first):
        shorter = second
        longer = first
    return len(shorter) >= MIN_GIVEN_PREFIX and longer.startswith(shorter)


def _middle_names_agree(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Missing on one side, or word for word equal or an initial of the other (q and quinn)."""
    if not first or not second:
        return True
    if len(first) != len(second):
        return False
    for left, right in zip(first, second):
        if left == right:
            continue
        one_is_initial: bool = len(left) == 1 or len(right) == 1
        if not (one_is_initial and left[0] == right[0]):
            return False
    return True


def same_person_name(a: Name, b: Name) -> bool:
    # Both must be full names: a given name and a surname.
    if not a.given or not b.given or not a.family or not b.family:
        return False
    if a.family != b.family or a.generation != b.generation:
        return False
    if not _middle_names_agree(a.middle, b.middle):
        return False
    # Any given name of one against any given name of the other: the alternates are tried too.
    for left in a.given:
        for right in b.given:
            if _given_names_match(left, right):
                return True
    return False


# ---- the recall gate for the Sol judge


def jaro(first: str, second: str) -> float:
    """The Jaro similarity from the Winkler Census specification."""
    if first == second:
        return 1.0
    if not first or not second:
        return 0.0
    match_distance: int = max(len(first), len(second)) // 2 - 1
    first_matches: list[bool] = [False] * len(first)
    second_matches: list[bool] = [False] * len(second)
    # Count the characters that appear in both within the match window.
    matches: int = 0
    for index, character in enumerate(first):
        lower: int = max(0, index - match_distance)
        upper: int = min(index + match_distance + 1, len(second))
        for other in range(lower, upper):
            if not second_matches[other] and second[other] == character:
                first_matches[index] = True
                second_matches[other] = True
                matches += 1
                break
    if not matches:
        return 0.0
    # Count the matched characters that are out of order: half of them are transpositions.
    transpositions: int = 0
    other = 0
    for index, matched in enumerate(first_matches):
        if not matched:
            continue
        while not second_matches[other]:
            other += 1
        if first[index] != second[other]:
            transpositions += 1
        other += 1
    transpositions //= 2
    return (matches / len(first) + matches / len(second) + (matches - transpositions) / matches) / 3


def jaro_winkler(first: str, second: str, prefix_weight: float = 0.1) -> float:
    """Jaro-Winkler similarity with the standard four-character prefix cap."""
    base: float = jaro(first, second)
    prefix: int = 0
    for left, right in zip(first, second):
        if left != right or prefix >= 4:
            break
        prefix += 1
    return base + prefix * prefix_weight * (1 - base)


def name_words(text: str) -> tuple[str, ...]:
    """The words of a name, given name first and titles left out: "Bravo, Dr Jordan" reads as jordan
    bravo. A generation word (jr, iii) is kept at the end. An email address saved as the name has no
    words. An apostrophe does not split a word: o'bravo is one word."""
    if "@" in text:
        return ()
    person = _PARSER.parse(unicodedata.normalize("NFC", text).strip())
    ordered: str = _APOSTROPHES.sub("", unicodedata.normalize("NFC", " ".join((person.given, person.middle, person.family))))
    suffix: str = _APOSTROPHES.sub("", unicodedata.normalize("NFC", person.suffix))
    words: list[str] = _LETTER_WORDS.findall(ordered.casefold())
    for word in _LETTER_WORDS.findall(suffix.casefold()):
        if word in GENERATION_SUFFIXES:
            words.append(word)
    return tuple(words)


def _is_full_name(words: tuple[str, ...]) -> bool:
    """A first and a last word that are both spelled out, not initials."""
    return len(words) > 1 and len(words[0]) > 1 and len(words[-1]) > 1


def _generations(words: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    for word in words:
        if word in GENERATION_SUFFIXES:
            found.add(word)
    return found


def _same_full_name(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Two full names with the same generation and either the same words in any order, or the same
    first and last word with middle names that do not differ."""
    if not _is_full_name(first) or not _is_full_name(second):
        return False
    if _generations(first) != _generations(second):
        return False
    if sorted(first) == sorted(second):
        return True
    if first[0] != second[0] or first[-1] != second[-1]:
        return False
    return _middle_names_agree(first[1:-1], second[1:-1])


def _word_forms_match(first: str, second: str) -> bool:
    """A given name equals, begins or nearly spells the other: jordan/j, ben/benjamin, jon/john."""
    if first.startswith(second) or second.startswith(first):
        return True
    return jaro_winkler(first, second) >= GATE_NAME_SIM


def _surnames_match(first: str, second: str) -> bool:
    """A surname equals the other, is a one-letter initial of it, or nearly spells it (a typo or a
    transliteration: bleuel/bluel, kamhawi/kamwahi). A longer prefix is no longer a match by itself:
    Li is not Litwak, Ho is not Hoang; Tan still meets Tang because they nearly spell each other
    (decided 2026-10-07)."""
    if first == second:
        return True
    if len(first) == 1 or len(second) == 1:
        return second.startswith(first) or first.startswith(second)
    return jaro_winkler(first, second) >= GATE_NAME_SIM


def names_can_match(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Can one name be a form of the other? Decides which pairs are worth a judgment, not the judgment."""
    if not first or not second:
        return False
    if first == second or _same_full_name(first, second):
        return True
    # A one-word name is never paired by name: a first name alone could be anyone's (decided 2026-10-07).
    if len(first) < 2 or len(second) < 2:
        return False
    return _word_forms_match(first[0], second[0]) and _surnames_match(first[-1], second[-1])


def source_names_can_match(names: list[str]) -> bool:
    """Every written name has words and is compatible with every other one."""
    words: list[tuple[str, ...]] = []
    for name in names:
        read: tuple[str, ...] = name_words(name)
        if not read:
            return False
        words.append(read)
    for index, left in enumerate(words):
        for right in words[index + 1:]:
            if not names_can_match(left, right):
                return False
    return bool(words)


def names_for_matching(written: str, dossier_name: str) -> str:
    """The name dedupe blocks and gates on. The written name, unless it is a single word and the
    dossier's canonical name begins with that word: then the dossier name, which only extends what the
    source wrote ("Chris" with a dossier of "Chris Furmanski" matches as Chris Furmanski). An empty
    written name matches nothing."""
    words: tuple[str, ...] = name_words(written)
    dossier_words: tuple[str, ...] = name_words(dossier_name)
    if len(words) == 1 and len(dossier_words) >= 2 and dossier_words[0] == words[0]:
        return dossier_name
    return written

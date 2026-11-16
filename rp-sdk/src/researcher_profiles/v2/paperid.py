"""Mint `paperId`s that are unique within a profile and never reassigned.

A `paperId` is the key every in-profile reference uses: inline `[paperId]`
citations, `derivedFrom: {paper: ...}`, and the summary and full-text filenames.
So an id, once given to a paper, stays with it, and a new paper whose usual id
is taken gets a longer one rather than taking the short id away.

The usual id is first-author surname + year + the first significant title word
(`doe2020atlas`). On a collision the next significant title words are added one
at a time (`doe2020atlas-proteome`). Two records whose titles give the same id
however many words are added are the same work in two versions (a preprint and
its published version, say) and are merged instead, which this module leaves
to the caller: `assign_paper_ids` reports them.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# Words skipped when picking title words. Kept short on purpose: the id should
# read like the title.
STOPWORDS = frozenset(
    "a an the of on in for and or to with by from at as is are via toward towards".split()
)

# Conference abstract numbering ("Abstract 3439:", "Abstract B082:") says nothing
# about the work.
_ABSTRACT_PREFIX = re.compile(r"^\s*abstract\s+[a-z]?\d+[a-z]?\s*:\s*", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")


class SameWork(ValueError):
    """Two records give the same id with every title word used: one work, two versions."""


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def title_words(title: str) -> list[str]:
    """Significant title words in order, lowercased ASCII."""
    text = _ABSTRACT_PREFIX.sub("", _TAGS.sub("", title or ""))
    return [w for w in re.findall(r"[a-z0-9]+", _ascii(text)) if w not in STOPWORDS]


def surname_key(surname: str) -> str:
    return re.sub(r"[^a-z]", "", _ascii(surname or "")) or "anon"


def candidates(surname: str, year: int | str | None, title: str) -> list[str]:
    """Ids for one paper from shortest to longest: base, then one more title word each."""
    stem = f"{surname_key(surname)}{year or 'nd'}"
    words = title_words(title)
    if not words:
        return [stem]
    out = [stem + words[0]]
    for i in range(1, len(words)):
        out.append(out[0] + "-" + "-".join(words[1 : i + 1]))
    return out


def mint_paper_id(surname: str, year: int | str | None, title: str, existing: Iterable[str]) -> str:
    """The shortest candidate id not already in `existing`.

    Raises `SameWork` when every candidate is taken, which means an existing
    record has the same title: merge the two as versions of one work.
    """
    taken = set(existing)
    for cand in candidates(surname, year, title):
        if cand not in taken:
            return cand
    raise SameWork(f"every id for {title!r} is taken; merge it with the record that has it")


def assign_paper_ids(records: list[dict], *, key: str = "paperId") -> list[tuple[int, int]]:
    """Give every record without an id a unique one, in place.

    Records that already have an id keep it. New records are taken in a fixed
    order (year, then title, then `@id`) so two builders given the same
    bibliography mint the same ids. Each record needs `surname`, `year` and
    `title` under the keys `_surname`, `_year`, `_title`, or `name` and
    `datePublished` for the latter two.

    Returns `(i, j)` pairs: new record `i` has the same first author and title
    as record `j`, so it is the same work in another version and should be
    merged into `j` rather than given an id.
    """

    def year(r: dict) -> str:
        return str(r.get("_year") or r.get("datePublished") or "")[:4]

    def title(r: dict) -> str:
        return r.get("_title") or r.get("name") or ""

    def same_key(r: dict) -> tuple:
        return (surname_key(r.get("_surname", "")), tuple(title_words(title(r))))

    taken = {r[key] for r in records if r.get(key)}
    owner = {r[key]: i for i, r in enumerate(records) if r.get(key)}
    by_title = {same_key(r): i for i, r in enumerate(records) if r.get(key)}
    same: list[tuple[int, int]] = []

    new = sorted(
        (i for i, r in enumerate(records) if not r.get(key)),
        key=lambda i: (
            year(records[i]),
            title(records[i]).lower(),
            str(records[i].get("@id") or ""),
        ),
    )
    for i in new:
        r = records[i]
        if same_key(r) in by_title:
            same.append((i, by_title[same_key(r)]))
            continue
        try:
            pid = mint_paper_id(r.get("_surname", ""), year(r) or None, title(r), taken)
        except SameWork:
            longest = candidates(r.get("_surname", ""), year(r) or None, title(r))[-1]
            same.append((i, owner[longest]))
            continue
        r[key] = pid
        taken.add(pid)
        owner[pid] = i
        by_title[same_key(r)] = i
    return same

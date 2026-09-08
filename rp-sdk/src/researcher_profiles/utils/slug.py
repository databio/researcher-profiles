"""The slug rule: the one place that decides a profile directory's name.

Everything downstream treats a slug as an opaque, already-valid string. When
the slug scheme changes, it replaces exactly one function body here and nothing
else moves.

Two rules make that possible:

1. An existing linkage is durable: if a caller already carries an
   ``rp_slug`` for this person, that is the slug. It is returned unchanged,
   no matter what the current derivation rule would produce. A sync
   therefore never renames a live directory as a side effect of a policy
   change; a rename is a separate operation.
2. Directory names are arbitrary: a profile directory may be a raw ORCID
   (``0000-0002-1825-0097``), a ``lastname-firstname`` pair, or anything else
   satisfying the grammar. Nothing here inspects a slug's shape to infer
   meaning, and nothing renames one.

The grammar is ``SLUG_RE``, defined here as the single canonical copy:
``^[a-z0-9][a-z0-9-]*$``. ``schema`` and the serving side's
``researcher_profiles.api.upload`` import this same object rather than retyping
it, so every consumer of the grammar shares one object and the copies cannot
drift.
"""

import re

#: Directory-name grammar. Applies only to display handles (profile directory
#: names, the ``slug``). Never apply it to a ``rid``: it forbids uppercase and
#: would reject every ``X``-suffixed ORCID. This is the single canonical copy;
#: ``schema/`` and ``api/upload.py`` import it rather than redefining it. This
#: module stays stdlib-only, so the definition lives here (a leaf) and callers
#: that need the grammar depend on ``utils.slug`` rather than the reverse.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

__all__ = ["SLUG_RE", "SlugError", "resolve_slug"]


class SlugError(ValueError):
    """A slug could not be derived, or the derived slug is malformed."""


def resolve_slug(
    entry: dict,
    existing_rp_slug: str | None = None,
    *,
    taken: set[str] | None = None,
) -> str:
    """Return the profile directory slug for one source entry.

    ``entry`` is a source record (``user_id``/``orcid``/``name``/``rp_slug``).
    ``existing_rp_slug`` is the linkage the caller already holds, if any; when
    set it wins outright, per rule 1: an existing linkage is durable.
    ``taken`` is the set of directory names already in use, so a collision gets
    a numeric suffix instead of two people sharing a directory.

    The derivation is ``lastname-firstname`` from the display name, falling back
    to the lowercased ORCID when there is no usable name.

    Why not just the ORCID
    ----------------------

    ORCID was the initial rule because the one pre-existing bundle used it. It
    does not survive contact with a campaign: 300 directories named
    ``0000-0002-1825-0097`` are unreadable to the person who has to browse them,
    and this cache is browsed by hand. Deriving from the name is safe *because*
    identity is the rid and the directory name is only a display handle.
    ``<root>/.cache/index.json`` maps between them, so a name that later turns out
    wrong is a rename, not a data migration.

    The rule is the crudest one that works: last token as surname,
    first token as given name. On the profiles built before it existed, it
    reproduces every single directory name, including compound surnames such as
    ``vale-ortiz-robin``. A cleverer parser would be a liability: it would
    disagree with those names somewhere, and every disagreement orphans a
    directory.
    """
    if existing_rp_slug:
        return _validated(existing_rp_slug.strip(), source="rp_slug")

    derived = _from_name(entry.get("name"))
    source = "name"
    if derived is None:
        derived = (entry.get("orcid") or "").strip().lower()
        source = "orcid"
    if not derived:
        raise SlugError(
            f"cannot derive a slug for user_id={entry.get('user_id')!r}: "
            "no usable name and no orcid"
        )

    slug = _validated(derived, source=source)
    if not taken or slug not in taken:
        return slug
    # Two different people, one handle. Suffix rather than merge: sharing a
    # directory writes one person's papers into another person's profile.
    for n in range(2, 100):
        candidate = f"{slug}-{n}"
        if candidate not in taken:
            return candidate
    raise SlugError(f"cannot find a free directory name for {slug!r}")


def _from_name(name: str | None) -> str | None:
    """``"Robin Vale-Ortiz"`` -> ``"vale-ortiz-robin"``; ``None`` if unusable."""
    if not name:
        return None
    # Drop anything that is not a letter, a space or a hyphen: initials with
    # periods ("Jane R. Doe"), commas, and degrees would otherwise end up
    # in a directory name or break the grammar.
    cleaned = re.sub(r"[^A-Za-z \-]", " ", name)
    tokens = [t.strip("-") for t in cleaned.split()]
    tokens = [t for t in tokens if len(t) > 1]  # drops bare initials
    if len(tokens) < 2:
        return None
    candidate = f"{tokens[-1]}-{tokens[0]}".lower()
    candidate = re.sub(r"-+", "-", candidate).strip("-")
    return candidate or None


def _validated(slug: str, *, source: str) -> str:
    if not SLUG_RE.match(slug):
        raise SlugError(f"slug {slug!r} (from {source}) does not match {SLUG_RE.pattern}")
    return slug

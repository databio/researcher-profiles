"""Split a profile's text into the small pieces that each get their own embedding vector."""

import re
from dataclasses import dataclass, field

# Heading detection
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_H3_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
# Citation tags like [foo2024bar] or [foo2024bar, baz2025qux]. A paper id
# holds a 4-digit year and may end there (live2005), carry a numbered suffix
# (extracellular2023_2), start with the year (2016activation), or be all
# digits (20261062, 8+ so a bracketed year never matches). Keep in step with
# agent-runners pipelines/rprofiles/summaries.py PAPER_ID_PATTERN.
_PAPER_ID = r"(?:[^\W\d_A-Z][\w-]*?\d{4}[\w-]*|\d{4}[^\W\d_A-Z]{2,}[\w-]*|\d{8,})"
_CITATION_RE = re.compile(rf"\[(?:{_PAPER_ID}(?:,\s*)?)+\]")
_ABSTRACT_ONLY_RE = re.compile(r"\[abstract-only\]\s*", re.IGNORECASE)

MAX_CHUNK_CHARS = 8000
SECTION_SPLIT_THRESHOLD = 2000

#: Chunk source types that represent a paper. ``lite`` profiles index paper
#: abstracts; ``full``/``deep`` profiles index LLM-written paper summaries.
#: Anything counting or citing "papers" must consider both, otherwise lite
#: profiles silently report zero papers and zero paper evidence.
PAPER_CHUNK_TYPES = ("paper_summary", "paper_abstract")


@dataclass
class Chunk:
    """One indexable chunk."""

    source_type: str
    source_id: str
    chunk_index: int
    text: str  # stored / returned text (citations preserved)
    embed_text: str  # cleaned text actually fed to the embedder
    section: str | None = None
    meta: dict = field(default_factory=dict)


def _strip_citations(text: str) -> str:
    return _CITATION_RE.sub("", text)


def _split_long_paragraph(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split a chunk on paragraph boundaries when it exceeds ``limit`` chars."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    paragraphs = re.split(r"\n\s*\n", text)
    buf: list[str] = []
    buf_len = 0
    for p in paragraphs:
        if buf_len + len(p) + 2 > limit and buf:
            out.append("\n\n".join(buf).strip())
            buf = [p]
            buf_len = len(p)
        else:
            buf.append(p)
            buf_len += len(p) + 2
    if buf:
        out.append("\n\n".join(buf).strip())
    return [c for c in out if c.strip()]


def _split_by_headings(text: str, heading_re: re.Pattern) -> list[tuple[str | None, str]]:
    """Split text by a heading regex.

    Returns a list of ``(heading_or_None, body_including_heading_line)``.
    Content before the first heading is captured under ``None``.
    """
    out: list[tuple[str | None, str]] = []
    matches = list(heading_re.finditer(text))
    if not matches:
        return [(None, text)]
    if matches[0].start() > 0:
        prelude = text[: matches[0].start()].strip()
        if prelude:
            out.append((None, prelude))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1).strip(), text[start:end].strip()))
    return out


def _chunk_markdown_doc(text: str, source_type: str, source_id: str) -> list[Chunk]:
    """Generic H2/H3 chunker used for expertise and SOUL."""
    chunks: list[Chunk] = []
    sections = _split_by_headings(text, _H2_RE)
    idx = 0
    for h2, body in sections:
        if not body.strip():
            continue
        if len(body) > SECTION_SPLIT_THRESHOLD and _H3_RE.search(body):
            for h3, sub in _split_by_headings(body, _H3_RE):
                if not sub.strip():
                    continue
                section_label = h3 or h2
                for piece in _split_long_paragraph(sub):
                    embed_text = _strip_citations(piece).strip()
                    if not embed_text:
                        continue
                    chunks.append(
                        Chunk(
                            source_type=source_type,
                            source_id=source_id,
                            chunk_index=idx,
                            text=piece,
                            embed_text=(
                                f"{section_label}\n\n{embed_text}" if section_label else embed_text
                            ),
                            section=section_label,
                        )
                    )
                    idx += 1
        else:
            for piece in _split_long_paragraph(body):
                embed_text = _strip_citations(piece).strip()
                if not embed_text:
                    continue
                chunks.append(
                    Chunk(
                        source_type=source_type,
                        source_id=source_id,
                        chunk_index=idx,
                        text=piece,
                        embed_text=(f"{h2}\n\n{embed_text}" if h2 else embed_text),
                        section=h2,
                    )
                )
                idx += 1
    return chunks


def chunk_expertise(text: str) -> list[Chunk]:
    """Chunk an expertise.md document on H2/H3 boundaries."""
    return _chunk_markdown_doc(text, "expertise", "expertise")


def chunk_soul(text: str) -> list[Chunk]:
    """Chunk a SOUL.md document on H2/H3 boundaries."""
    return _chunk_markdown_doc(text, "soul", "soul")


def chunk_summary(text: str, paper_id: str) -> list[Chunk]:
    """Chunk a single paper summary.

    Summaries are typically short; default is a whole-document chunk.
    Longer ones get paragraph-split. Strip the ``[abstract-only]`` marker
    from the embedded text but keep a meta flag.
    """
    is_abstract_only = bool(_ABSTRACT_ONLY_RE.search(text))
    body = _ABSTRACT_ONLY_RE.sub("", text).strip()
    if not body:
        return []
    chunks: list[Chunk] = []
    pieces = _split_long_paragraph(body)
    for i, piece in enumerate(pieces):
        embed_text = _strip_citations(piece).strip()
        if not embed_text:
            continue
        chunks.append(
            Chunk(
                source_type="paper_summary",
                source_id=paper_id,
                chunk_index=i,
                text=piece,
                embed_text=embed_text,
                section=None,
                meta={"abstract_only": is_abstract_only} if is_abstract_only else {},
            )
        )
    return chunks


def chunk_abstract(text: str, paper_id: str) -> list[Chunk]:
    """Chunk a single paper abstract for a ``lite`` profile index.

    Produces ``source_type="paper_abstract"`` chunks. Abstracts are short,
    so this reuses the ``chunk_summary`` paragraph-splitting logic. Unlike
    summaries there is no ``[abstract-only]`` marker to strip. The input is
    a raw abstract string straight from ``PaperRecord.abstract``.
    """
    body = (text or "").strip()
    if not body:
        return []
    chunks: list[Chunk] = []
    pieces = _split_long_paragraph(body)
    for i, piece in enumerate(pieces):
        embed_text = _strip_citations(piece).strip()
        if not embed_text:
            continue
        chunks.append(
            Chunk(
                source_type="paper_abstract",
                source_id=paper_id,
                chunk_index=i,
                text=piece,
                embed_text=embed_text,
                section=None,
            )
        )
    return chunks


def chunk_grant(title: str, abstract: str | None, grant_id: str) -> list[Chunk]:
    """Chunk a single grant record for a ``deep`` profile index.

    Produces ``source_type="grant"`` chunks from the grant's title plus
    abstract (when present). Grants are short; the combined text is
    paragraph-split like abstracts.
    """
    parts = [p for p in ((title or "").strip(), (abstract or "").strip()) if p]
    body = "\n\n".join(parts)
    if not body:
        return []
    chunks: list[Chunk] = []
    for i, piece in enumerate(_split_long_paragraph(body)):
        embed_text = _strip_citations(piece).strip()
        if not embed_text:
            continue
        chunks.append(
            Chunk(
                source_type="grant",
                source_id=grant_id,
                chunk_index=i,
                text=piece,
                embed_text=embed_text,
                section=None,
            )
        )
    return chunks


def chunk_cv(text: str) -> list[Chunk]:
    """Chunk a sources/cv.md document on H2/H3 boundaries (``deep`` profiles)."""
    return _chunk_markdown_doc(text, "cv", "cv")


def chunk_web(text: str, page_id: str) -> list[Chunk]:
    """Chunk one extracted web page (sources/web/<page>.md) for a ``deep`` index.

    ``page_id`` is the page's filename stem. Pages are markdown-ish extracted
    text; split on H2/H3 boundaries like other markdown docs, keyed per page.
    """
    return _chunk_markdown_doc(text, "web", page_id)


# ---------------------------------------------------------------------------
# Enumeration through ArtifactStorage
# ---------------------------------------------------------------------------

#: Every chunk ``source_type`` the enumerator can produce.
ALL_SOURCE_TYPES = frozenset(
    {"expertise", "soul", "paper_summary", "paper_abstract", "grant", "cv", "web"}
)

_SUMMARY_SUFFIX = ".summary.md"


def strip_frontmatter(text: str) -> str:
    """Drop a leading ``---`` YAML frontmatter block, if present."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + len("\n---") :].lstrip("\n")


def _collection_entries(storage, content_url: str) -> list:
    """The raw ``hasPart`` entries of a collection artifact, ``[]`` when absent."""
    import json

    raw = storage.artifact_text(content_url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    return data.get("hasPart", []) or []


def _corpus_ids(storage) -> set[str] | None:
    """Paper ids in ``sources/papers.jsonld``; None when the profile has none.

    None means "no corpus to filter against" (an interview-built profile),
    not "an empty corpus".
    """
    if not storage.artifact_text("sources/papers.jsonld"):
        return None
    return {
        str(e["paper_id"])
        for e in _collection_entries(storage, "sources/papers.jsonld")
        if isinstance(e, dict) and e.get("paper_id")
    }


def _web_page_ids(storage) -> list[str]:
    """Stems of ``sources/web/*.md`` the storage holds, sorted."""
    try:
        parts, subjects = storage.build_manifest()
    # Boundary: a backend that cannot enumerate simply has no web pages here.
    except Exception:
        return []
    ids = set()
    for ref in [*parts, *subjects]:
        url = getattr(ref, "content_url", "") or ""
        if url.startswith("sources/web/") and url.endswith(".md") and url.count("/") == 2:
            ids.add(url[len("sources/web/") : -len(".md")])
    return sorted(ids)


def enumerate_source_chunks(
    source,
    *,
    source_types=None,
    level: str | None = None,
    build_state=None,
    corpus_only: bool = False,
) -> list[Chunk]:
    """Every chunk a profile's index is built over, read through ``ArtifactStorage``.

    The one enumeration both the indexer (``SqliteEmbeddingIndex``) and the
    serve-time text recovery (``api._semantic.chunk_texts``) use, so a stored
    vector row's ``(source_type, source_id, chunk_index)`` always names the
    same piece of text on both sides.

    ``source`` is a :class:`~researcher_profiles.profile.ResearcherProfile`
    (its storage, level and build state are used) or an ``ArtifactStorage``
    (pass ``level`` and ``build_state``). ``source_types`` limits the work to
    those types; ``None`` means all of them.

    A ``lite`` profile is indexed over paper abstracts only (contaminated
    papers skipped); ``full`` and ``deep`` over expertise, SOUL and summaries,
    and ``deep`` adds grants, the CV and web pages.

    ``corpus_only`` skips a summary whose paper is not in
    ``sources/papers.jsonld`` (an orphan a builder keeps on disk until it
    prunes it). Off by default: a profile from elsewhere may name its
    summaries differently, and the format does not require them to match.
    """
    storage = getattr(source, "storage", source)
    if level is None:
        try:
            level = str(getattr(source.metadata, "level", None) or "full")
        except Exception:  # noqa: BLE001 - an unreadable document indexes as full
            level = "full"
    if build_state is None:
        try:
            build_state = source.build_state
        except Exception:  # noqa: BLE001
            build_state = None
    want = ALL_SOURCE_TYPES if source_types is None else frozenset(source_types)

    out: list[Chunk] = []
    if level == "lite":
        if "paper_abstract" not in want:
            return out
        for i, entry in enumerate(_collection_entries(storage, "sources/papers.jsonld")):
            if not isinstance(entry, dict):
                continue
            paper_id = entry.get("paper_id") or entry.get("openalex_id") or f"paper-{i}"
            if build_state is not None and build_state.is_contaminated(entry.get("paper_id")):
                continue
            abstract = (entry.get("abstract") or "").strip()
            if abstract:
                out.extend(chunk_abstract(abstract, str(paper_id)))
        return out

    if "expertise" in want:
        text = storage.load_expertise()
        if text:
            out.extend(chunk_expertise(text))
    if "soul" in want:
        text = storage.load_soul()
        if text:
            out.extend(chunk_soul(text))
    if "paper_summary" in want:
        summaries = storage.load_summaries()
        corpus = _corpus_ids(storage) if corpus_only else None
        for paper_id in sorted(summaries):
            if corpus is not None and paper_id not in corpus:
                continue
            out.extend(chunk_summary(summaries[paper_id], paper_id))
    if level == "deep":
        if "grant" in want:
            for i, entry in enumerate(_collection_entries(storage, "sources/grants.jsonld")):
                if not isinstance(entry, dict):
                    continue
                grant_id = str(entry.get("id") or f"grant-{i}")
                out.extend(
                    chunk_grant(str(entry.get("name") or ""), entry.get("abstract"), grant_id)
                )
        if "cv" in want:
            cv = storage.artifact_text("sources/cv.md")
            if cv:
                out.extend(chunk_cv(strip_frontmatter(cv)))
        if "web" in want:
            for page_id in _web_page_ids(storage):
                body = storage.artifact_text(f"sources/web/{page_id}.md")
                if body:
                    out.extend(chunk_web(strip_frontmatter(body), page_id))
    return out

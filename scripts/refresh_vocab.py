#!/usr/bin/env python3
"""Rebuild the pinned vocabulary files (maintainer tool, not shipped).

Usage: python scripts/refresh_vocab.py [--openalex] [--mesh PATH]
                                       [--release YYYY-MM] [--out-dir DIR]

Run every year or two, not on a schedule: a refresh changes what every stored
code resolves to, so it is a deliberate, reviewed commit.

- OpenAlex: pages through ``/topics`` (cursor pagination). The
  release is the snapshot month, since OpenAlex publishes no release number.
- MeSH: parses NLM's annual descriptor XML (``desc2026.xml``, or the
  ``desc2026.gz`` NLM also ships). The release is the year in the file name.
  Descriptor level only: UI, preferred label, tree numbers, entry terms.

Network access is the refresh's alone; nothing on the load path fetches.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from researcher_profiles.vocab import MESH_FILE, MESH_SYSTEM, OPENALEX_FILE, OPENALEX_SYSTEM

if TYPE_CHECKING:
    from researcher_profiles.openalex_client import OpenAlexClient

#: OpenAlex's largest supported page size.
PER_PAGE = 100
#: Entry terms kept per descriptor. The tail is orthographic variants that
#: match nothing a person types and inflate the file.
MAX_SYNONYMS = 12


def package_dir() -> Path:
    """Where the pinned files live in this checkout (the package directory)."""
    return Path(__file__).resolve().parents[1] / "rp-sdk/src/researcher_profiles/vocab"


def fetch_openalex_topics(client: OpenAlexClient, *, pause: float = 0.1) -> list[dict]:
    """Every OpenAlex topic as ``{id, display_name, subfield, field, domain}``."""
    topics: list[dict] = []
    cursor = "*"
    while True:
        params: dict[str, Any] = {"per-page": PER_PAGE, "cursor": cursor}
        data = client.get("/topics", params)
        results = data.get("results") or []
        if not results:
            break
        for r in results:
            topics.append(
                {
                    "id": (r.get("id") or "").rsplit("/", 1)[-1],
                    "display_name": r.get("display_name") or "",
                    "subfield": (r.get("subfield") or {}).get("display_name") or "",
                    "field": (r.get("field") or {}).get("display_name") or "",
                    "domain": (r.get("domain") or {}).get("display_name") or "",
                }
            )
        nxt = (data.get("meta") or {}).get("next_cursor")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(pause)
    topics.sort(key=lambda t: int(t["id"][1:]) if t["id"][1:].isdigit() else 0)
    return topics


def mesh_release_from_name(path: Path) -> str:
    """``desc2026.xml`` / ``desc2026.gz`` -> ``2026``; ``unknown`` if no year."""
    digits = "".join(c for c in path.name.split(".")[0] if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else "unknown"


def _open_mesh(path: Path) -> IO[bytes]:
    return gzip.open(path) if path.suffix == ".gz" else path.open("rb")


def mesh_descriptors_from_xml(path: Path) -> list[dict]:
    """Descriptor UI, preferred label, tree numbers and entry terms.

    Parsed with ``iterparse`` and cleared record by record: the XML runs to
    hundreds of megabytes.
    """
    out: list[dict] = []
    with _open_mesh(path) as fh:
        for _event, record in ET.iterparse(fh, events=("end",)):
            if record.tag != "DescriptorRecord":
                continue
            ui = record.findtext("DescriptorUI") or ""
            label = record.findtext("DescriptorName/String") or ""
            if ui and label:
                trees = [t.text for t in record.iterfind("TreeNumberList/TreeNumber") if t.text]
                synonyms = [
                    text
                    for text in (t.text or "" for t in record.iterfind(".//Term/String"))
                    if text and text != label
                ]
                out.append(
                    {
                        "id": ui,
                        "label": label,
                        "tree_numbers": trees,
                        "synonyms": list(dict.fromkeys(synonyms))[:MAX_SYNONYMS],
                    }
                )
            record.clear()
    return out


def _dump(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def write_openalex(topics: list[dict], release: str, out_dir: Path) -> Path:
    """Gzipped, one topic per line inside, so the decompressed text diffs readably."""
    path = out_dir / OPENALEX_FILE
    head = json.dumps({"release": release, "system": OPENALEX_SYSTEM})[:-1]
    rows = ",\n".join(json.dumps(t, ensure_ascii=False) for t in topics)
    text = f'{head}, "topics": [\n{rows}\n]}}\n'
    # mtime=0: the same input gives the same bytes, so a no-op refresh is no diff.
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    return path


def write_mesh(descriptors: list[dict], release: str, out_dir: Path) -> Path:
    path = out_dir / MESH_FILE
    doc = {"release": release, "system": MESH_SYSTEM, "descriptors": descriptors}
    path.write_bytes(gzip.compress(_dump(doc), mtime=0))
    return path


def _ids(path: Path, key: str) -> set[str]:
    if not path.exists():
        return set()
    raw = path.read_bytes()
    data = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
    return {row["id"] for row in data.get(key, [])}


def refresh(
    *,
    openalex: bool = False,
    mesh_xml: Path | None = None,
    out_dir: Path | None = None,
    release: str | None = None,
) -> list[str]:
    """Rewrite the requested files; return one summary line per file."""
    out = out_dir or package_dir()
    lines: list[str] = []
    if openalex:
        path = out / OPENALEX_FILE
        before = _ids(path, "topics")
        from researcher_profiles.openalex_client import OpenAlexClient

        with OpenAlexClient(os.environ.get("OPENALEX_API_KEY")) as client:
            topics = fetch_openalex_topics(client)
        rel = release or datetime.now(timezone.utc).strftime("%Y-%m")
        write_openalex(topics, rel, out)
        after = {t["id"] for t in topics}
        lines.append(
            f"openalex topics {rel}: {len(after)} topics, "
            f"{len(after - before)} added, {len(before - after)} removed -> {path}"
        )
    if mesh_xml is not None:
        path = out / MESH_FILE
        before = _ids(path, "descriptors")
        descriptors = mesh_descriptors_from_xml(Path(mesh_xml))
        rel = mesh_release_from_name(Path(mesh_xml))
        write_mesh(descriptors, rel, out)
        after = {d["id"] for d in descriptors}
        lines.append(
            f"mesh {rel}: {len(after)} descriptors, "
            f"{len(after - before)} added, {len(before - after)} removed -> {path}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rewrite the pinned vocabulary files from their sources"
    )
    ap.add_argument(
        "--openalex", action="store_true", help="Refetch the OpenAlex topic list (network)"
    )
    ap.add_argument(
        "--mesh",
        metavar="PATH",
        help="NLM MeSH descriptor XML (desc<YEAR>.xml, or its .gz); the year is the release",
    )
    ap.add_argument("--release", help="OpenAlex snapshot release (default: this month, YYYY-MM)")
    ap.add_argument(
        "--out-dir",
        help="Directory to write into (default: the package's own vocab/ directory)",
    )
    args = ap.parse_args(argv)
    if not args.openalex and not args.mesh:
        print("error: nothing to refresh; pass --openalex and/or --mesh PATH", file=sys.stderr)
        return 2
    for line in refresh(
        openalex=args.openalex,
        mesh_xml=Path(args.mesh) if args.mesh else None,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        release=args.release,
    ):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Generate JSON Schema files from the package's Pydantic models.

A consumer who does not want to install this package can validate a document
against the JSON Schema files checked into ``schemas/`` at the repo root
instead. This module (re)generates them from the authoritative Pydantic
models.
"""

import json
from pathlib import Path

from .affil import Affiliation
from .biblio import Authorship, Paper
from .funding import Award, Opportunity
from .org import Organization
from .person import Person, PersonRef

_SCHEMA_MODELS = {
    "person": Person,
    "person_ref": PersonRef,
    "paper": Paper,
    "authorship": Authorship,
    "award": Award,
    "opportunity": Opportunity,
    "organization": Organization,
    "affiliation": Affiliation,
}


def build_schemas() -> dict[str, dict]:
    """Return ``{name: json_schema_dict}`` for every exported model."""
    return {name: model.model_json_schema() for name, model in _SCHEMA_MODELS.items()}


def export_schemas(out_dir: str | Path) -> list[Path]:
    """Write one ``<name>.schema.json`` file per model into ``out_dir``.

    Args:
        out_dir: The directory to write schema files into. Created if it does
            not exist.

    Returns:
        The list of written paths.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in build_schemas().items():
        path = out / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


__all__ = [
    "build_schemas",
    "export_schemas",
]

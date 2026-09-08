"""Value objects for the derived profile graph.

The graph models researchers as canonical persons (:class:`PersonNode`) and
the relations between them (:class:`GraphEdge`). It is derived entirely from the
already-published bibliometric fields of each profile (author lists,
affiliations, training records), so nothing here depends on an LLM, a persona,
or paper full text.

Two node kinds:

* ``profiled``: a person with a profile in the store. The node key is the
  profile ``rid`` (an ORCID or a ``local:`` id), the single join key used
  everywhere else in the system.
* ``external``: a coauthor / advisor name that appears in a corpus but has no
  profile. The node key is a normalized name (see :mod:`.identity`). No ``rid``.

Edge types, each carrying evidence:

1. ``coauthor`` (undirected): two persons on the same paper's author list.
2. ``shared_institution`` (undirected): two persons share an institution.
3. ``advised`` (directed advisee -> advisor): mentorship from ``training``.
4. ``citation`` (directed A-cites-B): RESERVED. Not derivable from the current
   corpus (``PaperRecord`` has no per-paper reference list); the type exists so
   storage and consumers do not change when an OpenAlex enrichment lands.
"""

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class EdgeType(str, Enum):
    """The relation an edge asserts. String-valued so it stores verbatim."""

    coauthor = "coauthor"
    shared_institution = "shared_institution"
    advised = "advised"
    #: RESERVED: see the module docstring. No builder emits this by default.
    citation = "citation"


#: Edge types whose direction is meaningful (``src`` -> ``dst``). Everything
#: else is symmetric and is stored once with ``src <= dst``.
DIRECTED_TYPES = frozenset({EdgeType.advised, EdgeType.citation})


class InstitutionRef(BaseModel):
    """One institution a profiled person has been affiliated with.

    ``key`` is the join key (a ROR IRI when known, else a normalized name);
    ``name`` is a human-readable label for reasons/UI.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    name: Optional[str] = None
    year_start: Optional[int] = None
    year_end: Optional[int] = None


class PersonNode(BaseModel):
    """A canonical person: a profiled researcher or an external name."""

    model_config = ConfigDict(extra="forbid")

    key: str
    kind: Literal["profiled", "external"]
    #: The join key for a profiled person (ORCID or ``local:`` id). ``None`` for
    #: an external node.
    rid: Optional[str] = None
    #: Directory handle of a profiled person, so a slug ref resolves without a
    #: registry. ``None`` for external nodes.
    slug: Optional[str] = None
    #: Best display name seen for this person.
    name: Optional[str] = None
    #: Institution history: populated for profiled nodes only. Lets a COI check
    #: catch a same-institution conflict against an external manuscript author
    #: (who has no node of their own but passes an affiliation inline).
    institutions: list[InstitutionRef] = Field(default_factory=list)


class GraphEdge(BaseModel):
    """One derived relation between two persons, carrying its evidence."""

    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    type: EdgeType
    directed: bool = False
    #: Which resolution rule / evidence class produced this edge:
    #: ``high`` (cross-corpus paper join, no name matching),
    #: ``medium`` (name-matched to a profile), ``low`` (free-text name / external).
    confidence: str = "medium"

    # coauthor evidence
    paper_count: int = 0
    first_year: Optional[int] = None
    last_year: Optional[int] = None

    # shared_institution evidence
    institution: Optional[str] = None
    overlap_years: Optional[bool] = None

    # advised evidence
    #: The training ``kind`` (degree / postdoc / clinical_training) the advising
    #: relationship was recorded under.
    training_kind: Optional[str] = None

    #: Evidence pointers: paper ids / DOIs for a coauthor edge.
    evidence: list[str] = Field(default_factory=list)

    def key(self) -> tuple[str, str, str]:
        """Dedup key: (src, dst, type). Undirected edges are pre-canonicalized."""
        return (self.src, self.dst, self.type.value)


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    """Order two node keys so an undirected edge is stored exactly once."""
    return (a, b) if a <= b else (b, a)


__all__ = [
    "EdgeType",
    "DIRECTED_TYPES",
    "InstitutionRef",
    "PersonNode",
    "GraphEdge",
    "canonical_pair",
]

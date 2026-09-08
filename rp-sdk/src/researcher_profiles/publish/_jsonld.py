"""Self-contained JSON-LD document generation for the static site.

Each ``profile.jsonld`` is a self-contained graph: Person + every
ScholarlyArticle + every Grant inlined into one ``@graph``. Crawlers
ingest only the document they fetch.

All site-local ``@id`` values are relative (``#person``, ``#paper/<id>``,
``#grant/<id>``), the same ids the ``sources/*.jsonld`` collections carry, so
a node here joins to its collection record by ``@id``. No ``@base`` is set.
Relative ids resolve against the document's fetch URL, making the tree
relocatable.

Global persistent identifiers (ORCID, DOI, ROR) are absolute IRIs in
``identifier``/``sameAs``/``url``.
"""

from typing import Any

from ..schema import GrantRecord, PaperRecord, normalize_doi
from ..schema.jsonld import CONTEXT_URL


def paper_node(paper: PaperRecord, *, person_id: str = "#person") -> dict[str, Any]:
    """Build a ScholarlyArticle node for the JSON-LD graph."""
    # The same @id as the record's node in sources/papers.jsonld: DOI IRI,
    # else OpenAlex IRI, else the #paper/<paper_id> fragment.
    node: dict[str, Any] = {"@type": "ScholarlyArticle", "@id": paper.resolve_id()}

    node["name"] = paper.title
    if paper.year is not None:
        node["datePublished"] = str(paper.year)
    if paper.journal:
        node["isPartOf"] = {"@type": "Periodical", "name": paper.journal}
    if paper.first_author:
        node["first_author"] = paper.first_author
    doi = normalize_doi(paper.doi)
    if doi:
        node["identifier"] = f"https://doi.org/{doi}"
    if paper.paper_id:
        node["paper_id"] = paper.paper_id
    # Authorship link back to person
    node["author"] = {"@id": person_id}

    return {k: v for k, v in node.items() if v is not None}


def grant_node(
    grant: GrantRecord,
    *,
    include_abstract: bool = False,
) -> dict[str, Any]:
    """Build a MonetaryGrant node for the JSON-LD graph."""
    node: dict[str, Any] = {
        "@type": "MonetaryGrant",
        "@id": f"#grant/{grant.id}",
        "name": grant.title,
    }
    if grant.funder:
        node["funder"] = {"@type": "Organization", "name": grant.funder}
    # The context's MonetaryGrant scope maps these to rp:grantRole and
    # rp:grantStatus, the same terms sources/grants.jsonld uses.
    if grant.role:
        node["role"] = grant.role
    if grant.status:
        node["status"] = grant.status
    if grant.start:
        node["startDate"] = grant.start
    if grant.end:
        node["endDate"] = grant.end
    if include_abstract and grant.abstract:
        node["abstract"] = grant.abstract
    if grant.number:
        node["identifier"] = grant.number
    return {k: v for k, v in node.items() if v is not None}


def profile_jsonld_graph(
    profile_data: dict[str, Any],
    papers: list[PaperRecord],
    grants: list[GrantRecord],
    *,
    include_grant_abstracts: bool = False,
) -> dict[str, Any]:
    """Build the self-contained JSON-LD document for a profile.

    Returns a dict with ``@context`` and ``@graph`` containing the Person
    node plus all inlined ScholarlyArticle and MonetaryGrant nodes.
    """
    md = profile_data.get("metadata", {})
    rid = profile_data.get("rid")

    # Person node
    person: dict[str, Any] = {
        "@type": "Person",
        "@id": "#person",
        "name": md.get("name", ""),
    }

    if md.get("affiliation"):
        person["affiliation"] = {"@type": "Organization", "name": md["affiliation"]}
    if md.get("field"):
        person["rp:field"] = md["field"]
    if md.get("summary"):
        person["description"] = md["summary"]
    if rid:
        person["rp:rid"] = rid
    if md.get("level"):
        person["rp:level"] = md["level"]
    if profile_data.get("slug"):
        person["rp:slug"] = profile_data["slug"]

    # sameAs and identifier for global persistent IDs
    same_as = []
    if rid and not str(rid).startswith("local:"):
        same_as.append(f"https://orcid.org/{rid}")
    if md.get("scholar_url"):
        same_as.append(md["scholar_url"])
    if same_as:
        person["sameAs"] = same_as

    identifiers = []
    if md.get("openalex_id"):
        identifiers.append(
            {
                "@type": "PropertyValue",
                "propertyID": "openalex",
                "value": md["openalex_id"],
            }
        )
    if identifiers:
        person["identifier"] = identifiers

    # Inline paper references into person (rp:authored)
    paper_nodes = []
    paper_refs = []
    for p in papers:
        pn = paper_node(p, person_id="#person")
        paper_nodes.append(pn)
        paper_refs.append({"@id": pn["@id"]})
    if paper_refs:
        person["rp:authored"] = paper_refs

    # Inline grant references
    grant_nodes = []
    grant_refs = []
    for g in grants:
        gn = grant_node(g, include_abstract=include_grant_abstracts)
        grant_nodes.append(gn)
        grant_refs.append({"@id": gn["@id"]})
    if grant_refs:
        person["rp:heldGrant"] = grant_refs

    # Conformance
    person["conformsTo"] = CONTEXT_URL

    # Build the graph
    graph = [person] + paper_nodes + grant_nodes

    return {
        "@context": CONTEXT_URL,
        "@graph": graph,
    }


def catalog_jsonld(
    profiles: list[dict[str, Any]],
    *,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Build the root ``index.jsonld`` DataCatalog.

    With ``base_url`` set, every entry carries the absolute ``@id`` of its
    ``profile.jsonld`` and the ``url`` of its folder, so a consumer can get from
    the catalog to a profile without re-deriving paths by convention. Without
    it, not one key is added.
    """
    base = base_url.rstrip("/") if base_url else None
    datasets = []
    for p in sorted(profiles, key=lambda x: x.get("slug", "")):
        slug = p.get("slug", "")
        entry: dict[str, Any] = {
            "@type": "Dataset",
            "name": p.get("name", slug),
            "rp:slug": slug,
        }
        if base:
            entry["@id"] = f"{base}/profiles/{slug}/profile.jsonld"
            entry["url"] = f"{base}/profiles/{slug}/"
        rid = p.get("rid")
        if rid:
            entry["rp:rid"] = rid
        if p.get("affiliation"):
            entry["rp:affiliation"] = p["affiliation"]
        datasets.append(entry)

    catalog: dict[str, Any] = {
        "@context": CONTEXT_URL,
        "@type": "DataCatalog",
        "name": "Researcher Profiles",
        "conformsTo": CONTEXT_URL,
        "dataset": datasets,
    }
    if base:
        catalog["@id"] = f"{base}/index.jsonld"
        catalog["url"] = f"{base}/"
    return catalog

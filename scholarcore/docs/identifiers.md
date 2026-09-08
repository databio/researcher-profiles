# Canonical identifiers

For the identifier helper functions and their exact behavior, see the
[model reference](reference/models.md).

Every scholarcore entity has exactly one join key:

| Entity | Identifier | Assigned by |
|---|---|---|
| Person | `rid` | ORCID, or minted locally |
| Paper | normalized DOI, or PMID | the publisher / PubMed |
| Award | `application_id` | the system of record tracking the application |
| Opportunity | `opportunity_number` | the funding agency |

Two systems that both store the same identifier can join their records without
sharing a database, and without either system knowing the other exists.

## Person: `rid`

A researcher id is either a canonical ORCID or an explicitly-prefixed local id:

```
0000-0002-1825-0097           an ORCID
local:josiah-carberry-a3f19c  a locally-minted id
```

ORCIDs are validated by pattern and by their ISO 7064 MOD 11-2 check digit, so
a transposed digit is rejected rather than stored as a plausible-looking id for
a different person.

There is no separate `orcid` field. An ORCID is derived from the `rid` with
`orcid_of()`, which returns `None` for a local id. A single field cannot drift
out of sync with itself.

### Local ids

Most researchers in a real dataset have no ORCID, so `mint_local_rid()` builds
one from a display name plus six random hex characters:

```python
from scholarcore import mint_local_rid

mint_local_rid("Josiah Carberry")  # local:josiah-carberry-a3f19c
```

The hex suffix is generated once and stored. It is what keeps the id stable
when a person changes their name, and what keeps two people with the same name
apart. Minting a second local id for a person who already has one creates a
second identity for the same human.

The `local:` prefix contains a colon and lowercase letters, so it can never be
mistaken for an ORCID, whose pattern allows only digits and hyphens.

## Paper: normalized DOI

The same DOI is written many ways in practice:

```
10.1101/gr.example
https://doi.org/10.1101/gr.example
http://dx.doi.org/10.1101/gr.example
doi:10.1101/gr.example
```

`Paper.doi` normalizes on load, with any resolver prefix and scheme stripped, so
those forms collapse to one written form. Case is preserved: some sources
(OpenAlex among them) carry the publisher's original case, and consumers
depend on it. DOIs are case-insensitive, so compare with `.lower()`, or call
`normalize_doi(value, lowercase=True)`, when using one as a join key. PMIDs
normalize the same way, to bare digits.

Normalization happens in the model rather than in each consumer because a join
key that every caller has to remember to normalize is a join key that
eventually fails to match. A PMID carrying no digits loads as `None` instead of
raising, so one malformed record does not fail an entire import.

## Award: `application_id`

An award has several plausible keys: a grant number, an internal slug, a
funder's tracking number. `application_id` is the one scholarcore treats as
canonical, because it is the identifier already shared between the systems that
track a grant through its lifecycle. `Award.number` carries the award or grant
number separately.

## Opportunity: `opportunity_number`

The funder-assigned number for a published call, such as `PA-25-168`. It is
assigned by the agency, appears in the announcement, and is what applicants
quote, so no local identifier is needed.

## Organizations and affiliations

`Organization` and `Affiliation` have no canonical identifier of their own.
Organizations carry an optional `ror_id` from the
[Research Organization Registry](https://ror.org), which is the closest thing
to a canonical key, but many organizations and nearly all departments have
none, so an organization is identified by whatever fields a record happens to
carry.

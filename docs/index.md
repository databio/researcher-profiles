# Researcher Profiles

A portable format specification and SDK for researcher profiles: static,
self-contained bundles of files describing one researcher, served from any
HTTP host. The project has five parts.

- The [Specification](rp-spec/) is the published profile standard. It covers
  the format, conformance levels, and privacy tiers.
- The [Researcher Profile Python SDK](rp-sdk/) provides models, validation,
  serving, vector search, and persona methods.
- [scholarcore](https://github.com/databio/researcher-profiles/tree/master/scholarcore/docs)
  is the shared academic vocabulary (Person, Paper, Award, Opportunity). This
  repo publishes it too; it is documented separately.
- The [Researcher Profile React UI Library](rp-ui-lib/) is a React + TypeScript
  component library that renders the wire contract.
- The [Researcher Profile Browser](rp-browser/) is the runnable web app that
  composes rp-ui-lib with a data layer.

A profile holds much more than a bibliography. It records each paper at several
depths, from full text to a short abstract to a written summary, with a citation
graph and vector embeddings built over all of it. Around the papers it carries
grants and funding, expertise, career and training history, collaborators, and
an explicit list of what a researcher does not work on. It can also include a
persona document describing how a person thinks and writes, which a publication
list cannot capture.

## Scope

This project defines what a *published researcher profile* is and provides a
reference implementation for producing and consuming one.
The [Researcher Profile Specification](rp-spec/) is the definition.
The [Researcher Profile Python SDK](rp-sdk/) (`rp-sdk`) is the reference
implementation. It provides models, a CLI, a validator, an HTTP server, and
vector search, all built against the specification.

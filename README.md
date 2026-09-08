# researcher-profiles

researcher-profiles is a specification for describing a researcher as a
self-contained folder of structured files, plus the tools that read and serve
it: a Python SDK, an `rp` command-line tool, and a web API for sharing profiles
between tools.

A Researcher Profile (RP) captures a person's papers, expertise, funding, and
career history in far more depth than a publication list. Privacy is built into
the format. Every part of a profile carries a tier (`public`, `internal`, or
`restricted`) set by the author, and the sharing API enforces it, so a public
profile never exposes its internal or restricted parts.

The case driving it is AI. An agent can read a profile and understand what a
researcher knows and has done at a depth that is otherwise slow and expensive to
reach. It could pick paper reviewers on real evidence instead of a web search.
The format is general, though: a researcher can publish their own profile and
put far more in it than ORCID or Google Scholar will hold.

The `rp` command-line tool validates a profile against the spec, builds and
searches embeddings over its contents, renders a profile as a static site, and
installs or publishes profiles through a registry.

See the [format specification](docs/rp-spec/index.md).

This monorepo contains:

- [`spec/`](spec/): the specification prose (in [`docs/rp-spec/`](docs/rp-spec/index.md))
  and a set of example profiles, valid and invalid, that define what conforms.
- [`rp-sdk/`](rp-sdk/): the Python SDK and the `rp` CLI.
- [`scholarcore/`](scholarcore/): a shared academic vocabulary (Person, Paper,
  funding), published as its own package.
- [`rp-browser/`](rp-browser/): a browser app for viewing published profiles.
- [`rp-ui-lib/`](rp-ui-lib/): React components for profile views.

## Install

The two Python packages are not on PyPI yet. Install them from a checkout,
`scholarcore` first, because `rp-sdk` depends on it.

```bash
git clone https://github.com/databio/researcher-profiles.git
cd researcher-profiles
python -m venv .venv && source .venv/bin/activate
pip install -e ./scholarcore
pip install -e ./rp-sdk
```

Optional extras attach to the second command, for example
`pip install -e "./rp-sdk[vectors,st]"`.

See the [SDK documentation](docs/rp-sdk/index.md) for the API, the CLI, and the
optional extras. `scholarcore` has its own docs in
[`scholarcore/docs/`](scholarcore/docs/index.md).

## License

MIT. See [LICENSE](LICENSE).

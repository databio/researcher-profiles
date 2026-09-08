# rp-sdk

Python SDK and CLI for loading, validating, querying, serving, and storing researcher profiles.

## Install

The two Python packages (`scholarcore` and `rp-sdk`) are not on PyPI yet.
Install both from a checkout, `scholarcore` first, because `rp-sdk` depends on
it.

```bash
git clone https://github.com/databio/researcher-profiles.git
cd researcher-profiles
python -m venv .venv && source .venv/bin/activate
pip install -e ./scholarcore
pip install -e ./rp-sdk
```

Optional extras attach to the second command, for example
`pip install -e "./rp-sdk[vectors,st]"`.

See the [install guide](https://github.com/databio/researcher-profiles/tree/master/docs/rp-sdk/index.md) for optional extras (embeddings, HTTP API, SQL store, LLM persona).

## Quick start

```python
from researcher_profiles import ResearcherProfile

p = ResearcherProfile.from_files("path/to/jane-doe", eager=True)
p.name, p.papers, p.expertise
```

```bash
rp validate path/to/jane-doe
```

## Repository layout

| Path | What it is |
|------|-----------|
| `src/researcher_profiles/` | the SDK source and the `rp` CLI |
| `schemas/` | JSON Schema files exported from the models, for external tooling |
| `skills/` | agent instruction files (SKILL.md) that teach an LLM to publish, agent, or talk to a profile; see [`skills/README.md`](skills/README.md) |
| `scripts/` | docs-generation tooling that rebuilds the Python API reference; see [`scripts/README.md`](scripts/README.md) |
| `tests/` | the test suite |
| `AGENTS.md` | conventions for AI agents working in this repo |

## Documentation

- [Tutorial](https://github.com/databio/researcher-profiles/tree/master/docs/rp-sdk/tutorial.md)
- [Python API reference](https://github.com/databio/researcher-profiles/tree/master/docs/rp-sdk/reference/python-api.md)
- [CLI reference](https://github.com/databio/researcher-profiles/tree/master/docs/rp-sdk/reference/cli.md)
- [HTTP API reference](https://github.com/databio/researcher-profiles/tree/master/docs/rp-sdk/reference/api.md)
- [Format specification](https://github.com/databio/researcher-profiles/tree/master/docs/rp-spec/index.md)

## License

MIT. See [LICENSE](LICENSE).

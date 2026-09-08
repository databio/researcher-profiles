# Contributing to researcher-profiles

## Getting help and reporting issues

Please use the [GitHub issue tracker](https://github.com/databio/researcher-profiles/issues)
for bug reports, questions, and feature requests. That is the best place to
reach the maintainers.

## Repository layout

This is a monorepo:

- `rp-sdk/`: the Python package (`researcher-profiles`), which loads,
  validates, queries, and serves profiles.
- `scholarcore/`: the shared academic vocabulary package `rp-sdk` depends on.
- `spec/`: the conformance suite for the Published Researcher Profile (PRP)
  specification; the prose specification itself lives in `docs/rp-spec/`.
- `rp-browser/`, `rp-ui-lib/`: TypeScript web applications.

## Development

`scholarcore` must be installed first: `rp-sdk` depends on it and neither
package is on PyPI.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ./scholarcore
pip install -e "./rp-sdk[dev]"
./scripts/test-all.sh
```

The SDK uses [`ruff`](https://docs.astral.sh/ruff/) for linting (`ruff.toml` at
the repo root). Run the tests before submitting a change.

## Pull requests

- Keep changes focused, and include tests where relevant.
- Make sure `pytest` passes in `rp-sdk/`.
- Changes to the on-disk format must go through `spec/` and its conformance
  suite, not only the SDK.

## License

Contributions are MIT-licensed, like the rest of the project. See
[LICENSE](LICENSE).

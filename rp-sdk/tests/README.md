# tests

The `rp-sdk` test suite, run with [pytest](https://docs.pytest.org/).

```bash
pip install -e ".[dev]"   # the supported test install
pytest
```

Shared fixtures (example profile directories such as `jane-doe`) live in
`tests/fixtures/` and are wired up in `conftest.py`. `test_guardrails.py` pins
invariants the rest of the SDK relies on: that a plain `import
researcher_profiles` pulls in no heavy or network dependencies, and that the
committed `@context` bytes match their lock file.

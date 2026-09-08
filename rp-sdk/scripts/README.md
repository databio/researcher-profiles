# scripts

These scripts auto-regenerate documentation for `rp-sdk`; they are not part of the
SDK itself.

## Python API reference generator

`render_python_api.py` builds the Python API reference page
(`docs/rp-sdk/reference/python-api.md`) from the SDK's own docstrings. It reads
`python-api-directives.md`, a hand-maintained list of which classes and
functions appear in the reference and in what order, resolves each
`::: module.Class` directive with [griffe](https://mkdocstrings.github.io/griffe/),
and writes the rendered Markdown.

The output page is committed but generated. Do not hand-edit it. To change the
reference, edit the docstrings in `src/researcher_profiles/`, or add and remove
directives in `python-api-directives.md`, then re-run the generator:

```bash
pip install -e ".[docs]"          # provides griffe
python scripts/render_python_api.py
```

Pass `--check` to render in memory and compare against the committed file
without writing it, which is how CI verifies the reference is up to date.
Griffe parses source statically, so no runtime extras are needed to regenerate.

# Researcher Profile specification (non-docs assets)

The prose specification documents live in `docs/rp-spec/`. This directory holds
only the non-documentation assets:

- [conformance/](conformance/): the shared conformance corpus. It holds valid
  and invalid profiles plus `cases.json`, run through both
  validators by `.github/workflows/conformance.yml`. Their verdicts must
  agree.

The consumer skill is not mirrored here. It ships inside the Python package at
`rp-sdk/src/researcher_profiles/skill/`, which is the only copy: `rp skill
--install` writes it, and `rp-browser/scripts/copy-skills.mjs` publishes it to
the browser app, which is how a deployment serves it at `/skills/researcher-profile/`.

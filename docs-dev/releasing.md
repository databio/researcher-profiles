# Versioning and releases

This repository holds several parts that change at different speeds: the
Python SDK, the shared models package, the browser app, the UI library, and the
specification. Each part has its own version number and its own release tag,
so a release of one part does not force a release of the others.

## Versioning scheme

Every part follows [Semantic Versioning](https://semver.org/). Before 1.0, a
minor bump (`0.1.0` -> `0.2.0`) may include breaking changes, and a patch bump
(`0.2.0` -> `0.2.1`) is for fixes only.

Versions are independent. A part's number moves only when that part changes,
and the parts do not need to share a number.

| Part | Where the version lives | Tag |
|---|---|---|
| `rp-sdk` (the `researcher-profiles` Python package) | `rp-sdk/pyproject.toml` and `CITATION.cff` | `vX.Y.Z` |
| `scholarcore` | `scholarcore/pyproject.toml` | `scholarcore-X.Y.Z` |
| `rp-browser` | `rp-browser/package.json` | `browser-X.Y.Z` |
| `rp-ui-lib` | `rp-ui-lib/package.json` | `ui-lib-X.Y.Z` |
| Specification | the top heading of `docs/rp-spec/CHANGELOG.md` | `spec-X.Y.Z` |

`CITATION.cff` describes the repository as a whole, and its `version` tracks
`rp-sdk`.

The spec version is not the context version. A profile's `conformsTo` names the
context major version (the IRI ending in `context/v1.jsonld`). The spec version
tracks revisions of the spec text and the conformance suite within that major
version.

## Changelogs

- `CHANGELOG.md` at the repository root covers the packages. It uses the
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format, with one
  heading per release and one subheading per part that changed.
- `docs/rp-spec/CHANGELOG.md` covers the specification. It is published with
  the spec docs.

Add entries under `## [Unreleased]` as you work. At release time, rename that
heading to the version and date.

## How to make a release

Work happens on `dev`. A release moves `dev` onto `master` and tags the result.

1. **Decide which parts to release.** List the parts that changed since their
   last tag, for example `git diff --stat v0.2.0 dev -- rp-sdk`. Release only
   those parts.
2. **Bump versions on `dev`.** Update each released part's version file from
   the table above. For `rp-sdk`, also update `CITATION.cff`.
3. **Finish the changelogs on `dev`.** Rename `[Unreleased]` to the new version
   and today's date in `CHANGELOG.md`. For a spec release, add the new version
   heading to `docs/rp-spec/CHANGELOG.md`.
4. **Check that CI passes on `dev`.**
5. **Squash-merge `dev` into `master`.**

   ```bash
   git checkout master
   git merge --squash dev
   git commit -m "Release rp-sdk 0.2.0, spec 0.2.0"
   ```

6. **Reset `dev` to `master`.** A squash merge leaves `dev`'s separate commits
   out of `master`'s history. Without this step, the next merge would try to
   apply them again.

   ```bash
   git checkout dev
   git reset --hard master
   git push --force-with-lease origin dev
   ```

7. **Tag the release commit on `master`,** one tag per released part, and push
   the tags.

   ```bash
   git tag v0.2.0 master
   git tag spec-0.2.0 master
   git push origin master v0.2.0 spec-0.2.0
   ```

8. **Create a GitHub release for each tag.** Paste that part's changelog entry
   as the notes. Create it as a draft, check it, then publish.

   ```bash
   gh release create v0.2.0 --draft --title "rp-sdk 0.2.0" --notes-file notes.md
   ```

## Publishing

Nothing is published to PyPI or npm yet. A tag marks the release in git and on
GitHub only. Install from a clone, with `scholarcore` installed before
`rp-sdk` (see `CONTRIBUTING.md`).

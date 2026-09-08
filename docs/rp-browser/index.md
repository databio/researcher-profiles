# RP Browser

A static React web app for browsing and searching remotely-hosted researcher profiles.

You can browse researchers' expertise, publications, and background; search by
topic to find researchers working on specific problems (the search runs in your
browser, with no server); and paste a profile URL to check that it is published
correctly.

Run it locally with `npm run dev`.

## Running it

```bash
cd rp-browser
npm install
npm run dev          # Vite dev server on http://localhost:5185
```

To test against locally-published profiles, run `rp render` and `rp site`
first (see the [rp-sdk docs](../rp-sdk/)), then serve the output directory and
add it as a source in rp-browser.

The [rp-browser README](../../rp-browser/README.md) covers the source model,
client-side validation, the local embedding models, environment variables,
and deployment.

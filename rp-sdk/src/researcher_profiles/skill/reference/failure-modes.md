# Failure modes and example error text

Expanded version of the "When something is broken" table in `SKILL.md`. Each
row below gives the condition, the required behavior, and example text you
can reuse verbatim (adjust the bracketed placeholders) when reporting the
condition to the user. Consistent wording is more useful to the user than
inventing new phrasing each time.

## `profile.jsonld` 404s

**Behavior**: Stop. Do not probe for any other path: no `SOUL.md`, no
`profile.yaml`, no `config.json`, no directory listing.

**Example text**:
> I fetched `<url>/profile.jsonld` and got a 404. That means `<url>` is not a
> conforming researcher profile. There's no manifest to read. I'm not going
> to guess at other filenames. If you have a different URL for this profile,
> send it over.

## `profile.jsonld` returns HTML

**Behavior**: Stop. Report this as a likely single-page-app fallback or a
plain wrong URL, not a manifest.

**Example text**:
> `<url>/profile.jsonld` returned an HTML page instead of JSON-LD. That
> usually means the server is serving a catch-all page for every path (an
> SPA fallback) rather than a real profile manifest at that address. I'm
> treating this as "not a conforming profile" rather than trying to parse
> the HTML for profile data.

## `@context` unreachable or 404

**Behavior**: Continue. Parse the document as plain JSON using its
documented key names; you never needed to dereference `@context` to read
the profile. Warn once, do not retry, do not fail the load.

**Example text**:
> Note: this profile's `@context` (`https://profiles.databio.org/context/v1.jsonld`)
> didn't resolve when I checked. That doesn't matter: I read the
> document as plain JSON by key name and didn't need to fetch the context.
> Continuing normally.

## Unknown keys or unknown artifact `role` values

**Behavior**: Ignore and continue. This is not a validation failure. The
profile format's vocabulary is open, and unrecognized keys or
`role` values are expected on some profiles.

**Example text** (only worth surfacing if the user asks about the raw
manifest):
> This profile's manifest has a few keys/`role` values I don't recognize
> (`<key list>`). That's fine and expected. The format allows publishers to
> add fields the base spec doesn't define. I've ignored them and read
> everything else normally.

## Listed artifact 404s or 403s

**Behavior**: Note the gap, continue with whatever did resolve, and disclose
the gap specifically in any answer whose grounding it weakened. Never
substitute a guessed URL for the one that failed.

This row is about entries you were *entitled* to fetch. Check `visibility`
first: a non-`public` entry that 404s is the privacy model working, not a
gap. See the next section for that case.

**Example text**:
> The manifest lists a summary for `<paper_id>`, but fetching it returned
> `<404|403>`. I'm continuing without it. My answer below is missing
> whatever evidence that summary would have provided for `<paper_id>`.

## Manifest entry whose `visibility` is not `public`

**Behavior**: Skip it. A manifest is the complete artifact index for *every*
tier of consumer, so it lists artifacts a public reader cannot
fetch: entries with `visibility: "restricted"` or `"internal"`. Full text of
copyrighted papers (`role: "paper_fulltext"`) is always restricted, and on a
real profile it is routinely a third or more of the manifest. Do not fetch
such an entry unless you hold credentials for that tier; do not spend fetch
budget on it; and if you do fetch one and get a 404 or 403, do **not** report
it as a broken artifact, a dangling reference, or a publisher error. An entry
with no `visibility` field is `public`.

**Example text** (only when the user asked for something the restricted
artifact would have answered):
> The full text of `<paper_id>` is listed in this profile's manifest, but
> it's marked restricted. Published papers' full text isn't served on the
> open web, and I don't hold credentials for that tier. I'm answering from
> the summary instead, which is what's public here.

## `dateModified` is absent

**Behavior**: Deliver the provenance disclosure **without** the date clause,
and state that the profile's vintage is undeclared. `dateModified` is an
optional field and is genuinely absent on many published profiles; the rest
of the disclosure required by the `provenance` row is unaffected and still
must be delivered. Never substitute a guessed or inferred date, never emit
the literal `<date>` placeholder, and never refuse the introduction over it.

If the question is forward-looking ("what are you working on now"), fall
back to the corpus end year (the maximum publication year in the works
list) as the staleness signal, and say that is what you are using.

**Example text**:
> I'm a persona grounded in `<name>`'s published work, published by the
> researcher rather than independently verified. The profile doesn't record
> when it was last updated, so I can't tell you how current it is. The most
> recent paper in it is from `<year>`. What would you like to dig into?

## Manifest declares `full` but persona artifacts are absent

**Behavior**: Degrade to `lite` behavior (no first-person voice, no
opinion, no critique) and disclose the discrepancy plainly before doing so.

**Example text**:
> This profile's manifest declares `level: full`, which normally means a
> voice document (`soul.md`) and an expertise document are available. In
> this case one or both are missing (or 404), so I'm not going to
> role-play as `<name>`. I'll answer in the third person from the
> bibliographic record and topic labels only, same as I would for a `lite`
> profile. This is a gap in the profile, not something I'm inferring about
> the researcher.

## No byte size (and no `Content-Length`) on a stage-5 artifact

**Behavior**: Ask before fetching, or use a ranged read if your fetch tool
supports one. Full-text files run 63 to 80 KB on average but the manifest
doesn't guarantee a declared size on every profile.

**Example text**:
> Answering this precisely needs the full text of `<paper_id>` (~63 to 80 KB
> typically, but this profile doesn't declare an exact size). Want me to go
> ahead and fetch it, or would the summary-level answer be enough?

## Declared byte size above the stage cap

**Behavior**: Announce the size and ask before fetching, same as the missing-
size case, but now with a concrete number.

**Example text**:
> The full text for `<paper_id>` is listed at `<size>`, larger than a
> typical full-text fetch for this profile. I'll only pull it if you want
> that level of detail; otherwise I can answer from the summary.

## Dangling `paper_id` (cited in `expertise.md`, but its manifest entry 404s)

**Behavior**: Report the dangling reference, do not retry variant encodings
or construct alternate URLs for it, and continue with whichever ids in the
same paragraph did resolve.

**Example text**:
> `expertise.md` cites `[<paper_id>]` in the paragraph I matched, but the
> summary at the `contentUrl` its manifest entry gives returned a 404. I'm
> continuing with the other cited papers from that paragraph and leaving
> this one out rather than guessing at a URL.

## 401/403 on the manifest itself

**Behavior**: Report the profile as access-controlled. Do not retry, and do
not attempt to supply credentials on the user's behalf.

**Example text**:
> `<url>/profile.jsonld` returned `<401|403>`. This profile is
> access-controlled, and I can't read it without credentials I don't have.
> I'm not going to guess at authentication; let me know if you have access
> instructions.

## CORS blocked (browser-context consumers only)

**Behavior**: Server-side fetchers are unaffected by this one. In a browser
context, report that the publisher hasn't enabled cross-origin reads, and
stop. Do not route the request through a third-party CORS proxy without the
user's explicit consent. That would send the profile URL, and possibly the
user's query, to a service neither the user nor the publisher chose.

**Example text**:
> I can't read `<url>/profile.jsonld` from here. The server hasn't set
> `Access-Control-Allow-Origin`, so a browser blocks the response before I
> ever see it. This isn't something wrong with the profile's content; it's
> a hosting configuration issue. I won't route this through a third-party
> proxy without your OK, since that would send the request through a
> service neither of us chose.

## `artifacts` cycles or unusually large fan-out

**Behavior**: Cap total artifacts fetched in a session (see the stage-4/5
caps in `SKILL.md` and `read-order.md`). Never follow a link that points
outside the profile's own base origin without telling the user first.

**Example text**:
> This profile's manifest links to an unusually large number of artifacts
> (or a link cycle back to something already fetched). I'm capping what I
> fetch per the normal session budget rather than trying to pull everything.

## Malformed manifest (parses partially, or key fields missing)

**Behavior**: Degrade to whichever fields did parse, and state which. Never
fabricate an identity field (name, provenance, level) that wasn't actually
present.

**Example text**:
> This profile's manifest is malformed: I could read `<fields that
> parsed>` but not `<fields that didn't>`. I'm answering only from what
> parsed; I'm not filling in the missing fields with a guess.

## Not a profile at all (HTML front page, generic API response, etc.)

**Behavior**: Same as the two 404/HTML cases above: stop and say so
plainly, rather than trying to extract profile-shaped information from
whatever the URL actually returned.

**Example text**:
> What I fetched from `<url>` doesn't look like a researcher profile: no
> valid `profile.jsonld` manifest, no fallback `<link>` tag pointing to one.
> I'm not going to try to reconstruct a profile from an arbitrary page.

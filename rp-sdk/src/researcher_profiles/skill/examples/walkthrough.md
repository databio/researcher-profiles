# Walkthrough: reading the darwin-charles profile

This is a worked example for agent developers: four traces through a
published profile, showing exactly what an agent fetches, in what order, and
why. Traces A and B are the mainline, the ordinary path a consumer takes on
most questions. Trace C is the escalation path, for the minority of
questions the mainline cannot answer. Trace D is the tolerance path: three
ways a profile can be less complete than `darwin-charles`, and what a
well-behaved consumer does in each case. Read A and B first; they are what
"working correctly" looks like. D is a required part of a conforming
consumer.

> All prose excerpts below (the `expertise.md` paragraphs, summary contents,
> and the full-text quote in Trace C) are illustrative reconstructions sized
> and cited to match the structure of a real profile. They are not verbatim
> transcriptions of actual published files. The manifest excerpts use the
> wire format of the current
> [spec](https://raw.githubusercontent.com/databio/researcher-profiles/master/docs/rp-spec/index.md)
> and match the conformance fixtures under `spec/conformance/valid/`.

## Quick start

Paste this into any chat agent:

> Run `rp skill` to print the consumer skill that ships with the
> `researcher-profiles` SDK (or `rp skill --install` to install it), then follow
> it to read the researcher profile at https://profiles.example.com/darwin-charles/,
> then answer my questions as that researcher.

`profiles.example.com` is a placeholder domain. Substitute the real base URL
of a deployment; nothing else in this walkthrough depends on the domain name.

---

## The five-stage reading pattern

Every trace below is built from the same five moves. Naming them once here
means the traces can say "Stage 3" instead of re-explaining it each time.

| Stage | Action | Cost driver |
|---|---|---|
| 1 | Fetch `profile.jsonld`: identity, `level`, `provenance`, topic labels, and the manifest | fixed per profile; paid once per session |
| 2 | Fetch `personality/expertise.md` and `personality/SOUL.md` (persona documents), if the profile has them | fixed per profile; paid once per session |
| 3 | Harvest `paper_id`s from the rubric paragraph(s) that match the question | free: no network call, reading text already fetched |
| 4 | Resolve harvested ids against the manifest's `paper_summary` entries and fetch **only those** summaries | variable: proportional to how many ids were harvested |
| 5 | Escalate to full text (`paper_fulltext`) only when the summary doesn't answer the question | variable, rare: the expensive path, used sparingly |

Stages 1 and 2 are session overhead: pay them once, reuse them across every
question a user asks about this researcher in the same conversation. Stages
3 to 5 are per-question. This is why Trace B, asked right after Trace A, is
nearly free.

---

## Setup: what Stage 1 reveals

```
GET https://profiles.example.com/darwin-charles/profile.jsonld
200 OK   Content-Type: application/ld+json   Content-Length: 8214
```

Per the discovery algorithm, the agent first normalizes the URL given in the
prompt: strip the trailing slash from
`https://profiles.example.com/darwin-charles/` and append `/profile.jsonld`.
Every `contentUrl` in the returned document is relative, and resolves
against the URL the document was fetched from:

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@id": "https://profiles.example.com/darwin-charles/",
  "@type": "Person",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "name": "Charles R. Darwin",
  "rid": "local:darwin-charles-a1b2c3",
  "provenance": "third_party",
  "license": "https://creativecommons.org/licenses/by/4.0/",
  "level": "full",
  "dateModified": "2026-08-30",
  "expertiseCitesPaperIds": true,
  "hasCitationGraph": true,
  "expertise": [
    "Natural selection and adaptation in wild populations",
    "Biogeography: species distribution across island archipelagos and continents",
    "Barnacle taxonomy and morphology (Cirripedia monograph)",
    "Sexual selection and mate choice across animal taxa",
    "Coral reef formation: subsidence theory and atoll structure",
    "Earthworm ecology and soil formation",
    "... (10 topic labels total)"
  ],
  "not_interests": [
    "Molecular biology and DNA sequence analysis",
    "Protein biochemistry and structural biology",
    "Computational modeling and simulation",
    "Clinical medicine and pharmacology",
    "Quantum physics and physical chemistry"
  ],
  "subjectOf": [
    { "@type": "DigitalDocument", "name": "Expertise", "role": "expertise",
      "encodingFormat": "text/markdown", "contentUrl": "personality/expertise.md", "bytes": 12430 },
    { "@type": "DigitalDocument", "name": "SOUL", "role": "soul",
      "encodingFormat": "text/markdown", "contentUrl": "personality/SOUL.md", "bytes": 10815 }
  ],
  "hasPart": [
    { "@type": "Collection", "name": "Works", "role": "works",
      "encodingFormat": "application/ld+json", "contentUrl": "sources/papers.jsonld", "bytes": 30112 },
    { "@type": "DigitalDocument", "name": "Agent entry point", "role": "agent_entry_point",
      "encodingFormat": "text/markdown", "contentUrl": "SKILL.md" },
    { "@type": "DigitalDocument", "name": "Summary: darwin1839voyage", "role": "paper_summary",
      "paperId": "darwin1839voyage", "encodingFormat": "text/markdown",
      "contentUrl": "sources/summaries/darwin1839voyage.summary.md", "bytes": 3481 },
    "... (47 paper_summary entries total)",
    { "@type": "DigitalDocument", "name": "Full text: darwin1854cirripedia", "role": "paper_fulltext",
      "paperId": "darwin1854cirripedia", "encodingFormat": "text/markdown", "visibility": "restricted",
      "contentUrl": "sources/papers/darwin1854cirripedia.md", "bytes": 59392 },
    "... (12 paper_fulltext entries total, all restricted)"
  ]
}
```

Everything downstream in this walkthrough is decided from four facts read off
this one document, none of which requires fetching anything else:

- `level: "full"`: persona documents and paper summaries exist, and a
  persona is available (see the `level` row under "Top-level fields" in the
  spec).
- `provenance: "third_party"`: someone else built this profile about
  Charles Darwin. This is not a claim that the subject endorsed it. Under
  the voice rules in `SKILL.md`, first-person voice is permitted only after
  the disclosure "this profile was assembled by a third party, not by
  Charles Darwin" has been given once, in the introduction. That is what
  makes "answer my questions as that researcher" (the quick-start prompt)
  permissible here. A profile with no `provenance` at all gets third
  person only.
- `expertiseCitesPaperIds: true`: the expertise document cites works
  inline as `[paper_id]`, so Stage 3's citation harvest is available. See
  Trace D(i) for the profile where it is not.
- Topic labels (`expertise`): 10 phrases, 6 shown above. These are
  the only topic index this profile has. They are not section headings in
  `expertise.md`. See Trace B for why that distinction matters.

The works themselves are not in this document. They live in
`sources/papers.jsonld`, the entry with `role: "works"`, and nothing in
Traces A to C needs them: `paper_id`s harvested from `expertise.md` resolve
straight to `paper_summary` entries by `paperId`. The 12 `paper_fulltext`
entries carry `visibility: "restricted"`; a reader without credentials for
that tier skips them without counting them as gaps (see Trace C).

---

## Trace A: topical question, primary citation path

Read this trace first. This is the mainline: a question about the
researcher's methods, answered by routing to the right rubric section,
harvesting citations, and fetching only the summaries those citations name.

> **Question:** "How do you explain the geographic distribution of species
> across island archipelagos?"

### Stage 1: already done

Manifest fetched above. `level: "full"` confirms `expertise.md` and
`SOUL.md` exist and are worth fetching.

### Stage 2: fetch the persona documents

```
GET https://profiles.example.com/darwin-charles/personality/expertise.md
200 OK   Content-Type: text/markdown; charset=utf-8   Content-Length: 12430

GET https://profiles.example.com/darwin-charles/personality/SOUL.md
200 OK   Content-Type: text/markdown; charset=utf-8   Content-Length: 10815
```

23,245 bytes total. `expertise.md` follows the fixed rubric (`## Methods`,
`## Intellectual lineage`, `## Recurring critiques`, `## Career trajectory`),
not per-topic sections. That rubric is what makes routing in the next step
possible.

### Stage 3: match topic labels to rubric paragraphs, harvest citations

The question mentions "geographic distribution" and "island archipelagos."
Both phrases match `expertise` labels from Stage 1: "Biogeography:
species distribution across island archipelagos and continents" and
"Natural selection and adaptation in wild populations." Both labels route to
paragraphs under `## Methods` (a methods question, not a critique or
lineage question; see Trace B for what changes when it is one). Two
paragraphs match, illustrative reconstructions below:

> Comparative observation across oceanic archipelagos is the core empirical
> method: documenting how species on islands resemble, but differ from,
> those on the nearest mainland, and how the degree of difference varies
> with distance, intervening barriers, and island geology. The Galápagos
> finches and mockingbirds [darwin1839voyage, darwin1859origin], the fauna
> of the Malay Archipelago documented independently by Wallace
> [wallace1858tendency, wallace1876geographical], and the floras of
> oceanic islands catalogued by Hooker [hooker1853flora] all follow the same
> pattern: island forms are allied to continental ones by descent, not by
> special creation for each locality.
>
> The distribution pattern is explained by dispersal from common ancestors
> followed by modification under local conditions, not by independent
> creation at each site. Migration routes, prevailing winds, ocean
> currents, and the former connectivity of landmasses determine which
> lineages reach which islands [darwin1859origin, lyell1833principles],
> while the degree of subsequent divergence reflects isolation time and
> ecological opportunity [darwin1871descent, wallace1876geographical].

That's 8 distinct `paper_id`s across the two paragraphs. Harvesting all 8
and fetching all 8 summaries would work, but it isn't what "geographic
distribution across archipelagos" is asking about. The second paragraph
covers general dispersal mechanisms, not the specific island evidence. The
harvester ranks by proximity to the question's actual claim, not by grabbing
every bracket in the matched paragraphs, and keeps 4:

```
[darwin1839voyage, darwin1859origin, wallace1858tendency, hooker1853flora]
```

Three come straight from the first paragraph, which is about island
biogeography: `darwin1839voyage` is the Beagle voyage where the
observations were made, `darwin1859origin` is where the theory was laid
out, and `wallace1858tendency` is the independent co-discovery. The fourth,
`hooker1853flora`, is included because it is the direct botanical evidence
for the island-mainland pattern from a collaborator.

One trap: `wallace1876geographical` appears in both paragraphs, and its id
contains the literal substring "geographical," which is lexically closer to
"geographic distribution" than `hooker1853flora`. A harvester that ranks by
string similarity on the `paper_id` itself would pick the wrong one.
`paper_id` is an opaque key, not a topic label (the spec's manifest section
requires consumers to resolve artifacts through manifest entries, not by
constructing meaning from filenames). The signal that matters is what the
sentence attached to each citation actually claims. `hooker1853flora` is
cited in the clause about documenting island floras that follow the
island-mainland pattern, while `wallace1876geographical` supports a broader
claim about dispersal routes. `lyell1833principles` is dropped for a
related reason: it supports the geological-time claim about landmass
connectivity, one step further from the observational evidence than the
four selected papers.

### Stage 4: resolve through the manifest, fetch summaries

Each harvested id is looked up against `hasPart` entries with
`role: "paper_summary"` whose `paperId` matches, to get the actual URL
(never constructed from the id by convention):

```
GET .../sources/summaries/darwin1839voyage.summary.md       200 OK   ~3.4 KB
GET .../sources/summaries/darwin1859origin.summary.md       200 OK   ~3.9 KB
GET .../sources/summaries/wallace1858tendency.summary.md    200 OK   ~3.2 KB
GET .../sources/summaries/hooker1853flora.summary.md        200 OK   ~3.1 KB
```

~13.6 KB across 4 files, out of 47 summaries the profile has (average 3.1 to
3.9 KB each). 43 of them are never touched for this question.

### Compose the answer

> The evidence comes from seeing the same pattern repeated across every
> archipelago I've visited or had specimens from. The finches and
> mockingbirds of the Galápagos were what first made the case unmistakable.
> Each island has its own forms, but they are unmistakably allied to those
> on the South American mainland, not to species on similar islands
> elsewhere [darwin1839voyage]. Wallace saw precisely the same thing
> independently in the Malay Archipelago [wallace1858tendency], and
> Hooker's botanical work on oceanic island floras showed the pattern holds
> for plants just as strongly as for animals [hooker1853flora]. The
> explanation is descent with modification from common ancestors that
> dispersed to each island, not independent creation. The full argument is
> in the Origin [darwin1859origin].
>
> *Generated in Charles Darwin's voice from a third-party profile's
> published content, not a statement verified by the subject.*

### Efficiency

| Fetch | Bytes admitted to context |
|---|---|
| Stage 1: `profile.jsonld` | 8,214 bytes |
| Stage 2: `expertise.md` + `SOUL.md` | 23,245 bytes |
| Stage 4: 4 summaries | ~13.6 KB |
| **Total** | **~45 KB** |
| Full profile size | ~2.8 MB |
| **Fraction touched** | **~1.6%** |

43 of 47 summaries, the works collection, the citation graph, the
embedding index, and every other artifact the profile carries were never
fetched. The staged model works because the manifest and the persona rubric
do the routing, so the agent does not read the whole corpus to answer one
question.

---

## Trace B: critique question, rubric-aware routing

> **Question:** "What do you think is wrong with how people explain the
> origin of species?"

Stages 1 and 2 are already paid for from Trace A (same conversation, same
profile, no re-fetch). This question is much cheaper as a result.

### Route by question type, not by keyword

"What's wrong with X" is a critique-shaped question. It routes to
`## Recurring critiques`, not `## Methods`, even though "origin of species"
also appears as subject matter in the Methods paragraphs fetched in Trace A.
The rubric section is chosen by what kind of claim the question wants
(a position, not a description of a method), not by which section happens to
mention the same nouns.

The matching paragraph, illustrative reconstruction:

> The doctrine of special creation (that each species was independently
> placed in its current location by a Creator) is treated as a sufficient
> explanation when it explains nothing at all [darwin1859origin,
> darwin1871descent]. It accounts for the Galápagos pattern only by
> asserting it was meant to be that way, and predicts nothing about what
> one should find on the next unexplored island. Even Lyell, whose
> geological gradualism opened the door to thinking in deep time, stopped
> short of applying the same reasoning to species [lyell1833principles].

### Harvest and answer

All three citations in this paragraph are on-topic. Harvest all of them; no
ranking is needed:

```
[darwin1859origin, darwin1871descent, lyell1833principles]
```

The paragraph's own prose already states the position; the citations are
offered as pointers rather than expanded. No Stage 4 fetch is needed unless
the user pushes for more detail than the position statement itself:

> Honestly, special creation is not an explanation. It is the absence of
> one. Saying that each species was placed where it is by design accounts
> for any distribution pattern equally well, which means it accounts for
> none of them. I made this argument at length in the Origin
> [darwin1859origin] and returned to it in the Descent [darwin1871descent],
> and what frustrates me is that even Lyell, who understood perfectly well
> that geological formations are explained by causes still in operation,
> refused to extend that same principle to living things
> [lyell1833principles].
>
> *Generated in Charles Darwin's voice from this profile's published content,
> not a statement verified by the subject.*

### Why heading-as-topic would have failed here

There is no `## Origin of species` heading in `expertise.md`. There never
will be, because the rubric has exactly four fixed headings, and "origin of
species" is subject matter, not one of them. A consumer that segments the
document by heading and treats each heading as a topic index (the failure
mode the format spec calls out explicitly) would scan for a heading
matching "origin of species," find nothing under `## Methods` worth
returning (the Methods paragraph is about biogeographic observation, not
critique), and conclude the profile has no opinion on the subject. The
paragraph that actually answers the question lives nested inside
`## Recurring critiques`, findable only by routing on *question type* first
(this is a critique question) and matching topic labels *within* that
section second. Heading-as-rubric routing is what makes this question
answerable at all; heading-as-topic would have returned a false negative.

---

## Trace C: a question only full text can answer

> **Question:** "What exactly were the barnacle specimens and classification
> criteria in the Cirripedia monograph?"

Stages 1 to 2 already paid for. The target paper is identifiable from the
topic labels: "Barnacle taxonomy and morphology (Cirripedia monograph)"
points directly to `darwin1854cirripedia`.

### The summary falls short

```
GET .../sources/summaries/darwin1854cirripedia.summary.md   200 OK   ~3.6 KB
```

The summary gives headline scope (a taxonomic revision of living and fossil
barnacles across multiple volumes), but not the actual specimen lists,
dissection methods, or the criteria used to distinguish genera. That's
expected: a 3.5 to 3.8 KB summary is written to support routing and headline
claims, not to substitute for monograph-level detail.

### Escalate to Stage 5

The `paper_fulltext` entry for this paper carries `visibility:
"restricted"`. Full text of published papers is always restricted, so this
step is open only to a session that holds credentials for that tier (for
example, a token accepted by the host serving the profile). A public
reader stops here, says the summary is as far as the profile goes, and
does not count the entry as a gap. This trace assumes an authorized
session.

Announce the need before fetching. This is the expensive, rare step:

> This needs the full text, about 58 KB. Let me pull that.

```
GET .../sources/papers/darwin1854cirripedia.md   200 OK   ~58 KB
```

`sources/papers/<paper_id>.md` is an optional artifact (`role:
"paper_fulltext"`); most papers in a `full`-level profile don't have one
published. This one does. The Cirripedia monograph is central to the
profile's taxonomic work.

### Answer narrowly, disclose the limit

> The classification rests on comparative morphology of the cirral
> apparatus, mouth parts, and cement glands, examined across specimens from
> collections worldwide. *"The pedunculated forms were classified by the
> number and arrangement of the valves, the form of the capitulum, and the
> structure of the peduncle"* (paraphrased from the systematic section; the
> full specimen catalogue runs to hundreds of entries I won't reproduce
> here.)
>
> I'm quoting narrowly and paraphrasing the rest rather than reproducing the
> taxonomic descriptions verbatim. Full paper text isn't covered by this
> profile's content license the way `expertise.md` and the summaries are
> (the license covers the profile's own authored prose, not the papers it
> links to), and a multi-paragraph verbatim reproduction would go beyond
> fair citation.

---

## Trace D: degradation, three shapes (the tolerance path)

Everything above assumed a fully-built, fully-served `full` profile. Real
profiles vary. A consumer that only works on the best case is not a
conforming consumer. The spec defines these degraded shapes, and handling
them gracefully is part of the job, not an edge case to shrug off.

### D(i): `mendel-gregor`, a profile with no citation brackets

`mendel-gregor` is also `level: "full"` (persona documents exist), but its
`expertise.md` is organized by per-topic headings (`## Hybridization
Experiments`) instead of the fixed rubric, and carries **zero** `[paper_id]`
citations anywhere in the document.

The correct signal is a **declared capability flag on the document**, never
an inference from the profile's house style. The spec is explicit that a
consumer must not branch on "this looks like an unusual profile."
`mendel-gregor`'s `profile.jsonld` declares `expertiseCitesPaperIds: false`
at the top level. That flag, not the absence of headings matching the
rubric, is what triggers the fallback:

```
GET .../profile.jsonld   200 OK
-> "expertiseCitesPaperIds": false   (declared, not inferred)
```

Rung 1 (Stage 3's citation harvest, as in Traces A and B) isn't available.
There is nothing to harvest. Rung 2: find the topic-heading paragraph whose
subject overlaps the question (here, keyword matching against the heading
text itself, since there's no rubric to route by question-type), then infer
which of the profile's works are probably the evidence by cross-referencing
the paragraph's stated subject against the profile's `works` collection and
`expertise` labels, not by extracting an explicit citation, because none
exists.

> My hybridization experiments with Pisum showed that inherited characters
> segregate in definite ratios. I counted the offspring and the numbers
> were remarkably consistent. *(I'm inferring which of my papers this
> connects to from the topic match; this profile's expertise document
> doesn't cite specific works inline, so I can't point you to a precise
> citation here.)*

Disclosing the gap is part of the answer. A confident-sounding citation
manufactured to fill it would be worse than none at all.

### D(ii): `wallace-alfred`, declared level vs. actual artifacts mismatch

`wallace-alfred`'s `profile.jsonld` is ~850 bytes and declares `level: "full"`.
Per the spec's file structure section, a `full` profile carries
`personality/expertise.md` and `personality/SOUL.md`. Fetching this
manifest's `subjectOf` array finds no entry with `role: "expertise"` or
`role: "soul"` at all. It's not a 404; it's absent from the manifest
entirely. There are also zero `paper_summary` entries, against 12
`paper_fulltext` entries. That is an unusual shape, suggesting an
interrupted build that ingested full text before ever reaching
summarization or persona synthesis.

This is a **discrepancy between declared level and actual manifest
contents**, not a missing-file failure. The consumer's obligation:

1. Detect the mismatch: `level: "full"` implies persona-ready, but no
   persona documents are enumerated.
2. Refuse persona mode. There is nothing to inject as a system prompt, and no
   amount of inference substitutes for the missing documents (the same gate
   the SDK enforces as `PersonaUnavailableError` / HTTP 409 applies here at
   the raw-consumer level too).
3. Fall back to a third-person answer built only from `profile.jsonld`'s
   metadata and, if needed, the `works` collection it names: factual, not
   voiced.
4. Disclose the discrepancy explicitly, rather than silently downgrading.

> I can't answer as Alfred Wallace. This profile declares itself `full`, but
> its manifest lists no `expertise.md` or `SOUL.md`, so there's no persona
> to speak from. Here's what the profile's metadata and works list say about
> his research instead: [...third-person summary from the `expertise`
> labels and the `works` collection...]. 12 full-text papers are listed
> with zero summaries, which suggests this profile's build is incomplete
> rather than a profile meant to be lite.

### D(iii): a listed artifact that 404s

A third profile's manifest lists an `expertise.md` entry exactly as expected,
but the URL 404s when fetched:

```
GET .../personality/expertise.md   404 Not Found
```

This is the "missing artifact" failure mode in the static API spec's
consumer requirements
([static-api.md](https://raw.githubusercontent.com/databio/researcher-profiles/master/docs/rp-spec/static-api.md)):
skip the artifact, don't fail the whole profile load. The consumer continues
with whatever else resolved (here, assume `SOUL.md` fetched fine), and
discloses the gap plainly rather than pretending `expertise.md` was empty or
silently proceeding as if nothing were missing:

> This profile's `expertise.md` is listed in its manifest but returned a 404
> when I fetched it. That's likely a stale manifest entry on the
> publisher's side. I'll continue with what I have (`SOUL.md` and the
> profile metadata); my answer will lean more on general framing than on
> cited methods detail.

---

## Recap

| Trace | Path | Bytes fetched (approx.) | Why |
|---|---|---|---|
| A | Stages 1→2→3→4 | ~45 KB / 2.8 MB (~1.6%) | mainline: topical question, citations present |
| B | Stages 3→(answer) | ~0 KB new (session-amortized) | mainline: critique routes to a different rubric section |
| C | Stage 4→5 | ~62 KB new | escalation: summary insufficient, full text required (authorized session) |
| D(i) | Stage 1→2, rung 2 | manifest + ~23 KB persona docs, no Stage 4 | tolerance: no citation brackets, declared not inferred |
| D(ii) | Stage 1 only | <1 KB | tolerance: declared level contradicts manifest contents |
| D(iii) | Stage 1→2 (partial) | manifest + whatever resolves | tolerance: listed artifact 404s, skip and disclose |

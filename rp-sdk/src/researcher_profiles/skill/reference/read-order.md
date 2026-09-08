# Why the read order is normative, not advisory

This is background reading for an agent that wants to know *why* before
deviating from the stage table in `SKILL.md`. It is not itself part of the
read order. You do not need to fetch this file to consume a profile
correctly. It exists for the case where you're tempted to fetch more than
the question needs "for good measure," and to give you the actual numbers
behind the caps.

## The naive slurp costs hundreds of times what the question needs

Measured across three profiles built by one automated pipeline:

| Artifact | Size |
|---|---|
| `profile.jsonld` (manifest + metadata) | 7-10 KB |
| `expertise.md` | 9-15 KB |
| `soul.md` | 7-15 KB |
| stage 1+2 working set | 25-40 KB |
| raw works collection (with abstracts) | 82-236 KB (mean ~150 KB) |
| one paper summary | mean 3.5-3.8 KB (n=79-87 per profile) |
| whole summary corpus | 269-311 KB |
| one full text | mean 63-80 KB |
| whole full-text corpus | 2.4-2.9 MB |
| everything except build bookkeeping | 3.1-3.7 MB |

An agent that fetches "everything" to answer "what does this person work on?"
burns roughly 3.5 MB (on the order of 875,000 tokens) to answer a question
that stage 1 alone (8 to 15 KB) already answers. That is not a rounding
error. It is a ~300x overshoot. The entire value of this skill is knowing
when to stop, not knowing how to parse JSON-LD.

## What each stage buys you

Stage 1 (`profile.jsonld`, 8 to 15 KB) alone answers: who this is, their
field and affiliation, `level`, `provenance`, `license`, how recently the
profile was updated, the topic-label list, and (on a current-generation
profile) `not_interests`, recurring critiques, and methodological
commitments, all of which sit directly in the metadata at no extra fetch
cost. A large fraction of the questions a user actually asks ("what does she
work on," "is this a real person or an AI," "how current is this") never
need to go past stage 1.

Stages 1 to 2 together (~25 to 40 KB, roughly 6,000 to 10,000 tokens) answer
the large majority of conversational questions: anything that needs the
researcher's voice, opinions, or framing, without needing citation-level
evidence for a specific claim. That is roughly 1% of the ~3.5 MB naive slurp
for a set of questions that covers most of an ordinary conversation.

Stage 3's raw form is 4 to 6 times the entire persona layer. A raw works
collection runs 82 to 236 KB because every entry embeds its abstract, 4 to 6
times the whole stage-1+2 persona layer (25 to 40 KB), for a stage whose only
job is answering "what papers, which
years, which venues, how many." A slim projection (id, title, year, venue,
links, no abstracts) does the same job in 10 to 40 KB. Fetch the raw form
only if you specifically need abstract text, which is rare; the summary
(stage 4) is almost always the better source for content questions about a
specific paper.

## Why the stage-4 cap is 8, not "however many appear"

The cap is calibrated against a real measurement, not chosen arbitrarily. A
single evidence-bearing paragraph in a current-generation `expertise.md`
carries 3 to 10 distinct `paper_id`s. One imaging-mass-cytometry paragraph
in a real profile carries roughly 10 on its own. If you fetched every id in
every matched paragraph across 1 to 3 topic labels, you would routinely be
pulling 15 to 30 summaries (55 to 115 KB) for a question that a ranked
top-8 (about 28 to 30 KB) answers equally well, because the ids nearest the
matched text are the ones actually supporting the claim. The rest are
adjacent context. Rank by proximity to the matched text, take the top 8,
and use the one optional hop (Step 2, item 8) if that genuinely isn't
enough. This is also why the cap is per-turn as well as per-session (20): a
multi-turn conversation that re-fetches the same handful of summaries every
turn should recognize it already has them, not repeatedly re-spend the
budget.

## Session budget

Track your budget internally rather than silently truncating:

- stage 4: at most 8 summary fetches per turn, at most 20 per session
- stage 5: at most 2 full-text fetches per turn; ask before fetching,
  with the approximate size (63 to 80 KB), since full text is an order of
  magnitude larger than a summary for the same paper
- stage 6: on demand only, and only on a `deep` profile

The budget stays invisible until it changes what the user gets (see "Talk
like a person" in `SKILL.md`). When a cap genuinely limits an answer, say
so once, in plain terms ("I've pulled as much of the record as I'm going to
in one sitting; here's what I have"), rather than either silently stopping
short or silently blowing through the cap. What you must not do is narrate
the budget when it constrained nothing.

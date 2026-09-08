# How to consume profiles efficiently

When reading a profile programmatically (especially from an AI agent), fetch
only what you need. The manifest is a menu, not a preload list.

## Progressive read order

A profile can contain hundreds of files. Fetching everything for a simple
question wastes bandwidth and context. Follow this order, stopping as soon as
you have enough information:

| Stage | Fetch | When to use | Cap |
|-------|-------|-------------|-----|
| 0 | Resolve base URL to the manifest | Always | 1 redirect |
| 1 | `profile.jsonld` | Always, first | 1 |
| 2 | `soul` + `expertise` | Voice, opinion, critique | Both |
| 3 | Works collection | Papers, years, venues, counts | 1 |
| 4 | Paper summaries (`paper_summary`) | Evidence for claims | ≤8/turn, ≤20/session |
| 5 | Full text | Only if summary can't answer AND user named the paper | ≤2/turn, announce first |
| 6 | `grants` | Funding questions (deep only) | On demand |
| none | `embedding_index` | Never by a text agent | none |

A consumer that fetches everything for a Stage-1 question wastes roughly 300x
the bandwidth of a manifest-only read. The measurement behind that figure is in
the consumer skill's
[read-order reference](https://github.com/databio/researcher-profiles/blob/master/rp-sdk/src/researcher_profiles/skill/reference/read-order.md).

## Using the citation graph

When the manifest has a `citations` entry, use it to find relevant papers
before fetching summaries:

1. Check if the question names specific papers
2. If so, verify they exist in the works collection
3. Use the citation graph to find related papers
4. Fetch summaries only for identified papers

/**
 * Human labels for the well-known manifest roles.
 *
 * Lives in `model/` rather than beside one panel because two screens name
 * the same roles: the Files list a visitor browses and the Publication panel an
 * owner sets tiers in. A second copy of this map is a screen that calls the
 * same artifact "Paper full text" in one place and `paper_fulltext` in the
 * other, which reads as two different things to the person deciding whether to
 * publish it.
 */
export const ROLE_LABELS: Record<string, string> = {
  profile: "Profile document",
  context: "Context (vocabulary)",
  agent_entry_point: "Agent entry point",
  works: "Publications",
  grants: "Grants",
  citations: "Citations",
  cv: "CV",
  paper_summary: "Paper summary",
  paper_fulltext: "Paper full text",
  web: "Web page",
  expertise: "Expertise",
  soul: "Research identity",
  embedding_index: "Embedding index",
  embedding_index_sqlite: "Embedding index (database)",
  embedding_chunks: "Embedding chunks",
  html: "Rendered page",
};

/** The label for a role, falling back to the token with separators softened. */
export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role.replace(/[-_]/g, " ");
}

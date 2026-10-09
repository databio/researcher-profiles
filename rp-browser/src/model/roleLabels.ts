/**
 * Human labels for the well-known manifest roles. Shared so every screen names
 * a role the same way; keep one copy.
 */
export const ROLE_LABELS: Record<string, string> = {
  profile: "Profile document",
  context: "Context (vocabulary)",
  agent_entry_point: "Agent entry point",
  works: "Publications",
  grants: "Grants",
  citations: "Citations",
  cv: "CV",
  interview: "Interview digest",
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

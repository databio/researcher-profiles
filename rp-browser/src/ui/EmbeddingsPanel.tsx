import type { ProfileEmbeddings } from "../model/dataLayer";

interface Props {
  loading: boolean;
  error: string | null;
  data: ProfileEmbeddings | null;
}

const SOURCE_LABELS: Record<string, string> = {
  paper_summary: "Paper summaries",
  paper_abstract: "Paper abstracts",
  expertise: "Expertise",
  soul: "Research identity",
};

function sourceLabel(key: string): string {
  return SOURCE_LABELS[key] ?? key.replace(/_/g, " ");
}

/**
 * Shows what embedding vectors a profile publishes: the backend/model, vector
 * dimension, chunk count, and a breakdown of which parts of the profile were
 * embedded. Renders an honest empty state when a profile ships no vectors.
 */
export function EmbeddingsPanel({ loading, error, data }: Props) {
  if (loading) return <p className="text-muted">Loading embeddings…</p>;
  if (error) return <p className="text-danger">{error}</p>;

  if (!data) {
    return (
      <div className="mb-6">
        <p>This profile publishes no embeddings.</p>
        <p className="text-muted">
          Only full/lite profiles with a searchable index export vectors; deep
          profiles contribute only a registry centroid.
        </p>
      </div>
    );
  }

  const { index, chunks } = data;

  const counts = new Map<string, number>();
  for (const c of chunks) {
    counts.set(c.source_type, (counts.get(c.source_type) ?? 0) + 1);
  }
  const order = ["paper_summary", "paper_abstract", "expertise", "soul"];
  const breakdown = [...counts.entries()].sort(
    (a, b) => order.indexOf(a[0]) - order.indexOf(b[0]),
  );

  return (
    <div className="embeddings">
      <dl className="fact-grid">
        <div className="fact-grid__item">
          <dt className="fact-grid__label">Model</dt>
          <dd className="fact-grid__value mono">{index.backend_spec}</dd>
        </div>
        <div className="fact-grid__item">
          <dt className="fact-grid__label">Vectors</dt>
          <dd className="fact-grid__value">{index.count.toLocaleString()}</dd>
        </div>
        <div className="fact-grid__item">
          <dt className="fact-grid__label">Dimensions</dt>
          <dd className="fact-grid__value">{index.dim}</dd>
        </div>
        <div className="fact-grid__item">
          <dt className="fact-grid__label">Metric</dt>
          <dd className="fact-grid__value">{index.metric ?? "cosine"}</dd>
        </div>
        <div className="fact-grid__item">
          <dt className="fact-grid__label">Normalized</dt>
          <dd className="fact-grid__value">{index.normalized ? "yes" : "no"}</dd>
        </div>
        <div className="fact-grid__item">
          <dt className="fact-grid__label">Precision</dt>
          <dd className="fact-grid__value mono">{index.dtype ?? "float32"}</dd>
        </div>
      </dl>

      {breakdown.length > 0 && (
        <div>
          <h3 className="embeddings__subhead">What was embedded</h3>
          <ul className="list-plain flex flex-col gap-1">
            {breakdown.map(([type, n]) => (
              <li key={type} className="embeddings__source-item">
                <span className="embeddings__source-name">{sourceLabel(type)}</span>
                <span className="embeddings__source-count">
                  {n} {n === 1 ? "chunk" : "chunks"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

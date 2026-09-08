import { useState, useCallback } from "react";
import { useStore } from "../store";
import { getGroups, getCentroidsForGroup, type ComparisonGroup } from "../model/groups";
import { resolveTier, type TierInfo } from "../model/strategy";
import { ensureProbe } from "../vec/probe";
import { ensureModel, embedQuery, type ModelLoadProgress } from "../vec/localModel";
import { rankAgainst, mmrDiversify, type ScoredItem } from "../vec/ops";
import { useSearchParams } from "react-router";
import { navigate } from "../router";

interface GroupResult {
  group: ComparisonGroup;
  tier: TierInfo;
  items: ScoredItem[];
  order: string[];
}

export function Search() {
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const [results, setResults] = useState<GroupResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [modelProgress, setModelProgress] = useState<ModelLoadProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [useMmr, setUseMmr] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const { cards } = useStore();

  const k = 20;

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;

    navigate({ page: "search", query: q });
    setSearching(true);
    setError(null);
    setResults([]);
    setHasSearched(true);

    try {
      const groups = getGroups();
      const groupResults: GroupResult[] = [];

      setModelProgress({ status: "loading", progress: 0 });
      await ensureModel((p: ModelLoadProgress) => setModelProgress(p));
      setModelProgress(null);

      for (const group of groups) {
        if (group.backendSpec === "st:all-MiniLM-L6-v2" && group.probe) {
          await ensureProbe(group.backendSpec, group.probe.text, group.probe.vector);
        }
      }

      const queryVec = await embedQuery(q);

      for (const group of groups) {
        const tier = resolveTier(group);

        if (!group.backendSpec || !group.hasCentroids) {
          groupResults.push({ group, tier, items: [], order: [] });
          continue;
        }

        if (tier.tier > 1) {
          groupResults.push({ group, tier, items: [], order: [] });
          continue;
        }

        const centroidData = getCentroidsForGroup(group.backendSpec);
        if (!centroidData) {
          groupResults.push({ group, tier, items: [], order: [] });
          continue;
        }

        let scored = rankAgainst(queryVec, centroidData.vectors, centroidData.dim, k * 2);

        if (useMmr && scored.length > 1) {
          scored = mmrDiversify(scored, centroidData.vectors, centroidData.dim, k, 0.5);
        } else {
          scored = scored.slice(0, k);
        }

        groupResults.push({
          group,
          tier,
          items: scored,
          order: centroidData.order,
        });
      }

      setResults(groupResults);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSearching(false);
    }
  }, [query, useMmr]);

  const cardByBase = new Map(cards.map((c) => [c.base, c]));

  return (
    <div>
      <h2>Search</h2>
      <div className="input-row mb-4">
        <input
          className="input-row__field"
          type="text"
          placeholder="Search by research interest, topic, or expertise..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          disabled={searching}
        />
        <button
          className="btn btn--primary"
          onClick={handleSearch}
          disabled={searching}
        >
          {searching ? "Searching..." : "Search"}
        </button>
      </div>

      <div className="mb-4">
        <label className="search__toggle">
          <input
            type="checkbox"
            checked={useMmr}
            onChange={(e) => setUseMmr(e.target.checked)}
          />
          Diversify results (MMR)
        </label>
      </div>

      {modelProgress && modelProgress.status === "loading" && (
        <div className="progress">
          <div className="progress__label">
            Loading embedding model{modelProgress.file ? `: ${modelProgress.file}` : ""}...
          </div>
          {modelProgress.progress != null && (
            <div className="progress__bar">
              <div
                className="progress__fill"
                style={{ '--progress': `${Math.round(modelProgress.progress)}%` } as React.CSSProperties}
              />
            </div>
          )}
        </div>
      )}

      {error && <p className="text-danger text-sm">{error}</p>}

      {!hasSearched && (
        <p className="page-info">
          Free-text search embeds your query locally using a small language model
          and ranks profiles by cosine similarity against their research centroids.
          The model (~23 MB) loads on first use.
        </p>
      )}

      {hasSearched && !searching && results.length > 0 && (
        <div className="mt-4">
          {results.map((gr, gi) => (
            <GroupSection
              key={gr.group.backendSpec ?? `null-${gi}`}
              result={gr}
              cardByBase={cardByBase}
              showHeader={results.length > 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function GroupSection({
  result,
  cardByBase,
  showHeader,
}: {
  result: GroupResult;
  cardByBase: Map<string, (typeof result.group.cards)[0]>;
  showHeader: boolean;
}) {
  const { group, tier, items, order } = result;

  return (
    <section className="mb-6">
      {showHeader && (
        <h3 className="search__group-header">
          {group.backendSpec ?? "No embeddings"}
        </h3>
      )}

      {tier.tier >= 2 && (
        <p className="search__tier-notice">{tier.reason}</p>
      )}

      {tier.tier === 1 && items.length === 0 && (
        <p className="page-info">No results found.</p>
      )}

      {items.length > 0 && (
        <ol className="result-list">
          {items.map((item) => {
            const base = order[item.index];
            const card = base ? cardByBase.get(base) : undefined;
            return (
              <li key={item.index} className="result-list__item">
                <button
                  className="result-list__link"
                  onClick={() => base && navigate({ page: "profile", url: base })}
                >
                  <span className="result-list__name">
                    {card?.name ?? base ?? `Profile #${item.index}`}
                  </span>
                  <span className="result-list__score">
                    {item.score.toFixed(3)}
                  </span>
                </button>
                {card && (card.affiliation || card.field) && (
                  <span className="result-list__meta">
                    {[card.field, card.affiliation].filter(Boolean).join(" · ")}
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

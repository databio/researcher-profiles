import { useState, useMemo } from "react";
import { useStore } from "../store";
import { getGroups, getCentroidsForGroup } from "../model/groups";
import { kmeans, type ClusterAssignment } from "../vec/ops";
import { navigate } from "../router";

export function Clusters() {
  const { cards } = useStore();
  const groups = useMemo(() => getGroups(), [cards]);
  const groupsWithCentroids = groups.filter((g) => g.hasCentroids && g.backendSpec);

  if (cards.length === 0) {
    return <p className="page-empty">No profiles loaded. Add a source to see clusters.</p>;
  }

  if (groupsWithCentroids.length === 0) {
    return (
      <div>
        <h2>Clusters</h2>
        <p className="page-info">
          {cards.length} profiles loaded but no embedding centroids available.
          Add a source that publishes a collection bundle with embeddings.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h2>Clusters</h2>
      {groupsWithCentroids.length > 1 && (
        <p className="page-hint">
          Multiple embedding backends loaded. Vectors from different backends are
          not comparable. Each group is clustered independently below.
        </p>
      )}
      {groupsWithCentroids.map((group) => (
        <ClusterGroup
          key={group.backendSpec!}
          backendSpec={group.backendSpec!}
          showBanner={groupsWithCentroids.length > 1}
        />
      ))}
    </div>
  );
}

function ClusterGroup({
  backendSpec,
  showBanner,
}: {
  backendSpec: string;
  showBanner: boolean;
}) {
  const { cards } = useStore();
  const centroidData = getCentroidsForGroup(backendSpec);
  if (!centroidData) return null;

  const n = centroidData.order.length;
  const defaultK = Math.max(2, Math.round(Math.sqrt(n)));
  const [k, setK] = useState(defaultK);

  const clusters = useMemo(
    () => kmeans(centroidData.vectors, centroidData.dim, k),
    [centroidData.vectors, centroidData.dim, k],
  );

  const baseToCard = useMemo(() => {
    const m = new Map<string, typeof cards[0]>();
    for (const c of cards) m.set(c.base, c);
    return m;
  }, [cards]);

  return (
    <div className="mb-8">
      {showBanner && (
        <h3 className="text-base mt-4 mb-2">
          Backend: <code>{backendSpec}</code> ({n} profiles)
        </h3>
      )}
      <div className="flex items-center gap-3 my-3">
        <label className="text-sm">
          Clusters (k): <strong>{k}</strong>
        </label>
        <input
          type="range"
          min={2}
          max={Math.max(2, n)}
          value={k}
          onChange={(e) => setK(Number(e.target.value))}
          className={`flex-1 cluster-range`}
        />
      </div>
      <div className="cluster-grid">
        {clusters.map((cluster, ci) => (
          <ClusterCard
            key={ci}
            cluster={cluster}
            order={centroidData.order}
            baseToCard={baseToCard}
          />
        ))}
      </div>
    </div>
  );
}

function ClusterCard({
  cluster,
  order,
  baseToCard,
}: {
  cluster: ClusterAssignment;
  order: string[];
  baseToCard: Map<string, { slug: string; name: string; base: string }>;
}) {
  const exemplarBase = order[cluster.exemplar];
  const exemplarCard = exemplarBase ? baseToCard.get(exemplarBase) : undefined;

  return (
    <div className="cluster-card">
      <div className="flex justify-between mb-2">
        <strong className="cluster-card__title">
          {exemplarCard?.name ?? `Cluster ${cluster.cluster + 1}`}
        </strong>
        <span className="cluster-card__count">
          {cluster.members.length} {cluster.members.length === 1 ? "profile" : "profiles"}
        </span>
      </div>
      <div className="flex flex-wrap gap-1">
        {cluster.members.map((idx) => {
          const base = order[idx];
          const card = base ? baseToCard.get(base) : undefined;
          const label = card?.name ?? `Profile ${idx}`;
          const isExemplar = idx === cluster.exemplar;
          return (
            <button
              key={idx}
              onClick={() => base && navigate({ page: "profile", url: base })}
              title={label}
              className={isExemplar ? "cluster-chip cluster-chip--exemplar" : "cluster-chip"}
            >
              {label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

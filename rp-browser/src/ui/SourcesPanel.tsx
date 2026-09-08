import { useStore, persistSources } from "../store";
import type { SourceEntry } from "../store";
import { useState, useMemo } from "react";
import { getGroups } from "../model/groups";
import { resolveTier } from "../model/strategy";
import { Copyable } from "./Copyable";

const KIND_LABEL: Record<SourceEntry["kind"], string> = {
  profile: "Profile",
  list: "List",
  registry: "Registry",
  unknown: "Source",
};

function shortLabel(url: string): string {
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);
    const last = parts[parts.length - 1] || u.hostname;
    return last.replace(/\.(jsonld|json)$/, "");
  } catch {
    return url;
  }
}

const TIER_LABEL: Record<number, string> = {
  1: "Tier 1: full search",
  2: "Tier 2: similarity only",
  3: "Tier 3: no embeddings",
};

export function SourcesPanel() {
  const { sources, cards } = useStore();
  const [input, setInput] = useState("");

  const tierBySpec = useMemo(() => {
    const groups = getGroups();
    const m = new Map<string | null, number>();
    for (const g of groups) m.set(g.backendSpec, resolveTier(g).tier);
    return m;
  }, [cards]);

  function handleAdd() {
    const url = input.trim();
    if (!url) return;
    useStore.addSource(url);
    setInput("");
    persistSources();
  }

  const ordered = [...sources].sort((a, b) => (b.builtin ? 1 : 0) - (a.builtin ? 1 : 0));

  return (
    <div className="sources">
      <h2 className="sources__heading">Sources</h2>
      <div className="input-row">
        <input
          className="input-row__field input-row__field--sm"
          type="url"
          placeholder="Paste a profile or list URL..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleAdd()}
        />
        <button className="btn btn--alt text-sm" onClick={handleAdd}>
          Add
        </button>
      </div>

      {sources.length === 0 ? (
        <p className="sources__empty">
          No sources yet. Paste a single profile URL (ending in{" "}
          <code>profile.jsonld</code>) or a profile-list URL above.
        </p>
      ) : (
        <ul className="list-plain flex flex-col gap-2">
          {ordered.map((s) => {
            const named = cards.find((c) => c.sourceUrl === s.url);
            const title = named?.name ?? shortLabel(s.url);
            return (
              <li key={s.url} className="sources__item">
                <div className="sources__item-top">
                  <span className="source-kind" data-kind={s.kind}>
                    {KIND_LABEL[s.kind]}
                  </span>
                  {s.status === "loading" && (
                    <span className="sources__badge">loading…</span>
                  )}
                  {s.status === "ready" && (
                    <span className="sources__badge sources__badge--ok">
                      {s.profileCount} {s.profileCount === 1 ? "profile" : "profiles"}
                    </span>
                  )}
                  {s.status === "unauthorized" && (
                    <span className="sources__badge sources__badge--warn">sign-in required</span>
                  )}
                  {s.status === "error" && (
                    <span className="sources__badge sources__badge--error">failed</span>
                  )}
                  {!s.builtin && (
                    <button
                      className="sources__remove"
                      onClick={() => {
                        useStore.removeSource(s.url);
                        persistSources();
                      }}
                      title="Remove source"
                      aria-label="Remove source"
                    >
                      ✕
                    </button>
                  )}
                </div>
                <div className="sources__name" title={title}>
                  {title}
                </div>
                <div className="sources__url" title={s.url}>
                  <Copyable value={s.url} title="Copy source URL">
                    {s.url}
                  </Copyable>
                </div>
                {s.status === "error" && s.error && (
                  <div className="sources__detail sources__detail--error">{s.error}</div>
                )}
                {s.status === "unauthorized" && (
                  <div className="sources__detail sources__detail--warn">
                    You don&rsquo;t have read access to this registry yet.
                  </div>
                )}
                {s.status === "ready" && (
                  <div className="sources__meta">
                    {s.backendSpec ? (
                      <>
                        <code>{s.backendSpec}</code>
                        {" · "}
                        {TIER_LABEL[tierBySpec.get(s.backendSpec) ?? 3]}
                      </>
                    ) : (
                      TIER_LABEL[3]
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

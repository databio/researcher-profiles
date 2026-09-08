import { useEffect, useState } from "react";
import { Link } from "react-router";
import { useStore } from "../store";
import { fetchJson } from "../net/fetchJson";
import { buildPath } from "../router";

interface TopicsDoc {
  version: number;
  computed_at: string;
  index: Record<string, string[]>;
}

interface MergedTopic {
  label: string;
  bases: string[];
}

export function Topics() {
  const { sources, cards } = useStore();
  const [topics, setTopics] = useState<MergedTopic[]>([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    const readySources = sources.filter((s) => s.status === "ready");
    if (readySources.length === 0) {
      setTopics([]);
      return;
    }

    let cancelled = false;
    setLoading(true);

    (async () => {
      const merged = new Map<string, Set<string>>();

      await Promise.all(
        readySources.map(async (s) => {
          try {
            const url = new URL("collection/topics.json", s.url).href;
            const outcome = await fetchJson<TopicsDoc>(url);
            if (!outcome.ok || cancelled) return;
            const idx = outcome.value.index;
            for (const [label, bases] of Object.entries(idx)) {
              const key = label.toLowerCase().trim();
              if (!key) continue;
              if (!merged.has(key)) merged.set(key, new Set());
              const set = merged.get(key)!;
              for (const b of bases) {
                const abs = new URL(b, s.url).href;
                set.add(abs);
              }
            }
          } catch {
            // skip sources without topics
          }
        }),
      );

      if (cancelled) return;

      const sorted = Array.from(merged.entries())
        .map(([label, basesSet]) => ({
          label,
          bases: Array.from(basesSet),
        }))
        .sort((a, b) => b.bases.length - a.bases.length);

      setTopics(sorted);
      setLoading(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [sources]);

  if (cards.length === 0) {
    return (
      <p className="page-empty">
        No profiles loaded. Add a source to see topics.
      </p>
    );
  }

  function toggle(label: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }

  function nameForBase(base: string): string {
    const card = cards.find((c) => c.base === base || base.endsWith(c.base));
    return card?.name ?? base;
  }

  return (
    <div>
      <h2>Topics</h2>
      {loading && <p className="page-info">Loading topics...</p>}
      {!loading && topics.length === 0 && (
        <p className="page-info">
          No topics available. Sources may not publish topic data.
        </p>
      )}
      {topics.length > 0 && (
        <p className="page-info">
          {topics.length} topics across {cards.length} loaded profiles.
        </p>
      )}
      <ul className="list-plain my-2">
        {topics.map((t) => (
          <li key={t.label} className="mb-1">
            <button
              onClick={() => toggle(t.label)}
              className="topic-button"
            >
              <span className="topic-caret">
                {expanded.has(t.label) ? "▾" : "▸"}
              </span>
              <strong>{t.label}</strong>
              <span className="page-info ml-2">
                ({t.bases.length})
              </span>
            </button>
            {expanded.has(t.label) && (
              <ul className="list-plain pl-6">
                {t.bases.map((base) => (
                  <li key={base} className="topic-member">
                    <Link to={buildPath({ page: "profile", url: base })}>
                      {nameForBase(base)}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

import { useEffect, useState, useCallback, useMemo } from "react";
import {
  ProfileHeader,
  MetadataPanel,
  MarkdownSection,
  PapersList,
} from "@rp/ui-lib";
import type { ProfileDetail, PaperEntry } from "@rp/ui-lib/types";
import { loadManifest, profileFiles, type ResolvedProfile } from "../model/manifest";
import {
  getProfileDetail,
  getProfilePapers,
  getPaperSummary,
  getProfileEmbeddings,
  type ProfileEmbeddings,
} from "../model/dataLayer";
import { useStore } from "../store";
import { cosine } from "../vec/ops";
import { EmbeddingsPanel } from "./EmbeddingsPanel";
import { FilesPanel } from "./FilesPanel";
import { Copyable } from "./Copyable";
import { useShellSlots } from "../slots";
import { Link, useSearchParams } from "react-router";
import { navigate, buildPath } from "../router";

/**
 * The built-in tabs, plus whatever a host application adds through the
 * `profileTabs` shell slot, hence the open string half of the union.
 */
type TabId =
  | "overview"
  | "expertise"
  | "soul"
  | "papers"
  | "embeddings"
  | "files"
  | (string & {});

interface ProfilePageProps {
  url?: string;
}

export function ProfilePage({ url: urlProp }: ProfilePageProps = {}) {
  const [searchParams] = useSearchParams();
  const url = urlProp ?? searchParams.get("u") ?? "";
  const [detail, setDetail] = useState<ProfileDetail | null>(null);
  const [papers, setPapers] = useState<PaperEntry[]>([]);
  const [resolved, setResolved] = useState<ResolvedProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState<TabId>("overview");

  const [emb, setEmb] = useState<ProfileEmbeddings | null>(null);
  const [embLoading, setEmbLoading] = useState(false);
  const [embError, setEmbError] = useState<string | null>(null);
  const [embRequested, setEmbRequested] = useState(false);

  const sameOrigin = useMemo(() => {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch {
      return false;
    }
  }, [url]);

  const slots = useShellSlots();
  const tabCtx = { url, slug: detail?.slug ?? null, sameOrigin };
  const extraTabs = slots.profileTabs.filter((t) => t.enabled(tabCtx));

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setActive("overview");
    setResolved(null);
    setEmb(null);
    setEmbError(null);
    setEmbRequested(false);

    (async () => {
      try {
        const manifest = await loadManifest(url);
        if (cancelled) return;
        const [d, p] = await Promise.all([
          getProfileDetail(manifest),
          getProfilePapers(manifest),
        ]);
        if (cancelled) return;
        setDetail(d);
        setPapers(p);
        setResolved(manifest);
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [url]);

  const loadSummary = useCallback(
    (paperId: string) =>
      loadManifest(url).then((m) => getPaperSummary(m, paperId)),
    [url],
  );

  useEffect(() => {
    if (active !== "embeddings" || embRequested) return;
    setEmbRequested(true);
    let cancelled = false;
    setEmbLoading(true);
    setEmbError(null);
    (async () => {
      try {
        const manifest = await loadManifest(url);
        const e = await getProfileEmbeddings(manifest);
        if (!cancelled) setEmb(e);
      } catch (e) {
        if (!cancelled) setEmbError(String(e));
      } finally {
        if (!cancelled) setEmbLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [active, embRequested, url]);

  const tabs = useMemo(() => {
    if (!detail) return [] as { id: TabId; label: string }[];
    const t: { id: TabId; label: string }[] = [{ id: "overview", label: "Overview" }];
    if (detail.expertise) t.push({ id: "expertise", label: "Expertise" });
    if (detail.soul) t.push({ id: "soul", label: "Research Identity" });
    if (papers.length) t.push({ id: "papers", label: `Publications (${papers.length})` });
    t.push({ id: "embeddings", label: "Embeddings" });
    t.push({ id: "files", label: "Files" });
    for (const e of extraTabs) t.push({ id: e.id, label: e.label });
    return t;
  }, [detail, papers.length, extraTabs]);

  const files = useMemo(
    () => (resolved ? profileFiles(resolved) : []),
    [resolved],
  );

  const { centroids, cards: allCards } = useStore();
  const similarProfiles = useMemo(() => {
    const results: { name: string; base: string; score: number }[] = [];
    for (const [, data] of centroids) {
      const myIdx = data.order.indexOf(url);
      if (myIdx === -1) continue;
      const myVec = new Float32Array(data.vectors.buffer, myIdx * data.dim * 4, data.dim);
      const scored: { idx: number; score: number }[] = [];
      const count = data.order.length;
      for (let i = 0; i < count; i++) {
        if (i === myIdx) continue;
        const otherVec = new Float32Array(data.vectors.buffer, i * data.dim * 4, data.dim);
        scored.push({ idx: i, score: cosine(myVec, otherVec) });
      }
      scored.sort((a, b) => b.score - a.score);
      for (const s of scored.slice(0, 5)) {
        const base = data.order[s.idx];
        const card = allCards.find((c) => c.base === base);
        results.push({ name: card?.name ?? base, base, score: s.score });
      }
      break;
    }
    return results;
  }, [url, centroids, allCards]);

  useEffect(() => {
    if (tabs.length && !tabs.some((t) => t.id === active)) setActive("overview");
  }, [tabs, active]);

  return (
    <div>
      <div className="profile-chrome">
        <span className="profile-chrome__origin">
          <Copyable value={url} title="Copy profile URL">
            {url}
          </Copyable>
        </span>
        <button
          className="btn btn--ghost"
          onClick={() => navigate({ page: "validate", url })}
        >
          Validate this profile
        </button>
      </div>
      {loading && <p>Loading profile...</p>}
      {error && <p className="text-danger">{error}</p>}
      {detail && !loading && (
        <div>
          <ProfileHeader metadata={detail.metadata} />

          <div className="tab-bar mt-5 mb-6" role="tablist">
            {tabs.map((t) => (
              <button
                key={t.id}
                role="tab"
                aria-selected={active === t.id}
                className={`tab-bar__tab ${active === t.id ? "tab-bar__tab--active" : ""}`}
                onClick={() => setActive(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="tab-bar__panel" role="tabpanel">
            {active === "overview" && (
              <>
                <MetadataPanel metadata={detail.metadata} />
                {similarProfiles.length > 0 && (
                  <div className="profile-similar">
                    <h3 className="profile-similar__title">Similar Researchers</h3>
                    <ul className="list-plain">
                      {similarProfiles.map((sp) => (
                        <li key={sp.base} className="profile-similar__item">
                          <Link
                            to={buildPath({ page: "profile", url: sp.base })}
                            className="profile-similar__link"
                          >
                            {sp.name}
                          </Link>
                          <span className="profile-similar__score">
                            {sp.score.toFixed(3)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
            {active === "expertise" && <MarkdownSection title="Expertise narrative" body={detail.expertise} />}
            {active === "soul" && <MarkdownSection title="Narrative voice (SOUL)" body={detail.soul} />}
            {active === "papers" && (
              <PapersList papers={papers} loadSummary={loadSummary} />
            )}
            {active === "embeddings" && (
              <EmbeddingsPanel loading={embLoading} error={embError} data={emb} />
            )}
            {active === "files" && <FilesPanel files={files} />}
            {extraTabs.find((t) => t.id === active)?.render(tabCtx)}
          </div>
        </div>
      )}
    </div>
  );
}

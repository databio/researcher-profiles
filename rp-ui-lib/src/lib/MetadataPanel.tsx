import type { ProfileMetadataPayload } from "../types";
import styles from "./viewer.module.css";

// The wire contract types `training` / `career` as untyped dicts (list[dict]).
// These loose shapes describe the keys the on-disk profile.jsonld conventionally
// uses; unknown keys are ignored by the renderer.
interface TrainingEntry {
  kind?: string;
  degree?: string;
  institution?: string;
  year_start?: number;
  year_end?: number;
  advisor?: string;
}
interface CareerEntry {
  role?: string;
  institution?: string;
  start_year?: number;
  end_year?: number;
}

/** "2016-2022", "2024-present", a bare year, or null when nothing is known. */
function spanLabel(start?: number | null, end?: number | null, held = false): string | null {
  if (start && end) return start === end ? String(end) : `${start}-${end}`;
  if (start) return held ? `${start}-present` : String(start);
  if (end) return String(end);
  return null;
}

// Who asserted this profile, and on what basis. Only `orcid_verified` means
// the subject endorsed it; the rest are honest labels for the other cases.
const PROVENANCE_LABELS: Record<string, string> = {
  orcid_verified: "The ORCID record links back to this profile",
  self_published: "Published by the subject about themselves",
  third_party: "Built by someone else about this researcher",
  synthetic: "Not a real person",
  historical: "A historical figure, who cannot hold an ORCID",
};

/**
 * Training and career histories plus the expertise tag cloud. Presentational
 * only; renders nothing when all sections are empty.
 */
export function MetadataPanel({ metadata }: { metadata: ProfileMetadataPayload }) {
  const training = (metadata.training ?? []) as TrainingEntry[];
  const career = (metadata.career ?? []) as CareerEntry[];
  const expertise = metadata.expertise ?? [];
  const provenance = metadata.provenance ?? null;
  const license = metadata.license ?? null;

  if (!training.length && !career.length && !expertise.length && !provenance)
    return null;

  return (
    <section className={styles.section}>
      <h2 className={styles.sectionTitle}>Background</h2>
      {(provenance || license) && (
        <div className={styles.headerMeta}>
          {provenance && (
            <span className={styles.badge} title={PROVENANCE_LABELS[provenance] ?? provenance}>
              {provenance.replace(/_/g, " ")}
            </span>
          )}
          {license && (
            <a
              className={styles.link}
              href={license}
              target="_blank"
              rel="noreferrer"
            >
              licence
            </a>
          )}
        </div>
      )}
      <div className={styles.metaGrid}>
        {training.length > 0 && (
          <div>
            <h3 className={styles.sectionTitle}>Training</h3>
            <ul className={styles.metaList}>
              {training.map((t, i) => (
                <li key={i} className={styles.metaItem}>
                  <span className={styles.metaPrimary}>
                    {[t.degree, t.institution].filter(Boolean).join(", ")}
                  </span>
                  <span className={styles.metaSecondary}>
                    {[spanLabel(t.year_start, t.year_end), t.advisor && `advisor: ${t.advisor}`]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {career.length > 0 && (
          <div>
            <h3 className={styles.sectionTitle}>Career</h3>
            <ul className={styles.metaList}>
              {career.map((c, i) => (
                <li key={i} className={styles.metaItem}>
                  <span className={styles.metaPrimary}>
                    {[c.role, c.institution].filter(Boolean).join(", ")}
                  </span>
                  {spanLabel(c.start_year, c.end_year, true) && (
                    <span className={styles.metaSecondary}>
                      {spanLabel(c.start_year, c.end_year, true)}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {expertise.length > 0 && (
        <div>
          <h3 className={styles.sectionTitle}>Expertise</h3>
          <div className={styles.tags}>
            {expertise.map((e, i) => (
              <span key={i} className={styles.tag}>
                {e}
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

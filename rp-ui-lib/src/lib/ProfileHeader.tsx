import type { ProfileMetadataPayload } from "../types";
import styles from "./viewer.module.css";

function orcidUrl(orcid: string): string {
  return orcid.startsWith("http") ? orcid : `https://orcid.org/${orcid}`;
}

/**
 * Name, affiliation, field / subfields, ORCID link, level badge, and the
 * one-line metadata summary. Presentational only.
 */
export function ProfileHeader({ metadata }: { metadata: ProfileMetadataPayload }) {
  const subfields = metadata.subfields ?? [];
  // `rid` is the identity on the wire, and it is an ORCID exactly when it
  // does not carry the `local:` prefix.
  const orcid =
    metadata.rid && !metadata.rid.startsWith("local:") ? metadata.rid : null;
  const fieldLine = [metadata.field, subfields.join(", ")]
    .filter(Boolean)
    .join(" · ");

  return (
    <header className={styles.header}>
      <h1 className={styles.name}>{metadata.name}</h1>
      {metadata.affiliation && (
        <p className={styles.subline}>{metadata.affiliation}</p>
      )}
      {fieldLine && <p className={styles.subline}>{fieldLine}</p>}
      <div className={styles.headerMeta}>
        {metadata.level && (
          <span className={styles.badge}>{metadata.level}</span>
        )}
        {orcid && (
          <a
            className={styles.link}
            href={orcidUrl(orcid)}
            target="_blank"
            rel="noreferrer"
          >
            ORCID {orcid}
          </a>
        )}
        {metadata.scholar_url && (
          <a
            className={styles.link}
            href={metadata.scholar_url}
            target="_blank"
            rel="noreferrer"
          >
            Scholar
          </a>
        )}
        {metadata.openalex_id && (
          <a
            className={styles.link}
            href={
              metadata.openalex_id.startsWith("http")
                ? metadata.openalex_id
                : `https://openalex.org/${metadata.openalex_id}`
            }
            target="_blank"
            rel="noreferrer"
          >
            OpenAlex
          </a>
        )}
      </div>
      {metadata.summary && <p className={styles.summaryText}>{metadata.summary}</p>}
    </header>
  );
}

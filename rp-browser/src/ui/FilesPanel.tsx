import type { ProfileFile } from "../model/manifest";
import { roleLabel } from "../model/roleLabels";

interface Props {
  files: ProfileFile[];
}

function fileName(href: string): string {
  try {
    const path = new URL(href).pathname;
    return path.slice(path.lastIndexOf("/") + 1) || href;
  } catch {
    return href;
  }
}

function tierClass(tier: string): string {
  if (tier === "public") return "badge--accent";
  if (tier === "internal") return "badge--warn";
  return "badge--danger";
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Lists the single-copy files a profile publishes (the manifest plus each
 * typed artifact) as direct links for exploring the raw data behind the page.
 */
export function FilesPanel({ files }: Props) {
  return (
    <div className="file-list">
      <p className="file-list__intro">
        The raw files behind this profile. Every link is a typed entry from the
        profile manifest (hasPart / subjectOf).
      </p>
      <div className="data-table-wrap">
        <table className="data-table data-table--static">
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">File</th>
              <th scope="col">Visibility</th>
              <th scope="col">Type</th>
              <th scope="col" className="col-right">
                Size
              </th>
            </tr>
          </thead>
          <tbody>
            {files.map((f) => (
              <tr key={f.href}>
                <td>
                  <a href={f.href} target="_blank" rel="noreferrer">
                    {roleLabel(f.role)}
                  </a>
                </td>
                <td className="file-list__name">{fileName(f.href)}</td>
                <td>
                  {f.visibility && (
                    <span className={`badge ${tierClass(f.visibility)}`}>{f.visibility}</span>
                  )}
                </td>
                <td className="file-list__type">{f.mediaType}</td>
                <td className="col-right">{f.bytes != null ? formatBytes(f.bytes) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

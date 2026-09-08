import { useStore } from "../store";
import { useState } from "react";
import { Link } from "react-router";
import { buildPath } from "../router";

export function Diagnostics() {
  const { failures } = useStore();
  const [open, setOpen] = useState(false);

  if (failures.length === 0) return null;

  return (
    <div className="diagnostics">
      <button className="diagnostics__chip" onClick={() => setOpen(!open)}>
        {failures.length} failed
      </button>
      {open && (
        <div className="diagnostics__panel">
          <h3 className="diagnostics__heading">Failed Fetches</h3>
          <ul className="list-plain">
            {failures.map((f, i) => (
              <li key={i} className="diagnostics__item">
                <span className="diagnostics__url">{f.url}</span>
                <span className="diagnostics__kind">
                  {!f.outcome.ok ? f.outcome.kind : "unknown"}
                </span>
                <Link
                  to={buildPath({ page: "validate", url: f.url })}
                  className="diagnostics__link"
                >
                  Diagnose
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

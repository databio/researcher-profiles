/**
 * One presentational card for a shell-level state (signed out, no access,
 * empty registry, ...). Every state in `selectShellState` renders one
 * instance of this so they share layout, spacing, and tokens instead of
 * each screen inventing its own empty-state markup.
 */
import type { ReactNode } from "react";
import { Link } from "react-router";

export interface ShellStateAction {
  label: string;
  onClick?: () => void;
  href?: string;
  primary?: boolean;
  disabled?: boolean;
}

interface Props {
  title: string;
  body: ReactNode;
  actions?: ShellStateAction[];
  /** An extra line rendered in warn color below the actions (e.g. "sign-in not configured"). */
  warning?: ReactNode;
}

export function ShellState({ title, body, actions, warning }: Props) {
  return (
    <div className="shell-state">
      <div className="shell-state__card">
        <h2 className="shell-state__title">{title}</h2>
        <div className="shell-state__body">{body}</div>
        {actions && actions.length > 0 && (
          <div className="shell-state__actions">
            {actions.map((a, i) =>
              a.href ? (
                a.href.startsWith("/") ? (
                  <Link
                    key={i}
                    to={a.href}
                    className={a.primary ? "btn btn--primary" : "btn btn--secondary"}
                  >
                    {a.label}
                  </Link>
                ) : (
                  <a
                    key={i}
                    href={a.href}
                    className={a.primary ? "btn btn--primary" : "btn btn--secondary"}
                  >
                    {a.label}
                  </a>
                )
              ) : (
                <button
                  key={i}
                  className={a.primary ? "btn btn--primary" : "btn btn--secondary"}
                  onClick={a.onClick}
                  disabled={a.disabled}
                >
                  {a.label}
                </button>
              ),
            )}
          </div>
        )}
        {warning && <p className="shell-state__warn">{warning}</p>}
      </div>
    </div>
  );
}

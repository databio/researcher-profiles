import { useState, type ReactNode } from "react";

export interface CopyableProps {
  /** The exact string copied to the clipboard. */
  value: string;
  /** Visible content. Defaults to `value` rendered as text. */
  children?: ReactNode;
  /** Optional label for screen readers / tooltip. */
  title?: string;
  className?: string;
}

function CopyIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

/**
 * Inline value with a small copy-to-clipboard button. The icon flips to a
 * checkmark for ~1.5s after a successful copy. Clicks on the button never
 * propagate, so it is safe inside clickable rows.
 */
export function Copyable({ value, children, title, className }: CopyableProps) {
  const [copied, setCopied] = useState(false);

  function handleCopy(e: React.MouseEvent) {
    e.stopPropagation();
    navigator.clipboard.writeText(value).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      },
      (err) => console.error("Copy failed:", err),
    );
  }

  return (
    <span className={`copyable${className ? ` ${className}` : ""}`}>
      <span className="copyable__value">{children ?? value}</span>
      <button
        type="button"
        className={`copyable__btn${copied ? " copyable__btn--copied" : ""}`}
        onClick={handleCopy}
        title={title ?? (copied ? "Copied" : "Copy")}
        aria-label={title ?? (copied ? "Copied" : "Copy to clipboard")}
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </button>
    </span>
  );
}

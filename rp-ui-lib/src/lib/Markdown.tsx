import { useMemo } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import styles from "./viewer.module.css";

// Tighten DOMPurify: forbid dangerous tags and attributes, force safe links.
DOMPurify.addHook("afterSanitizeAttributes", (node: Element) => {
  // Force target="_blank" rel="noopener noreferrer" on external anchors
  if (node.tagName === "A" && node.getAttribute("href")) {
    const href = node.getAttribute("href") || "";
    if (href.startsWith("http://") || href.startsWith("https://")) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  }
});

const PURIFY_CONFIG = {
  FORBID_TAGS: ["script", "iframe", "object", "embed", "form", "style"],
  FORBID_ATTR: [
    "onerror", "onload", "onclick", "onmouseover", "onfocus", "onblur",
    "onsubmit", "onchange", "oninput", "onkeydown", "onkeyup", "onkeypress",
  ],
  ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
};

/**
 * Render a markdown string to sanitized HTML.
 *
 * Self-contained (marked + dompurify) so the canonical viewer works standalone
 * and behaves identically when vendored into another app. No global styles are
 * emitted. Output is scoped through the CSS module `prose` class.
 */
export function Markdown({ source }: { source: string | null | undefined }) {
  const html = useMemo(() => {
    if (!source) return "";
    const raw = marked.parse(source, { async: false }) as string;
    return DOMPurify.sanitize(raw, PURIFY_CONFIG);
  }, [source]);

  if (!html) return null;
  return (
    <div
      className={styles.prose}
      // Sanitized above with DOMPurify.
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

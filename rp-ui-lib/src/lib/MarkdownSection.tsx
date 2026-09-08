import { Markdown } from "./Markdown";
import styles from "./viewer.module.css";

/** Renders one titled markdown body (e.g. the expertise or SOUL narrative). */
export function MarkdownSection({ title, body }: { title: string; body: string | null | undefined }) {
  if (!body?.trim()) return null;
  return (
    <section className={styles.section}>
      <h2 className={styles.sectionTitle}>{title}</h2>
      <Markdown source={body} />
    </section>
  );
}

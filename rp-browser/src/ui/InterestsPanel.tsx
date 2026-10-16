/**
 * The Interests tab: a profile's typed interests (`rp:researchInterests`),
 * one effective entry per concept, grouped by weight. Read-only, because the
 * browser is shared by sites that cannot edit.
 */
import type { ResearchInterest } from "@rp/ui-lib/types";
import { conceptKey, conceptText, groupInterests, sourceOf, vocabularyOf } from "@rp/ui-lib";

interface Props {
  /** Every stored entry; precedence is applied here. */
  entries: ResearchInterest[];
  /** The `focus` section was held back from this viewer. */
  withheld?: boolean;
}

function InterestRow({ e }: { e: ResearchInterest }) {
  const vocab = vocabularyOf(e.concept);
  const id = e.concept["@id"];
  const share = e.evidence?.share;
  return (
    <li className="profile-interests__item">
      <span className="profile-interests__label">{conceptText(e.concept)}</span>
      {vocab && id ? (
        <a className="profile-interests__chip" href={id} target="_blank" rel="noreferrer">
          {vocab}
        </a>
      ) : (
        <span className="profile-interests__chip">{vocab ?? "text"}</span>
      )}
      <span className="profile-interests__source">{sourceOf(e)}</span>
      {share != null && (
        <span className="profile-interests__share">in {Math.round(share * 100)}% of papers</span>
      )}
    </li>
  );
}

export function InterestsPanel({ entries, withheld }: Props) {
  const groups = groupInterests(entries);
  if (groups.length === 0) {
    return (
      <p className="text-muted">
        {withheld ? "Interests are private on this profile." : "No interests recorded yet."}
      </p>
    );
  }
  return (
    <div className="profile-interests">
      {groups.map((g) => (
        <section key={g.band} className="profile-interests__group" data-band={g.band}>
          <h3 className="profile-interests__title">
            {g.title} <span className="profile-interests__count">({g.entries.length})</span>
          </h3>
          <ul className="list-plain">
            {g.entries.map((e) => (
              <InterestRow key={conceptKey(e.concept)} e={e} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

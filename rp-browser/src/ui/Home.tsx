/**
 * The landing page at `#/`. A first-time visitor lands here rather than in the
 * inventory, so the page explains what a researcher profile is before showing
 * any sources. It is the front door: a hero, one paragraph that says what this
 * is, and two clear next steps. A host application can replace the calls to
 * action through the shell slots.
 */
import { Link } from "react-router";
import type { ReactNode } from "react";
import { useShellSlots, type HomeAction } from "../slots";

/**
 * Render a CTA as an in-app <Link> for an internal route (path starts with
 * "/") or a plain <a> for an external URL (the provider login link).
 */
function Cta({
  href,
  className,
  children,
}: {
  href: string;
  className: string;
  children: ReactNode;
}) {
  if (href.startsWith("/")) {
    return (
      <Link to={href} className={className}>
        {children}
      </Link>
    );
  }
  return (
    <a href={href} className={className}>
      {children}
    </a>
  );
}

export function Home() {
  const slots = useShellSlots();

  const actions: HomeAction[] = slots.homeActions ?? [
    { label: "Browse profiles", href: "/browse", primary: true },
    { label: "What is this?", href: "/about" },
  ];

  return (
    <div className="home">
      <section className="home__hero">
        <p className="home__eyebrow">{__RP_APP_NAME__}</p>
        <h1 className="home__title">
          Your research identity, made machine-readable.
        </h1>
        <p className="home__lede">
          A researcher profile is an open, structured record of who you are, what
          you work on, and what you have published, in a form other people and
          their AI agents can read, match, and cite. Publish yours, or
          browse everyone else&rsquo;s.
        </p>
        <div className="home__actions">
          {actions.map((a) => (
            <Cta
              key={a.href + a.label}
              href={a.href}
              className={a.primary ? "btn btn--primary" : "btn btn--secondary"}
            >
              {a.label}
            </Cta>
          ))}
        </div>
      </section>

      <section className="home__features">
        <div className="home__feature">
          <h3 className="home__feature-title">Open format</h3>
          <p className="home__feature-body">
            Every profile is a folder of static JSON-LD files anchored by one
            manifest. Standard schema.org vocabulary, readable with any JSON parser.
          </p>
        </div>
        <div className="home__feature">
          <h3 className="home__feature-title">Built for matching</h3>
          <p className="home__feature-body">
            Expertise, topics, and embeddings travel with the profile, so tools can
            rank, cluster, and match researchers on real content, not names alone.
          </p>
        </div>
        <div className="home__feature">
          <h3 className="home__feature-title">Readable by agents</h3>
          <p className="home__feature-body">
            A profile is a self-describing artifact any client (this browser, a
            search engine, or an AI agent) can read directly and act on.
          </p>
        </div>
      </section>
    </div>
  );
}

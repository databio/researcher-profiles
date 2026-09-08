import { useShellSlots } from "../slots";

const REPO_URL = "https://github.com/databio/researcher-profiles";

export function About() {
  const slots = useShellSlots();

  return (
    <div className="content-page">
      <h1 className="content-page__title">About</h1>

      <section className="content-page__section">
        <h2 className="content-page__h2">What is this browser?</h2>
        <p>
          <strong>{__RP_APP_NAME__}</strong> is a small web app for
          reading <em>published researcher profiles</em>. It renders each
          researcher&rsquo;s expertise, papers, and AI-generated summaries
          directly in your browser.
        </p>
        {slots.aboutNote ?? (
          <p>
            Everything runs client-side. There is no server and no account: the
            app fetches the public files a profile publishes and displays them.
            Add a source from the sidebar to get started.
          </p>
        )}
      </section>

      <section className="content-page__section">
        <h2 className="content-page__h2">What is a researcher profile?</h2>
        <p>
          A <strong>published researcher profile</strong> is an open, machine- and
          human-readable format describing a researcher and their work. Each
          profile is a folder of static files anchored by a single manifest,{" "}
          <code>profile.jsonld</code>, which links out to everything else
          (expertise, a papers list, per-paper summaries, embeddings for search).
        </p>
        <p>
          The format is built on standard{" "}
          <a href="https://json-ld.org/" target="_blank" rel="noopener noreferrer">
            JSON-LD
          </a>{" "}
          and{" "}
          <a href="https://schema.org/" target="_blank" rel="noopener noreferrer">
            schema.org
          </a>{" "}
          vocabulary, so a profile is a portable, self-describing artifact that any
          client (this browser, a search engine, or an AI agent) can read without
          special tooling. A client only ever hard-codes the path to{" "}
          <code>profile.jsonld</code>; every other file is discovered through a
          typed link in that manifest.
        </p>
        <p>
          Every profile shares one JSON-LD{" "}
          <a href="/context/v1.jsonld" target="_blank" rel="noopener noreferrer">
            <code>@context</code> document
          </a>
          , which maps each term to <code>schema.org</code> and a small
          researcher-profile vocabulary. It is what makes a profile expand into
          unambiguous linked data; read it to see exactly what every field means.
        </p>
      </section>

      <section className="content-page__section">
        <h2 className="content-page__h2">Learn more</h2>
        <p>
          The format specification, the publishing toolkit, and this browser are
          all open source.
        </p>
        <p>
          <a
            className="content-page__repo-link"
            href={REPO_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            github.com/databio/researcher-profiles →
          </a>
        </p>
      </section>
    </div>
  );
}

import { useState, useCallback } from "react";
import { useSearchParams } from "react-router";
import type { CheckResult } from "../validate/types";
import { runValidation } from "../validate/runner";
import { runComplianceValidation } from "../validate/complianceRunner";

type Tab = "profile" | "compliance";

export function Validator() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>(
    searchParams.get("tab") === "compliance" ? "compliance" : "profile",
  );
  const [url, setUrl] = useState(searchParams.get("u") ?? "");
  const [apiRoot, setApiRoot] = useState(searchParams.get("api") ?? "");
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<CheckResult[]>([]);
  const [verdict, setVerdict] = useState<string | null>(null);

  const handleRunProfile = useCallback(async () => {
    const target = url.trim();
    if (!target) return;
    setRunning(true);
    setResults([]);
    setVerdict(null);
    setSearchParams({ u: target, tab: "profile" });

    try {
      await runValidation(target, (check) => {
        setResults((prev) => [...prev, check]);
      });
      setVerdict("done");
    } catch (e) {
      setVerdict(`error: ${e}`);
    } finally {
      setRunning(false);
    }
  }, [url]);

  const handleRunCompliance = useCallback(async () => {
    const profileUrl = url.trim() || null;
    const api = apiRoot.trim() || null;
    if (!profileUrl && !api) return;
    setRunning(true);
    setResults([]);
    setVerdict(null);
    const params: Record<string, string> = { tab: "compliance" };
    if (profileUrl) params.u = profileUrl;
    if (api) params.api = api;
    setSearchParams(params);

    try {
      await runComplianceValidation(profileUrl, api, (check) => {
        setResults((prev) => [...prev, check]);
      });
      setVerdict("done");
    } catch (e) {
      setVerdict(`error: ${e}`);
    } finally {
      setRunning(false);
    }
  }, [url, apiRoot]);

  const errors = results.filter((r) => r.severity === "error" && r.passed === false);
  const warns = results.filter((r) => r.severity === "warn" && r.passed === false);
  const badges = results.filter((r) => r.severity === "badge");
  const indeterminate = results.filter((r) => r.passed === "indeterminate");
  const passed = results.filter((r) => r.passed === true);

  return (
    <div>
      <div className="tab-bar mb-6">
        <button
          className={`tab-bar__tab ${tab === "profile" ? "tab-bar__tab--active" : ""}`}
          onClick={() => { setTab("profile"); setResults([]); setVerdict(null); }}
        >
          Profile Validator
        </button>
        <button
          className={`tab-bar__tab ${tab === "compliance" ? "tab-bar__tab--active" : ""}`}
          onClick={() => { setTab("compliance"); setResults([]); setVerdict(null); }}
        >
          Server Compliance
        </button>
      </div>

      {tab === "profile" && (
        <>
          <p className="validator__desc">
            Paste a profile base URL, a profile list URL, or a registry URL. The
            validator fetches it <em>from your browser</em>, exercising the exact
            same cross-origin constraints a real client faces.
          </p>
          <div className="input-row mb-6">
            <input
              className="input-row__field"
              type="url"
              placeholder="https://example.com/profiles/jane/"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleRunProfile()}
            />
            <button
              className="btn btn--primary"
              onClick={handleRunProfile}
              disabled={running}
            >
              {running ? "Running..." : "Validate"}
            </button>
          </div>
        </>
      )}

      {tab === "compliance" && (
        <>
          <p className="validator__desc">
            Test a server against the RP Static API and Dynamic API specifications.
            Provide a profile base URL, a dynamic API root, or both.
          </p>
          <div className="input-row mb-4">
            <input
              className="input-row__field"
              type="url"
              placeholder="Profile base URL (static API)"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleRunCompliance()}
            />
          </div>
          <div className="input-row mb-6">
            <input
              className="input-row__field"
              type="url"
              placeholder="API root, e.g. https://host/api/v1 (dynamic API)"
              value={apiRoot}
              onChange={(e) => setApiRoot(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleRunCompliance()}
            />
            <button
              className="btn btn--primary"
              onClick={handleRunCompliance}
              disabled={running || (!url.trim() && !apiRoot.trim())}
            >
              {running ? "Running..." : "Test Compliance"}
            </button>
          </div>
        </>
      )}

      {results.length > 0 && (
        <div className="flex flex-col gap-4">
          {errors.length > 0 && (
            <section>
              <h3 className="validator__section-error">Errors ({errors.length})</h3>
              {errors.map((r) => (
                <CheckResultCard key={r.id} result={r} />
              ))}
            </section>
          )}
          {warns.length > 0 && (
            <section>
              <h3 className="validator__section-warn">Warnings ({warns.length})</h3>
              {warns.map((r) => (
                <CheckResultCard key={r.id} result={r} />
              ))}
            </section>
          )}
          {badges.length > 0 && (
            <section>
              <h3 className="validator__section-badge">Badges ({badges.length})</h3>
              {badges.map((r) => (
                <CheckResultCard key={r.id} result={r} />
              ))}
            </section>
          )}
          {indeterminate.length > 0 && (
            <section>
              <h3 className="validator__section-warn">Indeterminate ({indeterminate.length})</h3>
              {indeterminate.map((r) => (
                <CheckResultCard key={r.id} result={r} />
              ))}
            </section>
          )}
          {passed.length > 0 && (
            <section>
              <h3 className="validator__section-passed">Passed ({passed.length})</h3>
              {passed.map((r) => (
                <CheckResultCard key={r.id} result={r} />
              ))}
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function CheckResultCard({ result }: { result: CheckResult }) {
  return (
    <div className="validator__check">
      <div className="validator__check-header">
        <span
          className={
            result.passed === true
              ? "validator__icon--pass"
              : result.passed === false
                ? "validator__icon--fail"
                : "validator__icon--indeterminate"
          }
        >
          {result.passed === true ? "OK" : result.passed === false ? "FAIL" : "?"}
        </span>
        <strong>{result.title}</strong>
      </div>
      <p className="validator__check-msg">{result.message}</p>
      {result.evidence && (
        <pre className="validator__evidence">{result.evidence}</pre>
      )}
      {result.fix && (
        <div className="validator__fix">
          <strong>Fix:</strong>
          <pre className="validator__fix-code">{result.fix}</pre>
        </div>
      )}
    </div>
  );
}

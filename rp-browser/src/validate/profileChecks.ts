/**
 * Profile conformance check suite.
 *
 * Checks run sequentially and stream live via the emit callback. Each check is
 * a pure function of FetchOutcomes so it is unit-testable without a network.
 */

import type { ArtifactRef } from "../model/manifest";
import type { CheckResult } from "./types";
import type { FetchOutcome } from "../net/fetchJson";
import { fetchJson } from "../net/fetchJson";
import { fetchBinary } from "../net/fetchBinary";
import { getFix } from "./fixes";
import { validateAgainstSchema, schemaIdForRole } from "./schemaValidation";
import { TEXT_ARTIFACT_ROLES, textArtifactProblems } from "./textArtifact";
import { orcidLoginChecks } from "./orcidLogin";

type Emit = (check: CheckResult) => void;

interface Manifest {
  "@context"?: string;
  "@id"?: string;
  "@type"?: string;
  name?: string;
  rid?: string;
  orcid?: string | null;
  level?: string;
  conformsTo?: string[];
  hasPart?: ArtifactRef[];
  subjectOf?: ArtifactRef[];
  [key: string]: unknown;
}

/**
 * The format IRI a conforming profile.jsonld must declare in `conformsTo`.
 * Mirrors PROFILE_FORMAT_IRI in the Python package (jsonld.py).
 */
const PROFILE_FORMAT_IRI = "https://profiles.databio.org/context/v1.jsonld";

/** Known dimensions per backend_spec. */
const KNOWN_DIMS: Record<string, number> = {
  "st:all-MiniLM-L6-v2": 384,
  "st:all-mpnet-base-v2": 768,
  "openai:text-embedding-3-small": 1536,
  "openai:text-embedding-3-large": 3072,
};

function entriesOf(manifest: Manifest): ArtifactRef[] {
  return [...(manifest.hasPart ?? []), ...(manifest.subjectOf ?? [])];
}

export async function profileChecks(
  base: string,
  manifestOutcome: FetchOutcome<unknown>,
  emit: Emit,
): Promise<CheckResult[]> {
  const checks: CheckResult[] = [];

  function add(check: CheckResult) {
    checks.push(check);
    emit(check);
  }

  // --- reachable ---
  add({
    id: "reachable",
    title: "Manifest Reachable",
    severity: "error",
    passed: manifestOutcome.ok,
    message: manifestOutcome.ok
      ? `profile.jsonld resolved successfully.`
      : `Could not fetch profile.jsonld: ${manifestOutcome.ok ? "" : manifestOutcome.detail}`,
    evidence: manifestOutcome.url,
  });

  if (!manifestOutcome.ok) return checks;

  // --- cors ---
  add({
    id: "cors",
    title: "CORS",
    severity: "error",
    passed: true,
    message: "Cross-origin access is permitted.",
  });

  // --- content-type-jsonld ---
  const ct = manifestOutcome.ok ? manifestOutcome.contentType : null;
  const ctOk = ct?.includes("application/ld+json") ?? false;
  const ctJson = ct?.includes("application/json") ?? false;
  const ctOctet = ct?.includes("application/octet-stream") ?? false;
  const ctText = ct?.includes("text/plain") ?? false;
  add({
    id: "content-type-jsonld",
    title: "Content-Type",
    severity: "warn",
    passed: ctOk || ctJson,
    message: ctOk
      ? "Content-Type is application/ld+json."
      : ctJson
        ? "Content-Type is application/json (acceptable but application/ld+json is preferred)."
        : ctOctet || ctText
          ? `Content-Type is ${ct}. This is the classic S3/R2 unknown-extension symptom.`
          : `Content-Type is ${ct || "missing"}.`,
    evidence: ct || undefined,
    fix: (ctOctet || ctText) ? getFix("content-type-jsonld", manifestOutcome.url) : undefined,
  });

  const manifest = manifestOutcome.value as Manifest;
  // `@id` is the researcher's IRI, not the hosting location, so relative
  // `contentUrl`s resolve against the fetched `base`, never against `@id`.
  const rawId = typeof manifest["@id"] === "string" ? manifest["@id"] : "";

  // --- profile-schema ---
  // `@id` is nullable in the schema, so it is not required here.
  const requiredFields = ["@context", "name", "rid"];
  const missingFields = requiredFields.filter((f) => !(f in manifest));
  const hasManifestArrays =
    Array.isArray(manifest.hasPart) || Array.isArray(manifest.subjectOf);
  if (!hasManifestArrays) missingFields.push("hasPart");

  // `conformsTo` may be a string or an array; either must hold the format IRI.
  const conformsRaw = manifest.conformsTo as unknown;
  const conformsList = Array.isArray(conformsRaw)
    ? (conformsRaw as unknown[]).map(String)
    : conformsRaw != null
      ? [String(conformsRaw)]
      : [];
  const conformsOk = conformsList.includes(PROFILE_FORMAT_IRI);

  const schemaPassed = missingFields.length === 0 && conformsOk;
  const schemaProblems = [
    ...(missingFields.length > 0
      ? [`missing required fields: ${missingFields.join(", ")}`]
      : []),
    ...(!conformsOk
      ? [
          `conformsTo must be "${PROFILE_FORMAT_IRI}", got ${
            conformsList.length ? conformsList.join(", ") : "nothing"
          }`,
        ]
      : []),
  ];
  add({
    id: "profile-schema",
    title: "Profile Schema",
    severity: "error",
    passed: schemaPassed,
    message: schemaPassed
      ? "Profile document has all required fields and declares the profile format."
      : `Profile document is invalid: ${schemaProblems.join("; ")}.`,
    evidence: schemaProblems.length > 0 ? JSON.stringify(schemaProblems) : undefined,
  });

  if (!schemaPassed) return checks;

  // --- ajv schema validation of the manifest ---
  add(validateAgainstSchema("profile_jsonld", manifest, "profile.jsonld"));

  // --- manifest-identity ---
  // An ORCID `@id` must agree with `rid`: a copy-pasted document carrying
  // someone else's identity fails silently otherwise. Other absolute IRIs, a
  // relative id that is the tail of the fetched base, a fragment id (`#me`),
  // or no `@id` all pass.
  const orcidMatch = /orcid\.org\/([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X])/i.exec(
    rawId,
  );
  let identityPassed = true;
  let identityMessage = rawId
    ? `@id (${rawId}) identifies the profile subject.`
    : "No @id declared; identity resolves to the fetched base.";
  if (orcidMatch) {
    const idOrcid = orcidMatch[1];
    const rid = String(manifest.rid ?? "");
    identityPassed = rid === idOrcid;
    identityMessage = identityPassed
      ? `@id ORCID matches rid (${rid}).`
      : `@id ORCID (${idOrcid}) does not match rid (${rid}). A copy-pasted document carrying someone else's identity is a real and silent failure mode.`;
  } else if (rawId && !/^https?:\/\//i.test(rawId) && !rawId.startsWith("#")) {
    // A relative @id must be the tail of the base we fetched.
    identityPassed = base.endsWith(rawId);
    identityMessage = identityPassed
      ? `Relative @id matches the fetched base URL.`
      : `Relative @id (${rawId}) is not a suffix of the fetched base (${base}).`;
  }
  add({
    id: "manifest-identity",
    title: "Manifest Identity",
    severity: "error",
    passed: identityPassed,
    message: identityMessage,
    evidence: `@id: ${rawId || "(none)"}, base: ${base}`,
  });

  // --- proof-orcid-login / orcid-verified ---
  // A registry-issued ORCID login proof, trusted only from the origin that
  // served this document. No proof, no check.
  for (const c of orcidLoginChecks(manifest, manifestOutcome.finalUrl)) add(c);

  // --- required-roles ---
  const entries = entriesOf(manifest);
  const roles = new Set(entries.map((e) => e.role).filter(Boolean) as string[]);
  const level = manifest.level || "full";

  const requiredRoles: Record<string, string[]> = {
    lite: ["works"],
    full: ["works", "expertise", "soul"],
    deep: ["works", "expertise", "soul"],
  };

  const required = requiredRoles[level] || requiredRoles.full;
  const missingRoles = required.filter((r) => !roles.has(r));
  add({
    id: "required-roles",
    title: "Required Files",
    severity: "error",
    passed: missingRoles.length === 0,
    message: missingRoles.length === 0
      ? `All required manifest roles for level "${level}" are present.`
      : `Missing required manifest roles for level "${level}": ${missingRoles.join(", ")}`,
  });

  // --- artifact-reachable ---
  // Every declared `contentUrl` must fetch. Templated (`{...}`) and absolute
  // contentUrls are skipped: neither resolves under this profile's base.
  const unreachable: Array<{ role: string; href: string; detail: string }> = [];
  const mismatches: string[] = [];
  const fetchedJson: Array<{ role: string; data: unknown }> = [];
  let textChecked = 0;
  const garbled: Array<{ href: string; problems: string[] }> = [];
  let corsFix: CheckResult["fix"] | undefined;
  for (const entry of entries.slice(0, 40)) {
    const contentUrl = entry.contentUrl || "";
    if (!contentUrl || contentUrl.includes("{") || /^https?:\/\//.test(contentUrl)) continue;

    const href = new URL(contentUrl, base).href;
    const declaredBase = (entry.encodingFormat || "").split(";")[0].trim();
    const isJsonPart =
      declaredBase === "application/json" || declaredBase === "application/ld+json";
    const partOutcome = isJsonPart
      ? await fetchJson<unknown>(href)
      : await fetchBinary(href);

    const roleLabel = entry.role || "part";
    if (!partOutcome.ok) {
      unreachable.push({ role: roleLabel, href, detail: partOutcome.detail });
      if (partOutcome.kind === "cors" && !corsFix) corsFix = getFix("cors", href);
      continue;
    }
    if (isJsonPart && "value" in partOutcome && entry.role) {
      fetchedJson.push({ role: entry.role, data: partOutcome.value });
    }
    if (
      !isJsonPart &&
      entry.role &&
      TEXT_ARTIFACT_ROLES.has(entry.role) &&
      partOutcome.value instanceof ArrayBuffer
    ) {
      textChecked++;
      const problems = textArtifactProblems(partOutcome.value);
      if (problems.length > 0) garbled.push({ href: contentUrl, problems });
    }
    if (entry.encodingFormat && partOutcome.contentType) {
      const declared = entry.encodingFormat.split(";")[0].trim();
      const actual = partOutcome.contentType.split(";")[0].trim();
      if (declared !== actual) {
        mismatches.push(`${roleLabel}: Content-Type declared ${declared}, got ${actual}`);
      }
    }
    if (entry.bytes != null && partOutcome.bytes !== entry.bytes) {
      mismatches.push(`${roleLabel}: size declared ${entry.bytes}, got ${partOutcome.bytes}`);
    }
  }
  add({
    id: "artifact-reachable",
    title: "Artifacts Reachable",
    severity: "error",
    passed: unreachable.length === 0,
    message: unreachable.length === 0
      ? `All declared manifest artifacts resolve.${mismatches.length ? ` Notes: ${mismatches.join("; ")}.` : ""}`
      : `${unreachable.length} declared artifact(s) do not resolve: ${unreachable
          .map((u) => `${u.role} (${u.href}): ${u.detail}`)
          .join("; ")}.`,
    evidence: unreachable.length > 0 ? unreachable.map((u) => u.href).join(", ") : undefined,
    fix: corsFix,
  });

  // --- text-artifacts ---
  // Same rule as `rp validate` (researcher_profiles.text_artifact), but only
  // on the files fetched above, so a large profile is sampled.
  if (textChecked > 0) {
    add({
      id: "text-artifacts",
      title: "Paper Text Is Clean",
      severity: "error",
      passed: garbled.length === 0,
      message:
        garbled.length === 0
          ? `${textChecked} paper text file(s) checked; all are clean text.`
          : `${garbled.length} of ${textChecked} paper text file(s) are garbled: ${garbled
              .map((g) => `${g.href} (${g.problems.join("; ")})`)
              .join("; ")}.`,
      evidence: garbled.length > 0 ? garbled.map((g) => g.href).join(", ") : undefined,
    });
  }

  // --- schema validation of fetched auxiliary documents ---
  for (const { role, data } of fetchedJson) {
    const sid = schemaIdForRole(role);
    if (sid) {
      add(validateAgainstSchema(sid, data, role));
    }
  }

  // --- embedding checks ---
  const embeddingEntry = entries.find((e) => e.role === "embedding_index");
  if (embeddingEntry) {
    const indexUrl = new URL(embeddingEntry.contentUrl, base).href;
    const indexOutcome = await fetchJson<Record<string, unknown>>(indexUrl);

    if (indexOutcome.ok) {
      const idx = indexOutcome.value;

      add(validateAgainstSchema("embedding_index", idx, "embedding_index"));

      // backend-spec
      const backendSpec = idx.backend_spec as string | undefined;
      const specGrammar = /^(st|openai|voyage):/;
      add({
        id: "backend-spec",
        title: "Backend Spec",
        severity: "error",
        passed: !!backendSpec && specGrammar.test(backendSpec),
        message: backendSpec
          ? specGrammar.test(backendSpec)
            ? `Backend spec: ${backendSpec}`
            : `Backend spec "${backendSpec}" does not match the expected grammar (st:|openai:|voyage:)`
          : "Missing backend_spec in embeddings/index.json",
      });

      // centroid-dim
      const dim = idx.dim as number | undefined;
      const centroidEntry = entries.find((e) => e.role === "centroid");
      if (centroidEntry && dim) {
        const centroidUrl = new URL(centroidEntry.contentUrl, base).href;
        const centroidOutcome = await fetchBinary(centroidUrl);
        const expectedBytes = dim * 4;

        add({
          id: "centroid-dim",
          title: "Centroid Dimensions",
          severity: "error",
          passed: centroidOutcome.ok && centroidOutcome.value.byteLength === expectedBytes,
          message: centroidOutcome.ok
            ? centroidOutcome.value.byteLength === expectedBytes
              ? `Centroid blob is correct size: ${expectedBytes} bytes (${dim} x 4)`
              : `Centroid blob size mismatch: expected ${expectedBytes} (${dim} x 4), got ${centroidOutcome.value.byteLength}`
            : `Failed to fetch centroid: ${centroidOutcome.detail}`,
          evidence: centroidUrl,
        });

        if (backendSpec && backendSpec in KNOWN_DIMS) {
          const knownDim = KNOWN_DIMS[backendSpec];
          add({
            id: "centroid-dim-known",
            title: "Known Model Dimension",
            severity: "error",
            passed: dim === knownDim,
            message: dim === knownDim
              ? `Dimension ${dim} matches known dimension for ${backendSpec}.`
              : `Dimension ${dim} does not match known dimension ${knownDim} for ${backendSpec}. This catches silent wrong-numbers failures.`,
          });
        }
      }

      // probe
      const probe = idx.probe as { text?: string; vector?: number[] } | undefined;
      add({
        id: "probe",
        title: "Embedding Probe",
        severity: "error",
        passed: !!probe?.text && Array.isArray(probe?.vector) && probe.vector.length > 0,
        message: probe?.text
          ? `Probe present: "${probe.text.slice(0, 50)}..."`
          : "Missing probe in embeddings/index.json. A published embeddings/index.json without a probe fails the Searchable class.",
      });

      // vector-sanity
      if (centroidEntry) {
        const centroidUrl = new URL(centroidEntry.contentUrl, base).href;
        const centroidOutcome = await fetchBinary(centroidUrl);
        if (centroidOutcome.ok && dim) {
          const floats = new Float32Array(centroidOutcome.value);
          let hasNaN = false;
          let hasInf = false;
          let normIssues = 0;

          const count = floats.length / dim;
          for (let i = 0; i < count; i++) {
            let norm = 0;
            for (let j = 0; j < dim; j++) {
              const v = floats[i * dim + j];
              if (isNaN(v)) hasNaN = true;
              if (!isFinite(v)) hasInf = true;
              norm += v * v;
            }
            norm = Math.sqrt(norm);
            if (Math.abs(norm - 1.0) > 1e-3) normIssues++;
          }

          add({
            id: "vector-sanity",
            title: "Vector Sanity",
            severity: "warn",
            passed: !hasNaN && !hasInf && normIssues === 0,
            message: hasNaN
              ? "Centroid vectors contain NaN values."
              : hasInf
                ? "Centroid vectors contain Infinity values."
                : normIssues > 0
                  ? `${normIssues} of ${count} centroid rows are not unit-normalized (norm not within 1e-3 of 1.0).`
                  : `All ${count} centroid rows are unit-normalized, no NaN/Inf.`,
          });
        }
      }
    } else {
      add({
        id: "backend-spec",
        title: "Backend Spec",
        severity: "error",
        passed: false,
        message: `Failed to fetch embeddings index: ${indexOutcome.detail}`,
        evidence: indexUrl,
      });
    }
  }

  // --- paper-summary ---
  // Only the first summary is checked.
  const summaryEntry = entries.find((e) => e.role === "paper_summary" && e.contentUrl);
  if (summaryEntry) {
    const summaryUrl = new URL(summaryEntry.contentUrl, base).href;
    // Markdown, so reachability only.
    const summaryOutcome = await fetchBinary(summaryUrl);
    add({
      id: "paper-summary",
      title: "Paper Summary",
      severity: "error",
      passed: summaryOutcome.ok,
      message: summaryOutcome.ok
        ? `Paper summary resolves${summaryEntry.paperId ? ` for "${summaryEntry.paperId}"` : ""}.`
        : `A paper_summary entry${summaryEntry.paperId ? ` for "${summaryEntry.paperId}"` : ""} 404s. This makes the browser's paper rows dead-end.`,
      evidence: summaryUrl,
    });
  }

  return checks;
}

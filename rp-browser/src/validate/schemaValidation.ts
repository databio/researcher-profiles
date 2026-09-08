import Ajv from "ajv";
import addFormats from "ajv-formats";
import type { CheckResult } from "./types";

// JSON Schemas are generated from rp-sdk's pydantic models and live in exactly
// one place: rp-sdk/schemas/ (`rp schema export schemas/`). Resolved through
// the @rp/schemas alias. See rp-browser/vite.config.ts + tsconfig.json.
// The explorer imports only the six it validates against; the SDK ships the
// full set.
import profileSchema from "@rp/schemas/profile_jsonld.schema.json";
import embeddingIndexSchema from "@rp/schemas/embedding_index.schema.json";
import papersSchema from "@rp/schemas/papers_jsonld.schema.json";
import collectionSchema from "@rp/schemas/collection.schema.json";
import profileListSchema from "@rp/schemas/profile_list.schema.json";
import topicIndexSchema from "@rp/schemas/topic_index.schema.json";

const ajv = new Ajv({ allErrors: true, strict: false });
addFormats(ajv);

const validators = new Map<string, ReturnType<typeof ajv.compile>>();

function getValidator(schemaId: string): ReturnType<typeof ajv.compile> | null {
  if (validators.has(schemaId)) return validators.get(schemaId)!;
  const schema = SCHEMA_MAP[schemaId];
  if (!schema) return null;
  const v = ajv.compile(schema);
  validators.set(schemaId, v);
  return v;
}

const SCHEMA_MAP: Record<string, object> = {
  profile_jsonld: profileSchema,
  embedding_index: embeddingIndexSchema,
  papers_jsonld: papersSchema,
  collection: collectionSchema,
  profile_list: profileListSchema,
  topic_index: topicIndexSchema,
};

const ROLE_TO_SCHEMA: Record<string, string> = {
  embedding_index: "embedding_index",
  works: "papers_jsonld",
};

export function validateAgainstSchema(
  schemaId: string,
  data: unknown,
  label: string,
): CheckResult {
  const validate = getValidator(schemaId);
  if (!validate) {
    return {
      id: `schema-${schemaId}`,
      title: `Schema: ${label}`,
      severity: "warn",
      passed: "indeterminate",
      message: `No schema found for ${schemaId}.`,
    };
  }

  const valid = validate(data);
  if (valid) {
    return {
      id: `schema-${schemaId}`,
      title: `Schema: ${label}`,
      severity: "warn",
      passed: true,
      message: `${label} conforms to its JSON Schema.`,
    };
  }

  const errors = (validate.errors ?? []).slice(0, 5);
  const summary = errors
    .map((e) => `${e.instancePath || "/"}: ${e.message}`)
    .join("; ");

  return {
    id: `schema-${schemaId}`,
    title: `Schema: ${label}`,
    severity: "warn",
    passed: false,
    message: `${label} has schema violations: ${summary}`,
    evidence:
      errors.length < (validate.errors?.length ?? 0)
        ? `Showing ${errors.length} of ${validate.errors!.length} errors`
        : undefined,
  };
}

export function schemaIdForRole(role: string): string | null {
  return ROLE_TO_SCHEMA[role] ?? null;
}

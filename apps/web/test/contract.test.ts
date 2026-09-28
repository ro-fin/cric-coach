/**
 * API contract parity (T1, phase-8-plan.md "Contract parity").
 *
 * lib/api.ts mirrors the cricai_api response models by hand. This test pins
 * those mirrors to the API's real OpenAPI schema (test/openapi.json, written
 * by `uv run scripts/dump_openapi.py`; `--check` fails when it is stale).
 *
 * The check works in two halves so neither side can drift silently:
 *  - COMPILE TIME (pnpm typecheck): every field list and enum vocabulary below
 *    must be EXACTLY the TypeScript type's keys / union members. Adding a
 *    field to an interface without listing it here is a type error.
 *  - RUN TIME (pnpm test): the same lists must equal the schema's properties,
 *    required set, nullable set, $ref targets and enum values.
 *
 * Feature-local mirrors (reports, progress, notes, pipeline, ...) can use the
 * exported `expectMirrors` / `expectVocabulary` helpers from their own tests.
 * Not covered: MetricValue, because the API types `metrics` as a free-form
 * object; its {value, unit, confidence, reason?} contract is pinned by the
 * backend's own schema tests.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type {
  BowlerSource,
  ClipOut,
  ClipStatus,
  Contact,
  EventOut,
  EventSource,
  Footwork,
  LengthZone,
  Line,
  MetricPhase,
  Outcome,
  PhaseMetricsOut,
  PublishOut,
  ReportKind,
  ReportStatus,
  ReviewQueueItemOut,
  SessionOut,
  SessionPage,
  SessionState,
  SessionType,
  Shot,
  TagAuditOut,
  TagOut,
  VideoOut,
  VideoStatus,
} from "@/lib/api";

// ---------------------------------------------------------------------------
// Schema access.
// ---------------------------------------------------------------------------

interface JsonSchema {
  type?: string;
  enum?: string[];
  $ref?: string;
  anyOf?: JsonSchema[];
  items?: JsonSchema;
  properties?: Record<string, JsonSchema>;
  required?: string[];
}

// vitest runs from apps/web (pnpm test, pnpm --filter cricai-web test); under
// jsdom import.meta.url is not a file: URL, so resolve from the app root.
const OPENAPI = JSON.parse(
  readFileSync(join(process.cwd(), "test", "openapi.json"), "utf-8"),
) as { components: { schemas: Record<string, JsonSchema> } };

const SCHEMAS = OPENAPI.components.schemas;

function schemaOf(name: string): JsonSchema {
  const schema = SCHEMAS[name];
  expect(schema, `schema ${name} missing from openapi.json`).toBeDefined();
  return schema;
}

function refName(schema: JsonSchema | undefined): string | undefined {
  return schema?.$ref?.split("/").at(-1);
}

function isNullable(schema: JsonSchema): boolean {
  return (schema.anyOf ?? []).some((branch) => branch.type === "null");
}

/** The non-null branch's $ref (direct, inside anyOf, or on array items). */
function targetRef(schema: JsonSchema): string | undefined {
  const direct = schema.anyOf ? schema.anyOf.find((branch) => branch.type !== "null") : schema;
  return refName(direct) ?? refName(direct?.items);
}

// ---------------------------------------------------------------------------
// Compile-time exhaustiveness helpers.
// ---------------------------------------------------------------------------

type Exhaustive<Want extends PropertyKey, Got extends readonly PropertyKey[]> = [
  Exclude<Want, Got[number]>,
] extends [never]
  ? [Exclude<Got[number], Want>] extends [never]
    ? Got
    : never
  : never;

type OptionalKeys<T> = { [K in keyof T]-?: object extends Pick<T, K> ? K : never }[keyof T];
type NullableKeys<T> = { [K in keyof T]-?: null extends T[K] ? K : never }[keyof T];

/** Exactly the keys of T. */
const keys =
  <T>() =>
  <const K extends readonly (keyof T)[]>(list: K & Exhaustive<keyof T, K>): readonly string[] =>
    list as readonly string[];

/** Exactly the optional keys of T. */
const optionalKeys =
  <T>() =>
  <const K extends readonly OptionalKeys<T>[]>(
    list: K & Exhaustive<OptionalKeys<T>, K>,
  ): readonly string[] =>
    list as readonly string[];

/** Exactly the keys of T whose type admits null. */
const nullableKeys =
  <T>() =>
  <const K extends readonly NullableKeys<T>[]>(
    list: K & Exhaustive<NullableKeys<T>, K>,
  ): readonly string[] =>
    list as readonly string[];

/** Exactly the members of a string union. */
const vocabulary =
  <T extends string>() =>
  <const K extends readonly T[]>(list: K & Exhaustive<T, K>): readonly string[] =>
    list as readonly string[];

// ---------------------------------------------------------------------------
// Reusable assertions (exported for feature-local mirrors).
// ---------------------------------------------------------------------------

export interface Mirror {
  /** Schema name in openapi.json components. */
  schema: string;
  fields: readonly string[];
  optional?: readonly string[];
  nullable?: readonly string[];
  /** field → schema name its (non-null / item) $ref must point at. */
  refs?: Record<string, string>;
}

export function expectMirrors(mirror: Mirror): void {
  const schema = schemaOf(mirror.schema);
  const properties = schema.properties ?? {};
  const optional = new Set(mirror.optional ?? []);

  expect([...mirror.fields].sort(), `${mirror.schema}: field set`).toEqual(
    Object.keys(properties).sort(),
  );
  expect(
    mirror.fields.filter((field) => !optional.has(field)).sort(),
    `${mirror.schema}: required fields`,
  ).toEqual([...(schema.required ?? [])].sort());
  expect(
    [...(mirror.nullable ?? [])].sort(),
    `${mirror.schema}: nullable fields`,
  ).toEqual(
    Object.entries(properties)
      .filter(([, property]) => isNullable(property))
      .map(([field]) => field)
      .sort(),
  );
  for (const [field, target] of Object.entries(mirror.refs ?? {})) {
    expect(targetRef(properties[field]), `${mirror.schema}.${field}: $ref`).toBe(target);
  }
}

export function expectVocabulary(schemaName: string, values: readonly string[]): void {
  expect([...values].sort(), `${schemaName}: enum values`).toEqual(
    [...(schemaOf(schemaName).enum ?? [])].sort(),
  );
}

// ---------------------------------------------------------------------------
// lib/api.ts contract.
// ---------------------------------------------------------------------------

/** TS union name → schema enum name, with the exhaustive vocabulary. */
const VOCABULARIES: Record<string, readonly string[]> = {
  Line: vocabulary<Line>()(["outside_off", "off", "middle", "leg"]),
  Length: vocabulary<LengthZone>()(["yorker", "full", "good", "short"]),
  Shot: vocabulary<Shot>()([
    "leave",
    "defend",
    "drive",
    "cover_drive",
    "straight_drive",
    "on_drive",
    "cut",
    "pull",
    "hook",
    "sweep",
    "flick",
    "loft",
  ]),
  Footwork: vocabulary<Footwork>()(["front", "back", "leave"]),
  Contact: vocabulary<Contact>()(["middle", "edge", "miss"]),
  Outcome: vocabulary<Outcome>()([
    "controlled_ground_shot",
    "controlled_aerial",
    "uncontrolled",
    "beaten",
    "bowled",
    "edged",
    "left_alone",
  ]),
  SessionType: vocabulary<SessionType>()(["batting", "bowling", "mixed"]),
  BowlerSource: vocabulary<BowlerSource>()(["machine", "human", "coach"]),
  SessionState: vocabulary<SessionState>()([
    "created",
    "recording",
    "captured",
    "processing",
    "analyzed",
    "failed",
  ]),
  EventSource: vocabulary<EventSource>()(["auto", "manual", "corrected"]),
  ClipStatus: vocabulary<ClipStatus>()(["pending", "cut", "failed", "gap"]),
  MetricPhase: vocabulary<MetricPhase>()(["pre_release", "contact", "flight"]),
  VideoStatus: vocabulary<VideoStatus>()([
    "pending",
    "uploaded",
    "probed",
    "failed",
    "metadata_conflict",
  ]),
  ReportKind: vocabulary<ReportKind>()(["daily", "weekly", "monthly"]),
  ReportStatus: vocabulary<ReportStatus>()(["draft", "published", "blocked"]),
};

const MIRRORS: Mirror[] = [
  {
    schema: "SessionOut",
    fields: keys<SessionOut>()([
      "id",
      "player_id",
      "session_date",
      "session_type",
      "bowler_source",
      "machine_settings",
      "notes",
      "state",
      "degraded",
      "missing_views",
    ]),
    optional: optionalKeys<SessionOut>()([]),
    nullable: nullableKeys<SessionOut>()(["machine_settings", "notes"]),
    refs: { session_type: "SessionType", bowler_source: "BowlerSource", state: "SessionState" },
  },
  {
    schema: "SessionPage",
    fields: keys<SessionPage>()(["items", "total", "limit", "offset"]),
    optional: optionalKeys<SessionPage>()([]),
    nullable: nullableKeys<SessionPage>()([]),
    refs: { items: "SessionOut" },
  },
  {
    schema: "TagAuditOut",
    fields: keys<TagAuditOut>()(["actor", "field", "old_value", "new_value", "at"]),
    optional: optionalKeys<TagAuditOut>()([]),
    nullable: nullableKeys<TagAuditOut>()(["old_value", "new_value"]),
  },
  {
    schema: "TagOut",
    fields: keys<TagOut>()([
      "ball_no",
      "block_no",
      "line",
      "length",
      "shot",
      "footwork",
      "contact",
      "outcome",
      "control",
      "source",
      "ground_truth_eligible",
      "created_by",
      "audits",
    ]),
    optional: optionalKeys<TagOut>()([]),
    nullable: nullableKeys<TagOut>()(["block_no"]),
    refs: {
      line: "Line",
      length: "Length",
      shot: "Shot",
      footwork: "Footwork",
      contact: "Contact",
      outcome: "Outcome",
      audits: "TagAuditOut",
    },
  },
  {
    schema: "EventOut",
    fields: keys<EventOut>()([
      "id",
      "session_id",
      "ball_no",
      "start_ms",
      "release_ms",
      "contact_ms",
      "end_ms",
      "confidence",
      "source",
      "detector_version",
      "valid",
      "created_at",
    ]),
    optional: optionalKeys<EventOut>()([]),
    nullable: nullableKeys<EventOut>()(["contact_ms"]),
    refs: { source: "EventSource" },
  },
  {
    schema: "ClipOut",
    fields: keys<ClipOut>()([
      "id",
      "session_id",
      "ball_no",
      "camera_id",
      "object_key",
      "start_ms",
      "end_ms",
      "status",
      "error",
    ]),
    optional: optionalKeys<ClipOut>()([]),
    nullable: nullableKeys<ClipOut>()(["object_key", "error"]),
    refs: { status: "ClipStatus" },
  },
  {
    schema: "PhaseMetricsOut",
    fields: keys<PhaseMetricsOut>()(["phase", "metrics", "schema_version", "stored"]),
    optional: optionalKeys<PhaseMetricsOut>()([]),
    nullable: nullableKeys<PhaseMetricsOut>()([]),
    refs: { phase: "MetricPhase" },
  },
  {
    schema: "VideoOut",
    fields: keys<VideoOut>()([
      "id",
      "session_id",
      "camera_id",
      "object_key",
      "filename",
      "checksum_sha256",
      "size_bytes",
      "claimed_fps",
      "claimed_resolution",
      "claimed_duration_s",
      "codec",
      "status",
      "probe",
      "error",
    ]),
    optional: optionalKeys<VideoOut>()([]),
    nullable: nullableKeys<VideoOut>()([
      "claimed_fps",
      "claimed_resolution",
      "claimed_duration_s",
      "codec",
      "probe",
      "error",
    ]),
    refs: { status: "VideoStatus" },
  },
  {
    schema: "ReviewQueueItemOut",
    fields: keys<ReviewQueueItemOut>()([
      "id",
      "player_id",
      "session_id",
      "kind",
      "period_start",
      "period_end",
      "review_due_at",
      "created_at",
    ]),
    optional: optionalKeys<ReviewQueueItemOut>()([]),
    nullable: nullableKeys<ReviewQueueItemOut>()(["session_id", "review_due_at", "created_at"]),
    refs: { kind: "ReportKind" },
  },
  {
    schema: "PublishOut",
    fields: keys<PublishOut>()(["status", "reasons"]),
    optional: optionalKeys<PublishOut>()([]),
    nullable: nullableKeys<PublishOut>()([]),
    refs: { status: "ReportStatus" },
  },
];

describe("lib/api.ts mirrors the cricai_api OpenAPI schema", () => {
  it.each(Object.entries(VOCABULARIES))("enum %s matches", (schemaName, values) => {
    expectVocabulary(schemaName, values);
  });

  it.each(MIRRORS.map((mirror) => [mirror.schema, mirror] as const))(
    "%s matches field for field",
    (_name, mirror) => {
      expectMirrors(mirror);
    },
  );
});

describe("the contract checker itself", () => {
  it("rejects a drifted mirror", () => {
    expect(() => expectMirrors({ schema: "PublishOut", fields: ["status"] })).toThrow(/field set/);
    expect(() =>
      expectMirrors({ schema: "PublishOut", fields: ["status", "reasons"], optional: ["reasons"] }),
    ).toThrow(/required fields/);
    expect(() =>
      expectMirrors({ schema: "PublishOut", fields: ["status", "reasons"], nullable: ["status"] }),
    ).toThrow(/nullable fields/);
    expect(() =>
      expectMirrors({
        schema: "PublishOut",
        fields: ["status", "reasons"],
        refs: { status: "ReportKind" },
      }),
    ).toThrow(/\$ref/);
    expect(() => expectVocabulary("ReportKind", ["daily", "weekly"])).toThrow(/enum values/);
    expect(() => schemaOf("NoSuchSchema")).toThrow(/missing/);
  });
});

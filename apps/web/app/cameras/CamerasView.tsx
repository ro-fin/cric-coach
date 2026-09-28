"use client";

/**
 * Cameras (US-A1/I1 registry, US-A4 health checks), the parent's screen.
 * Shows each registered camera's active placement era exactly as the
 * registry serves it (optionally every past era), lets the parent set or
 * clear a camera's capture role in place (the server keeps the era, so
 * calibration stays valid), and lists pre-session health checks with every
 * probe's verdict and detail verbatim. Other roles see the forbidden state.
 */

import Link from "next/link";
import { useEffect, useId, useMemo, useState } from "react";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  ForbiddenState,
  PageHeader,
  Skeleton,
  useToast,
} from "@/components/ui";
import { type ApiConfig, defaultConfig } from "@/lib/api";
import { useRole } from "@/lib/auth/role";
import { accessFor, failure, type Load, LOADING, ready } from "../pipeline/access";
import { formatWhen } from "../pipeline/view";
import {
  CAMERA_ROLES,
  type CameraOut,
  type CameraRole,
  createCamerasApi,
  type HealthCheckRecordOut,
} from "./api";
import { CAMERA_ROLES_ALLOWED, checksOf, erasByCamera, offsetText, roleLabel } from "./view";

const LINK = "inline-flex min-h-11 items-center font-semibold text-accent underline underline-offset-4";
const NOT_RECORDED = "not recorded";

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-sm font-semibold text-ink-muted">{label}</dt>
      <dd className="text-ink">{value}</dd>
    </div>
  );
}

function CameraCard({
  camera,
  saving,
  onRole,
}: {
  camera: CameraOut;
  saving: boolean;
  onRole: (cameraId: string, role: CameraRole | null) => void;
}) {
  const ids = useId();
  return (
    <li data-testid={`camera-${camera.camera_id}-era${camera.era_no}`}>
      <Card aria-labelledby={`${ids}-title`}>
        <CardHeader>
          <CardTitle id={`${ids}-title`}>{camera.camera_id}</CardTitle>
          <div className="flex flex-wrap gap-2">
            <Badge tone="neutral">{`era ${camera.era_no}`}</Badge>
            <Badge tone={camera.active ? "success" : "neutral"}>
              {camera.active ? "active" : "past era"}
            </Badge>
            {camera.reserved_for_future && <Badge tone="info">reserved</Badge>}
            {camera.protected && <Badge tone="warning">protected</Badge>}
          </div>
        </CardHeader>
        <CardBody className="flex flex-col gap-4">
          <p className="text-lg text-ink">{camera.position_label}</p>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Fact label="Role" value={roleLabel(camera.role)} />
            <Fact label="Offset" value={offsetText(camera.xyz_offset_m)} />
            <Fact label="Height" value={`${camera.height_m} m`} />
            <Fact label="Frame rate" value={`${camera.fps} fps`} />
            <Fact label="Resolution" value={camera.resolution} />
            <Fact label="Lens" value={camera.lens === "" ? NOT_RECORDED : camera.lens} />
            <Fact label="Mount" value={camera.mount === "" ? NOT_RECORDED : camera.mount} />
            <Fact
              label="FOV reference"
              value={camera.fov_reference_key ?? "no reference image yet"}
            />
          </dl>
          {camera.active && (
            <label className="flex max-w-sm flex-col gap-2 font-semibold text-ink">
              {`Capture role for ${camera.camera_id}`}
              <select
                value={camera.role ?? ""}
                disabled={saving}
                onChange={(event) =>
                  onRole(
                    camera.camera_id,
                    event.target.value === "" ? null : (event.target.value as CameraRole),
                  )
                }
                className="min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-base font-normal text-ink disabled:opacity-60"
              >
                <option value="">{roleLabel(null)}</option>
                {CAMERA_ROLES.map((role) => (
                  <option key={role} value={role}>
                    {roleLabel(role)}
                  </option>
                ))}
              </select>
            </label>
          )}
        </CardBody>
      </Card>
    </li>
  );
}

function HealthCheck({ record }: { record: HealthCheckRecordOut }) {
  const checks = checksOf(record);
  return (
    <li data-testid={`health-${record.id}`} className="flex flex-col gap-2 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={record.passed ? "success" : "danger"}>{record.passed ? "passed" : "failed"}</Badge>
        <time dateTime={record.at} className="font-semibold text-ink">
          {formatWhen(record.at)}
        </time>
        {record.session_id !== null && (
          <Link href={`/sessions/${record.session_id}`} className={LINK}>
            Open session
          </Link>
        )}
      </div>
      {checks === null ? (
        <p className="text-ink-muted">This record has no per-check detail.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {checks.map((check) => (
            <li key={check.name} className="flex flex-wrap items-baseline gap-2">
              <Badge tone={check.passed ? "success" : "danger"}>{check.passed ? "ok" : "fail"}</Badge>
              <span className="font-mono text-sm text-ink">{check.name}</span>
              <span className="text-ink-muted">{check.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export interface CamerasViewProps {
  /** Injected for tests; defaults to the shared lib/api configuration. */
  config?: ApiConfig;
}

export default function CamerasView({ config }: CamerasViewProps) {
  const role = useRole();
  const access = accessFor(role, CAMERA_ROLES_ALLOWED);
  const { toast } = useToast();
  const api = useMemo(() => createCamerasApi(config ?? defaultConfig()), [config]);
  const [includeHistory, setIncludeHistory] = useState(false);
  const [cameras, setCameras] = useState<Load<CameraOut[]>>(LOADING);
  const [checks, setChecks] = useState<Load<HealthCheckRecordOut[]>>(LOADING);
  const [camerasAttempt, setCamerasAttempt] = useState(0);
  const [checksAttempt, setChecksAttempt] = useState(0);
  const [saving, setSaving] = useState<string | null>(null);

  useEffect(() => {
    if (access === "forbidden") {
      return;
    }
    let cancelled = false;
    setCameras(LOADING);
    api.listCameras({ includeHistory: includeHistory || undefined }).then(
      (rows) => {
        if (!cancelled) {
          setCameras(ready(rows));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setCameras(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [access, api, includeHistory, camerasAttempt]);

  useEffect(() => {
    if (access === "forbidden") {
      return;
    }
    let cancelled = false;
    setChecks(LOADING);
    api.listHealthChecks().then(
      (rows) => {
        if (!cancelled) {
          setChecks(ready(rows));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setChecks(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [access, api, checksAttempt]);

  const setRole = (list: CameraOut[], cameraId: string, next: CameraRole | null) => {
    setSaving(cameraId);
    api
      .setRole(cameraId, next)
      .then((updated) => {
        setCameras(ready(list.map((row) => (row.id === updated.id ? updated : row))));
        toast({
          title: `${updated.camera_id} role saved`,
          description: roleLabel(updated.role),
          tone: "success",
        });
      })
      .catch((error: unknown) => {
        toast({
          title: `${cameraId} role not saved`,
          description: error instanceof Error ? error.message : String(error),
          tone: "danger",
        });
      })
      .finally(() => setSaving(null));
  };

  const header = (
    <PageHeader
      title="Cameras"
      description="Where each camera sits, what it looks at, and the latest pre-session checks."
    />
  );

  if (access === "forbidden") {
    return (
      <main className="flex flex-col gap-6">
        {header}
        <ForbiddenState roles={CAMERA_ROLES_ALLOWED} />
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-6">
      {header}
      <section aria-labelledby="cameras-registry-title" className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="cameras-registry-title" className="text-xl font-semibold text-ink">
            Registered cameras
          </h2>
          <label className="flex min-h-11 cursor-pointer items-center gap-3 text-ink">
            <input
              type="checkbox"
              checked={includeHistory}
              onChange={(event) => setIncludeHistory(event.target.checked)}
              className="size-5 accent-accent"
            />
            Show past placement eras
          </label>
        </div>
        {cameras.kind === "loading" && <Skeleton lines={4} label="Loading cameras" />}
        {cameras.kind === "error" && (
          <ErrorState
            message={`Could not load cameras: ${cameras.message}`}
            onRetry={() => setCamerasAttempt((value) => value + 1)}
          />
        )}
        {cameras.kind === "forbidden" && (
          <ForbiddenState roles={CAMERA_ROLES_ALLOWED} detail={cameras.message} />
        )}
        {cameras.kind === "ready" && cameras.data.length === 0 && (
          <EmptyState
            title="No cameras registered yet"
            description="Cameras appear here once they are registered with a placement."
          />
        )}
        {cameras.kind === "ready" &&
          erasByCamera(cameras.data).map(([cameraId, eras]) => (
            <ul
              key={cameraId}
              aria-label={`${cameraId} placement eras`}
              className="grid gap-4 lg:grid-cols-2"
            >
              {eras.map((camera) => (
                <CameraCard
                  key={camera.id}
                  camera={camera}
                  saving={saving === camera.camera_id}
                  onRole={(id, next) => setRole(cameras.data, id, next)}
                />
              ))}
            </ul>
          ))}
      </section>

      <Card aria-labelledby="health-checks-title">
        <CardHeader>
          <CardTitle id="health-checks-title">Health checks</CardTitle>
        </CardHeader>
        <CardBody>
          {checks.kind === "loading" && <Skeleton lines={3} label="Loading health checks" />}
          {checks.kind === "error" && (
            <ErrorState
              message={`Could not load health checks: ${checks.message}`}
              onRetry={() => setChecksAttempt((value) => value + 1)}
            />
          )}
          {checks.kind === "forbidden" && (
            <ForbiddenState roles={CAMERA_ROLES_ALLOWED} detail={checks.message} />
          )}
          {checks.kind === "ready" && checks.data.length === 0 && (
            <EmptyState
              title="No health checks recorded yet"
              description="The capture agent records one before each session."
            />
          )}
          {checks.kind === "ready" && checks.data.length > 0 && (
            <ul aria-label="health checks, newest first" className="flex flex-col divide-y divide-border">
              {checks.data.map((record) => (
                <HealthCheck key={record.id} record={record} />
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </main>
  );
}

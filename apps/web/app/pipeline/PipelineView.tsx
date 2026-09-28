"use client";

/**
 * Pipeline run status (US-J1/L1 dashboard half): pick a session, see its runs
 * as the API lists them, and open a run's full stage trace (every attempt with
 * status, input digest, error and timings). The parent can start or resume a
 * run; the server answers a held session with 409 and that detail is shown
 * verbatim. Parents and coaches only (the router's read gate); anyone else
 * sees the forbidden state and no request is made.
 */

import { Eraser, Play, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  DegradedBanner,
  EmptyState,
  ErrorState,
  ForbiddenState,
  LinkButton,
  PageHeader,
  Skeleton,
  useToast,
} from "@/components/ui";
import {
  type ApiConfig,
  createApiClient,
  defaultConfig,
  type SessionOut,
} from "@/lib/api";
import { useRole } from "@/lib/auth/role";
import { cn } from "@/lib/cn";
import { degradedReasons } from "../pitchmap/view";
import { accessFor, failure, type Load, LOADING, ready } from "./access";
import { createPipelineApi, type RunDetailOut, type RunSummaryOut } from "./api";
import { RederiveDialog } from "./RederiveDialog";
import {
  canTrigger,
  formatWhen,
  newestRunId,
  PIPELINE_ROLES,
  sessionIdFromSearch,
  shortDigest,
  statusTone,
} from "./view";

const TH = "px-3 py-2 text-left text-sm font-semibold text-ink-muted";
const TD = "px-3 py-2 align-top";

function When({
  iso,
  pending,
  style,
}: {
  iso: string | null;
  pending?: string;
  style?: "datetime" | "time";
}) {
  if (iso === null) {
    return <span className="text-ink-muted">{formatWhen(null, pending)}</span>;
  }
  return (
    <time dateTime={iso} className="whitespace-nowrap">
      {formatWhen(iso, undefined, style)}
    </time>
  );
}

function sessionLabel(session: SessionOut): string {
  return `${session.session_date} · ${session.session_type} · ${session.state}`;
}

function RunsTable({
  runs,
  runId,
  onSelect,
}: {
  runs: RunSummaryOut[];
  runId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Pipeline runs table">
      <table data-testid="runs-table" className="w-full border-collapse">
        <caption className="sr-only">Pipeline runs for this session, oldest first</caption>
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className={TH}>Started</th>
            <th scope="col" className={TH}>Finished</th>
            <th scope="col" className={TH}>Status</th>
            <th scope="col" className={TH}>
              <span className="sr-only">Trace</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const selected = run.id === runId;
            return (
              <tr
                key={run.id}
                data-testid={`run-${run.id}`}
                className={cn("border-b border-border last:border-b-0", selected && "bg-surface-raised")}
              >
                <td className={TD}>
                  <When iso={run.started_at} />
                </td>
                <td className={TD}>
                  <When iso={run.finished_at} />
                </td>
                <td className={TD}>
                  <Badge tone={statusTone(run.status)}>{run.status}</Badge>
                </td>
                <td className={TD}>
                  <Button
                    variant={selected ? "primary" : "secondary"}
                    aria-pressed={selected}
                    onClick={() => onSelect(run.id)}
                  >
                    {selected ? "Showing trace" : "Show trace"}
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TraceTable({ run }: { run: RunDetailOut }) {
  const context = Object.entries(run.detector_context);
  return (
    <div className="flex flex-col gap-4">
      <p className="flex flex-wrap items-center gap-2 text-ink">
        <Badge tone={statusTone(run.status)}>{run.status}</Badge>
        <span>
          Started <When iso={run.started_at} />, finished <When iso={run.finished_at} />
        </span>
      </p>
      {context.length > 0 && (
        <dl data-testid="detector-context" className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
          {context.map(([key, value]) => (
            <div key={key} className="flex gap-2">
              <dt className="font-semibold text-ink-muted">{key}</dt>
              <dd className="text-ink">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {run.stages.length === 0 ? (
        <p data-testid="trace-empty" className="text-ink-muted">
          This run has not recorded any stage attempts yet.
        </p>
      ) : (
        <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Stage trace table">
          <table data-testid="trace-table" className="w-full border-collapse">
            <caption className="sr-only">Stage attempts in pipeline order</caption>
            <thead>
              <tr className="border-b border-border">
                <th scope="col" className={TH}>Stage</th>
                <th scope="col" className={TH}>Attempt</th>
                <th scope="col" className={TH}>Status</th>
                <th scope="col" className={TH}>Input digest</th>
                <th scope="col" className={TH}>Started</th>
                <th scope="col" className={TH}>Finished</th>
              </tr>
            </thead>
            {run.stages.map((stage) => (
              <tbody
                key={`${stage.stage}-${stage.attempt}`}
                data-testid={`stage-${stage.stage}-${stage.attempt}`}
                className="border-b border-border last:border-b-0"
              >
                <tr>
                  <th scope="row" className={cn(TD, "text-left font-semibold text-ink")}>
                    {stage.stage}
                  </th>
                  <td className={TD}>{stage.attempt}</td>
                  <td className={TD}>
                    <Badge tone={statusTone(stage.status)}>{stage.status}</Badge>
                  </td>
                  <td className={cn(TD, "font-mono text-sm")}>
                    <span title={stage.input_digest ?? undefined}>{shortDigest(stage.input_digest)}</span>
                  </td>
                  <td className={TD}>
                    <When iso={stage.started_at} pending="not started" style="time" />
                  </td>
                  <td className={TD}>
                    <When iso={stage.finished_at} style="time" />
                  </td>
                </tr>
                {stage.error !== null && (
                  <tr>
                    <td
                      colSpan={6}
                      className={cn(
                        "px-3 pb-3 text-base break-words",
                        stage.status === "skipped" ? "text-ink-muted" : "text-danger",
                      )}
                    >
                      {stage.error}
                    </td>
                  </tr>
                )}
              </tbody>
            ))}
          </table>
        </div>
      )}
    </div>
  );
}

export interface PipelineViewProps {
  /** Injected for tests; defaults to the shared lib/api configuration. */
  config?: ApiConfig;
}

export default function PipelineView({ config }: PipelineViewProps) {
  const role = useRole();
  const access = accessFor(role, PIPELINE_ROLES);
  const { toast } = useToast();
  const cfg = useMemo(() => config ?? defaultConfig(), [config]);
  const sessionsApi = useMemo(() => createApiClient(cfg), [cfg]);
  const pipeline = useMemo(() => createPipelineApi(cfg), [cfg]);

  const [sessions, setSessions] = useState<Load<SessionOut[]>>(LOADING);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [runs, setRuns] = useState<Load<RunSummaryOut[]>>(LOADING);
  const [runId, setRunId] = useState<string | null>(null);
  const [trace, setTrace] = useState<Load<RunDetailOut>>(LOADING);
  const [busy, setBusy] = useState<"run" | "resume" | null>(null);
  const [rederiving, setRederiving] = useState(false);
  const [sessionsAttempt, setSessionsAttempt] = useState(0);
  const [runsAttempt, setRunsAttempt] = useState(0);
  const [traceAttempt, setTraceAttempt] = useState(0);

  useEffect(() => {
    if (access === "forbidden") {
      return;
    }
    let cancelled = false;
    setSessions(LOADING);
    sessionsApi.listSessions({ limit: 50 }).then(
      (page) => {
        if (cancelled) {
          return;
        }
        setSessions(ready(page.items));
        const wanted = sessionIdFromSearch(window.location.search);
        const known = page.items.some((item) => item.id === wanted);
        setSessionId(known ? wanted : (page.items[0]?.id ?? null));
      },
      (error: unknown) => {
        if (!cancelled) {
          setSessions(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [access, sessionsApi, sessionsAttempt]);

  useEffect(() => {
    if (sessionId === null) {
      return;
    }
    let cancelled = false;
    setRuns(LOADING);
    setRunId(null);
    pipeline.listRuns(sessionId).then(
      (rows) => {
        if (!cancelled) {
          setRuns(ready(rows));
          setRunId(newestRunId(rows));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setRuns(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [pipeline, sessionId, runsAttempt]);

  useEffect(() => {
    if (runId === null) {
      return;
    }
    let cancelled = false;
    setTrace(LOADING);
    pipeline.runDetail(runId).then(
      (detail) => {
        if (!cancelled) {
          setTrace(ready(detail));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setTrace(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [pipeline, runId, traceAttempt]);

  const chooseSession = useCallback((id: string) => {
    window.history.replaceState({}, "", `/pipeline?session=${encodeURIComponent(id)}`);
    setSessionId(id);
  }, []);

  const start = useCallback(
    (kind: "run" | "resume", id: string) => {
      setBusy(kind);
      const call = kind === "run" ? pipeline.triggerRun(id) : pipeline.resumeRun(id);
      call
        .then((out) => {
          toast({
            title: `Pipeline ${out.status}`,
            description: `Run ${out.run_id}`,
            tone: statusTone(out.status),
          });
          setRunId(out.run_id);
          setRunsAttempt((value) => value + 1);
        })
        .catch((error: unknown) => {
          toast({
            title: kind === "run" ? "Pipeline not started" : "Pipeline not resumed",
            description: error instanceof Error ? error.message : String(error),
            tone: "danger",
          });
        })
        .finally(() => setBusy(null));
    },
    [pipeline, toast],
  );

  const parentActions =
    canTrigger(role) && sessionId !== null ? (
      <>
        <Button variant="primary" loading={busy === "run"} disabled={busy !== null} onClick={() => start("run", sessionId)}>
          <Play aria-hidden="true" className="size-5" />
          Run pipeline
        </Button>
        <Button variant="secondary" disabled={busy !== null} onClick={() => setRederiving(true)}>
          <Eraser aria-hidden="true" className="size-5" />
          Re-derive
        </Button>
        <Button
          variant="secondary"
          loading={busy === "resume"}
          disabled={busy !== null}
          onClick={() => start("resume", sessionId)}
        >
          <RotateCcw aria-hidden="true" className="size-5" />
          Resume
        </Button>
      </>
    ) : undefined;

  const header = (
    <PageHeader
      title="Pipeline"
      description="Where each session is in the analysis pipeline, stage by stage."
      actions={parentActions}
    />
  );

  if (access === "forbidden") {
    return (
      <main className="flex flex-col gap-6">
        {header}
        <ForbiddenState roles={PIPELINE_ROLES} />
      </main>
    );
  }

  const selected =
    sessions.kind === "ready" ? sessions.data.find((item) => item.id === sessionId) : undefined;

  return (
    <main className="flex flex-col gap-6">
      {header}
      {sessions.kind === "loading" && <Skeleton lines={4} label="Loading sessions" />}
      {sessions.kind === "error" && (
        <ErrorState
          message={`Could not load sessions: ${sessions.message}`}
          onRetry={() => setSessionsAttempt((value) => value + 1)}
        />
      )}
      {sessions.kind === "forbidden" && <ForbiddenState roles={PIPELINE_ROLES} detail={sessions.message} />}
      {sessions.kind === "ready" && sessions.data.length === 0 && (
        <EmptyState
          title="No sessions yet"
          description="Pipeline runs appear here once a session has been recorded."
          action={
            <LinkButton href="/sessions" variant="primary">
              Go to sessions
            </LinkButton>
          }
        />
      )}
      {selected !== undefined && (
        <>
          <Card aria-labelledby="pipeline-session-title">
            <CardHeader>
              <CardTitle id="pipeline-session-title">Session</CardTitle>
              <Badge tone={selected.state === "failed" ? "danger" : "neutral"}>{selected.state}</Badge>
            </CardHeader>
            <CardBody>
              <label className="flex flex-col gap-2 text-ink">
                <span className="font-semibold">Choose a session</span>
                <select
                  value={selected.id}
                  onChange={(event) => chooseSession(event.target.value)}
                  className="min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-base text-ink"
                >
                  {sessions.kind === "ready" &&
                    sessions.data.map((item) => (
                      <option key={item.id} value={item.id}>
                        {sessionLabel(item)}
                      </option>
                    ))}
                </select>
              </label>
            </CardBody>
          </Card>
          <DegradedBanner reasons={degradedReasons(selected)} />

          <Card aria-labelledby="pipeline-runs-title">
            <CardHeader>
              <CardTitle id="pipeline-runs-title">Runs</CardTitle>
            </CardHeader>
            <CardBody>
              {runs.kind === "loading" && <Skeleton lines={3} label="Loading pipeline runs" />}
              {runs.kind === "error" && (
                <ErrorState
                  message={`Could not load runs: ${runs.message}`}
                  onRetry={() => setRunsAttempt((value) => value + 1)}
                />
              )}
              {runs.kind === "forbidden" && <ForbiddenState roles={PIPELINE_ROLES} detail={runs.message} />}
              {runs.kind === "ready" && runs.data.length === 0 && (
                <EmptyState
                  title="No pipeline runs for this session yet"
                  description={
                    canTrigger(role)
                      ? "Use Run pipeline to analyse it."
                      : "The parent can start a run from this screen."
                  }
                />
              )}
              {runs.kind === "ready" && runs.data.length > 0 && (
                <RunsTable runs={runs.data} runId={runId} onSelect={setRunId} />
              )}
            </CardBody>
          </Card>

          {rederiving && (
            <RederiveDialog
              api={pipeline}
              sessionId={selected.id}
              sessionLabel={sessionLabel(selected)}
              onClose={() => setRederiving(false)}
              onDone={(out) => {
                setRederiving(false);
                setRunsAttempt((value) => value + 1);
                if (out.run !== null) {
                  setRunId(out.run.run_id);
                }
              }}
            />
          )}
          {runId !== null && (
            <Card aria-labelledby="pipeline-trace-title">
              <CardHeader>
                <CardTitle id="pipeline-trace-title">Run trace</CardTitle>
              </CardHeader>
              <CardBody>
                {trace.kind === "loading" && <Skeleton lines={5} label="Loading run trace" />}
                {trace.kind === "error" && (
                  <ErrorState
                    message={`Could not load the run trace: ${trace.message}`}
                    onRetry={() => setTraceAttempt((value) => value + 1)}
                  />
                )}
                {trace.kind === "forbidden" && (
                  <ForbiddenState roles={PIPELINE_ROLES} detail={trace.message} />
                )}
                {trace.kind === "ready" && <TraceTable run={trace.data} />}
              </CardBody>
            </Card>
          )}
        </>
      )}
    </main>
  );
}

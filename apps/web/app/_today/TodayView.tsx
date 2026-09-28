"use client";

/**
 * Today (home): the one screen the player opens before a net session.
 *
 * Shows the newest PUBLISHED daily report — one correction, one drill, one
 * goal, and the evidence clips behind the correction (each opens the session
 * player at that ball) — next to this week's bowling workload against the
 * age-band ceiling. Parent and coach also get "Start session". Every number
 * and sentence is the API's, rendered verbatim (US-K4); each panel loads on
 * its own and shows loading, empty, error and degraded states honestly.
 */

import Link from "next/link";
import { useCallback, useMemo } from "react";
import { createTodayApi } from "./api";
import type { PlayerOut, ReportOut, TodayApi, WindowSummaryOut } from "./api";
import { useLoad } from "./useLoad";
import type { LoadState } from "./useLoad";
import {
  canStartSession,
  evidenceLinks,
  latestPublished,
  pickPlayer,
  playerIdFromSearch,
  reportCaveats,
  safetyLabel,
  workloadTone,
} from "./view";
import type { Role } from "./view";

export interface TodayViewProps {
  role: Role | null;
  search: string;
  api?: TodayApi;
}

function Loading({ label }: { label: string }) {
  return (
    <div role="status" aria-label={label} className="animate-pulse text-ink-muted">
      {label}
    </div>
  );
}

function Failure({
  what,
  message,
  httpStatus,
  onRetry,
}: {
  what: string;
  message: string;
  httpStatus: number | null;
  onRetry: () => void;
}) {
  const text = httpStatus === 403 ? `Your role cannot see ${what}.` : `Could not load ${what}: ${message}`;
  return (
    <div role="alert" className="rounded-lg border border-danger p-4 text-danger">
      <p>{text}</p>
      <button type="button" className="min-h-11" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}

function Panel<T>({
  title,
  what,
  load,
  children,
}: {
  title: string;
  what: string;
  load: { state: LoadState<T>; retry: () => void };
  children: (data: T) => React.ReactNode;
}) {
  const { state, retry } = load;
  return (
    <section aria-label={title} className="rounded-xl border border-border bg-surface p-4">
      <h2 className="text-xl font-semibold text-ink">{title}</h2>
      {state.status === "loading" && <Loading label={`Loading ${what}`} />}
      {state.status === "error" && (
        <Failure
          what={what}
          message={state.message}
          httpStatus={state.httpStatus}
          onRetry={retry}
        />
      )}
      {state.status === "ready" && children(state.data)}
    </section>
  );
}

function Empty({ title, action }: { title: string; action: React.ReactNode }) {
  return (
    <div className="py-4 text-ink-muted">
      <p>{title}</p>
      {action}
    </div>
  );
}

function ReportContent({ report }: { report: ReportOut }) {
  const { body } = report;
  const caveats = reportCaveats(report);
  const safetyActive = body.safety !== null && body.safety.active;
  return (
    <div className="space-y-4">
      <p className="text-ink-muted">Report for {report.period_start}</p>
      {safetyActive && (
        <p role="alert" className="rounded-lg bg-danger p-3 text-accent-ink">
          {body.safety?.text}
        </p>
      )}
      {caveats.length > 0 && (
        <ul aria-label="Report caveats" className="rounded-lg border border-warning p-3">
          {caveats.map((text) => (
            <li key={text}>{text}</li>
          ))}
        </ul>
      )}
      <article aria-label="One correction">
        <h3 className="font-semibold">One correction</h3>
        {body.main_correction === null ? (
          <p>No correction today.</p>
        ) : (
          <>
            <p>{body.main_correction.text}</p>
            <ul aria-label="Evidence clips">
              {evidenceLinks(body.main_correction.evidence, report.session_id).map((clip) => (
                <li key={clip.key}>
                  {clip.href === null ? (
                    clip.label
                  ) : (
                    <Link href={clip.href} className="inline-flex min-h-11 items-center">
                      {clip.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </article>
      <article aria-label="One drill">
        <h3 className="font-semibold">One drill</h3>
        <p>{body.drill === null ? "No drill today." : body.drill.text}</p>
      </article>
      <article aria-label="One goal">
        <h3 className="font-semibold">One goal</h3>
        {body.goal === null ? (
          <p>No goal today.</p>
        ) : (
          <p>
            {body.goal.metric}
            {body.goal.target === null ? " — no target set" : `: ${body.goal.target}`}
          </p>
        )}
      </article>
      {body.positive !== "" && <p className="text-success">{body.positive}</p>}
      <Link href={`/reports?report_id=${report.id}`} className="inline-flex min-h-11 items-center">
        Open the full report
      </Link>
    </div>
  );
}

function WorkloadContent({ window }: { window: WindowSummaryOut }) {
  const tone = workloadTone(window);
  return (
    <div data-tone={tone} className="space-y-2">
      <p className="text-ink-muted">
        {window.window_start} to {window.window_end}
      </p>
      <p>
        {window.ceiling_overs === null
          ? `${window.weighted_overs} overs bowled — no ceiling set for this age band`
          : `${window.weighted_overs} of ${window.ceiling_overs} overs bowled`}
      </p>
      {window.remaining_balls !== null && <p>{window.remaining_balls} balls left this week</p>}
      {window.violations.length === 0 ? (
        <p className="text-success">Within the safe workload.</p>
      ) : (
        <ul aria-label="Workload warnings" className="text-danger">
          {window.violations.map((code) => (
            <li key={code}>{safetyLabel(code)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PlayerToday({ api, player }: { api: TodayApi; player: PlayerOut }) {
  const loadReports = useCallback(() => api.listDailyReports(player.id), [api, player.id]);
  const loadWorkload = useCallback(() => api.workloadSummary(player.id), [api, player.id]);
  const reports = useLoad(loadReports);
  const workload = useLoad(loadWorkload);
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <Panel title="Today's report" what="today's report" load={reports}>
        {(list) => {
          const report = latestPublished(list);
          return report === null ? (
            <Empty
              title="No published report yet."
              action={
                <Link href="/sessions" className="inline-flex min-h-11 items-center">
                  See sessions
                </Link>
              }
            />
          ) : (
            <ReportContent report={report} />
          );
        }}
      </Panel>
      <Panel title="Workload" what="the workload" load={workload}>
        {(windows) =>
          windows.length === 0 ? (
            <Empty
              title="No workload window returned."
              action={
                <Link href="/wellness" className="inline-flex min-h-11 items-center">
                  Open wellness
                </Link>
              }
            />
          ) : (
            <WorkloadContent window={windows[0]} />
          )
        }
      </Panel>
    </div>
  );
}

export default function TodayView({ role, search, api: injected }: TodayViewProps) {
  const api = useMemo(() => injected ?? createTodayApi(), [injected]);
  const loadPlayers = useCallback(() => api.listPlayers(), [api]);
  const players = useLoad(loadPlayers);
  const requested = playerIdFromSearch(search);
  const player = players.state.status === "ready" ? pickPlayer(players.state.data, requested) : null;

  return (
    <main className="mx-auto max-w-5xl space-y-4 p-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-2xl font-bold text-ink">Today</h1>
          {player !== null && <p className="text-ink-muted">{player.name}</p>}
        </div>
        {canStartSession(role) && (
          <Link
            href={player === null ? "/sessions/new" : `/sessions/new?player=${player.id}`}
            className="inline-flex min-h-11 items-center rounded-lg bg-accent px-4 text-accent-ink"
          >
            Start session
          </Link>
        )}
      </header>
      {players.state.status === "loading" && <Loading label="Loading players" />}
      {players.state.status === "error" && (
        <Failure
          what="players"
          message={players.state.message}
          httpStatus={players.state.httpStatus}
          onRetry={players.retry}
        />
      )}
      {players.state.status === "ready" &&
        (player === null ? (
          <Empty
            title="No players yet."
            action={
              <Link href="/settings" className="inline-flex min-h-11 items-center">
                Add a player in settings
              </Link>
            }
          />
        ) : (
          <PlayerToday key={player.id} api={api} player={player} />
        ))}
    </main>
  );
}

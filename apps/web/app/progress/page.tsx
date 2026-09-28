"use client";

/**
 * Progress dashboard (US-K4): one screen answering "is this working, and is
 * he safe?".
 *
 * Parent view: the workload safety panel renders FIRST and sticky (never
 * below the fold), then trend charts (with per-point n, confidence shading
 * and the suspect-data exclusion note), the milestone feed and the
 * weekly/monthly report cards. Kid mode: a simplified celebratory view —
 * streaks and milestones, no raw workload numbers, and a regression only ever
 * appears inside a coaching frame. Every number on screen is an API number
 * (US-K4 data-parity; enforced by test). Built on the design-system
 * primitives with a labelled skeleton, an empty state that points to the
 * sessions list, and an error state with retry.
 */

import { Lightbulb } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorState,
  LinkButton,
  PageHeader,
  Skeleton,
} from "@/components/ui";
import { DEFAULT_CONFIG, loadDashboard } from "./api";
import { COPY } from "./copy";
import { MilestoneFeed } from "./MilestoneFeed";
import { ReportCard } from "./ReportCard";
import { TrendChart } from "./TrendChart";
import type { DashboardData } from "./types";
import { playerIdFromSearch, regressingTrends } from "./view";
import { WorkloadPanel } from "./WorkloadPanel";

function ParentView({ data }: { data: DashboardData }) {
  const trends = data.weekly === null ? [] : data.weekly.body.trends;
  return (
    <div data-testid="parent-view" className="flex flex-col gap-6">
      <WorkloadPanel window={data.workload} wellness={data.wellness} />
      <Card aria-labelledby="trends-title">
        <CardHeader>
          <CardTitle id="trends-title">{COPY.trendsTitle}</CardTitle>
        </CardHeader>
        <CardBody className="flex flex-col gap-4">
          <p data-testid="suspect-note" className="text-sm text-ink-muted">
            {COPY.suspectNote}
          </p>
          {trends.length === 0 && (
            <div data-testid="no-trends">
              <EmptyState title={COPY.noTrends} />
            </div>
          )}
          <div className="grid gap-4 lg:grid-cols-2">
            {trends.map((trend) => (
              <TrendChart key={`${trend.metric}-${trend.zone_key}`} trend={trend} />
            ))}
          </div>
        </CardBody>
      </Card>
      <Card aria-labelledby="milestones-title">
        <CardHeader>
          <CardTitle id="milestones-title">{COPY.milestonesTitle}</CardTitle>
        </CardHeader>
        <CardBody>
          <MilestoneFeed milestones={data.milestones} kidMode={false} />
        </CardBody>
      </Card>
      <section aria-labelledby="reports-title" className="flex flex-col gap-4">
        <h2 id="reports-title" className="text-xl font-semibold text-ink">
          {COPY.reportsTitle}
        </h2>
        {data.weekly === null && data.monthly === null && (
          <div data-testid="no-reports">
            <EmptyState title={COPY.noReports} />
          </div>
        )}
        <div className="grid gap-4 lg:grid-cols-2">
          {data.weekly !== null && <ReportCard report={data.weekly} />}
          {data.monthly !== null && <ReportCard report={data.monthly} />}
        </div>
      </section>
    </div>
  );
}

function KidView({ data }: { data: DashboardData }) {
  const dips = data.weekly === null ? [] : regressingTrends(data.weekly.body.trends);
  return (
    <div data-testid="kid-view" className="flex flex-col gap-6">
      <Card aria-labelledby="wins-title">
        <CardHeader>
          <CardTitle id="wins-title">{COPY.kidTitle}</CardTitle>
        </CardHeader>
        <CardBody>
          <MilestoneFeed milestones={data.milestones} kidMode={true} />
        </CardBody>
      </Card>
      {dips.length > 0 && (
        <p
          data-testid="coaching-frame"
          className="flex items-start gap-3 rounded-xl border-2 border-info bg-surface px-5 py-4 text-lg text-ink"
        >
          <Lightbulb aria-hidden="true" className="mt-1 size-6 shrink-0 text-info" />
          {COPY.regressionFrame}
        </p>
      )}
    </div>
  );
}

type PageState =
  | { kind: "resolving" }
  | { kind: "missing-player" }
  | { kind: "loading"; playerId: string }
  | { kind: "failed"; playerId: string }
  | { kind: "ready"; playerId: string; data: DashboardData };

export default function ProgressPage() {
  const [state, setState] = useState<PageState>({ kind: "resolving" });
  const [kidMode, setKidMode] = useState(false);

  const load = useCallback((playerId: string) => {
    setState({ kind: "loading", playerId });
    loadDashboard(DEFAULT_CONFIG, playerId)
      .then((data) => setState({ kind: "ready", playerId, data }))
      .catch(() => setState({ kind: "failed", playerId }));
  }, []);

  useEffect(() => {
    const id = playerIdFromSearch(window.location.search);
    if (id === null) {
      setState({ kind: "missing-player" });
      return;
    }
    load(id);
  }, [load]);

  const ready = state.kind === "ready";
  const toggle = ready ? (
    <Button variant="secondary" onClick={() => setKidMode(!kidMode)}>
      {kidMode ? COPY.toggleToParent : COPY.toggleToKid}
    </Button>
  ) : undefined;

  return (
    <main className="flex flex-col gap-6">
      <PageHeader
        title={ready && kidMode ? COPY.kidTitle : COPY.title}
        description={ready && kidMode ? COPY.kidDescription : COPY.description}
        actions={toggle}
      />
      {(state.kind === "resolving" || state.kind === "loading") && (
        <Skeleton lines={5} label={COPY.loading} />
      )}
      {state.kind === "missing-player" && (
        <div data-testid="missing-player">
          <EmptyState
            title={COPY.pickPlayerTitle}
            description={COPY.missingPlayer}
            action={
              <LinkButton href="/sessions" variant="primary">
                {COPY.pickPlayerAction}
              </LinkButton>
            }
          />
        </div>
      )}
      {state.kind === "failed" && (
        <div data-testid="load-failed">
          <ErrorState message={COPY.loadFailed} onRetry={() => load(state.playerId)} />
        </div>
      )}
      {state.kind === "ready" &&
        (kidMode ? <KidView data={state.data} /> : <ParentView data={state.data} />)}
    </main>
  );
}

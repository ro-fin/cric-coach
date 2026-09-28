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
 * (US-K4 data-parity; enforced by test).
 */

import { useEffect, useState } from "react";
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
    <div data-testid="parent-view">
      <WorkloadPanel window={data.workload} wellness={data.wellness} />
      <section aria-label="trends">
        <h2>{COPY.trendsTitle}</h2>
        <p data-testid="suspect-note">{COPY.suspectNote}</p>
        {trends.length === 0 && <p data-testid="no-trends">{COPY.noTrends}</p>}
        {trends.map((trend) => (
          <TrendChart key={`${trend.metric}-${trend.zone_key}`} trend={trend} />
        ))}
      </section>
      <section aria-label="milestones">
        <h2>{COPY.milestonesTitle}</h2>
        <MilestoneFeed milestones={data.milestones} kidMode={false} />
      </section>
      <section aria-label="reports">
        <h2>{COPY.reportsTitle}</h2>
        {data.weekly === null && data.monthly === null && (
          <p data-testid="no-reports">{COPY.noReports}</p>
        )}
        {data.weekly !== null && <ReportCard report={data.weekly} />}
        {data.monthly !== null && <ReportCard report={data.monthly} />}
      </section>
    </div>
  );
}

function KidView({ data }: { data: DashboardData }) {
  const dips = data.weekly === null ? [] : regressingTrends(data.weekly.body.trends);
  return (
    <div data-testid="kid-view">
      <h2>{COPY.kidTitle}</h2>
      <MilestoneFeed milestones={data.milestones} kidMode={true} />
      {dips.length > 0 && <p data-testid="coaching-frame">{COPY.regressionFrame}</p>}
    </div>
  );
}

export default function ProgressPage() {
  const [playerId, setPlayerId] = useState<string | null>(null);
  const [resolved, setResolved] = useState(false);
  const [data, setData] = useState<DashboardData | null>(null);
  const [failed, setFailed] = useState(false);
  const [kidMode, setKidMode] = useState(false);

  useEffect(() => {
    const id = playerIdFromSearch(window.location.search);
    setPlayerId(id);
    setResolved(true);
    if (id === null) {
      return;
    }
    loadDashboard(DEFAULT_CONFIG, id)
      .then(setData)
      .catch(() => setFailed(true));
  }, []);

  if (!resolved) {
    return <main>{COPY.loading}</main>;
  }
  if (playerId === null) {
    return <main data-testid="missing-player">{COPY.missingPlayer}</main>;
  }
  if (failed) {
    return <main data-testid="load-failed">{COPY.loadFailed}</main>;
  }
  if (data === null) {
    return <main>{COPY.loading}</main>;
  }
  return (
    <main>
      <header>
        <h1>{kidMode ? COPY.kidTitle : COPY.title}</h1>
        <button type="button" onClick={() => setKidMode(!kidMode)}>
          {kidMode ? COPY.toggleToParent : COPY.toggleToKid}
        </button>
      </header>
      {kidMode ? <KidView data={data} /> : <ParentView data={data} />}
    </main>
  );
}

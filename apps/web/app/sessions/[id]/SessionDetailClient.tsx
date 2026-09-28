"use client";

/**
 * Session detail view (US-K1, US-B5): ball-by-ball timeline with per-ball
 * tag/event/clip summary chips, filters, multi-cam clip player and per-ball
 * metric chips. Opens on the first ball (synchronized-start playback), or on
 * the ?ball=N deep link a pitch-map ball list emits (US-K2 URL contract) —
 * selecting AND scrolling to that ball — and supports ↑/↓ ball-stepping
 * across the filtered timeline. Per-camera fps is read from the session
 * videos payload when the viewer's role can see it; otherwise the player
 * assumes the US-B2 default, visibly.
 *
 * Tablet first, video first: the selected ball's player and metrics lead the
 * page; below 1024px the filters and timeline follow underneath, above it
 * they sit in a side column.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  DegradedBanner,
  EmptyState,
  ErrorState,
  PageHeader,
  Skeleton,
} from "@/components/ui";
import { ApiError, createApiClient, defaultConfig, defaultMediaBase } from "@/lib/api";
import type { ApiClient, SessionOut } from "@/lib/api";
import { formatBallCount } from "@/lib/format";
import { BOWLER_LABELS, degradedReasons, stateTone } from "../list";
import BallMetricsPanel from "./BallMetricsPanel";
import BallTimeline from "./BallTimeline";
import { isEditableTarget } from "./keyboard";
import MultiCamPlayer from "./MultiCamPlayer";
import { fpsByCamera } from "./sync";
import {
  EMPTY_FILTER,
  applyFilter,
  blockOptions,
  buildBallRows,
  playableClips,
  stepBallNo,
} from "./timeline";
import type { BallRow, TimelineFilter } from "./timeline";
import TimelineFilters from "./TimelineFilters";

export interface SessionDetailClientProps {
  sessionId: string;
  client?: ApiClient;
  mediaBase?: string;
}

interface LoadedData {
  session: SessionOut;
  rows: BallRow[];
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return "Your role cannot see this session.";
  }
  if (error instanceof ApiError && error.status === 404) {
    return "This session does not exist.";
  }
  return `Could not load session: ${error instanceof Error ? error.message : String(error)}`;
}

/** The ?ball=N deep link from the pitch map (US-K2), or null. */
function requestedBallNo(search: string): number | null {
  const raw = new URLSearchParams(search).get("ball");
  if (raw === null) {
    return null;
  }
  const ballNo = Number(raw);
  return Number.isInteger(ballNo) && ballNo > 0 ? ballNo : null;
}

export default function SessionDetailClient({
  sessionId,
  client,
  mediaBase,
}: SessionDetailClientProps) {
  const api = useMemo(() => client ?? createApiClient(defaultConfig()), [client]);
  const media = mediaBase ?? defaultMediaBase();
  const [data, setData] = useState<LoadedData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [filter, setFilter] = useState<TimelineFilter>(EMPTY_FILTER);
  const [selectedBallNo, setSelectedBallNo] = useState<number | null>(null);
  const [deepLinkBallNo, setDeepLinkBallNo] = useState<number | undefined>(undefined);
  const [cameraFps, setCameraFps] = useState<Record<string, number> | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setData(null);
    Promise.all([
      api.getSession(sessionId),
      api.listEvents(sessionId),
      api.listTags(sessionId),
      api.listClips(sessionId),
    ]).then(
      ([session, events, tags, clips]) => {
        if (cancelled) {
          return;
        }
        const rows = buildBallRows(events, tags, clips);
        setData({ session, rows });
        // US-K2 URL contract: /sessions/{id}?ball=N opens on that ball;
        // otherwise US-B5 synchronized-start playback of the first ball.
        const requested = requestedBallNo(window.location.search);
        if (requested !== null && rows.some((row) => row.ballNo === requested)) {
          setSelectedBallNo(requested);
          setDeepLinkBallNo(requested);
        } else {
          setSelectedBallNo(rows.length > 0 ? rows[0].ballNo : null);
        }
      },
      (reason: unknown) => {
        if (!cancelled) {
          setError(errorMessage(reason));
        }
      },
    );
    // Per-camera fps (US-B2 claimed_fps) — parent/coach-only endpoint; a
    // refusal degrades to the assumed default, shown visibly by the player.
    api.listSessionVideos(sessionId).then(
      (videos) => {
        if (!cancelled) {
          setCameraFps(fpsByCamera(videos));
        }
      },
      () => {
        if (!cancelled) {
          setCameraFps(null);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, sessionId, attempt]);

  const rows = data === null ? [] : data.rows;
  const filtered = applyFilter(rows, filter);

  // Keyboard: ↑/↓ steps the selected ball through the filtered timeline.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (isEditableTarget(event.target)) {
        return;
      }
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") {
        return;
      }
      event.preventDefault();
      setSelectedBallNo(stepBallNo(filtered, selectedBallNo, event.key === "ArrowUp" ? -1 : 1));
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  if (error !== null) {
    return (
      <main className="mx-auto w-full max-w-7xl p-4 md:p-6">
        <PageHeader title="Session" />
        <div data-testid="detail-error">
          <ErrorState message={error} onRetry={() => setAttempt((n) => n + 1)} />
        </div>
      </main>
    );
  }
  if (data === null) {
    return (
      <main className="mx-auto w-full max-w-7xl p-4 md:p-6">
        <div data-testid="detail-loading">
          <Skeleton label="Loading session" lines={5} />
        </div>
      </main>
    );
  }

  const { session } = data;
  const selectedRow = rows.find((row) => row.ballNo === selectedBallNo) ?? null;

  return (
    <main className="mx-auto w-full max-w-7xl p-4 md:p-6">
      <PageHeader
        title={`Session ${session.session_date} · ${session.session_type}`}
        description={`${session.state} · ${session.bowler_source} · ${formatBallCount(rows.length)}${
          session.degraded ? " · degraded capture" : ""
        }`}
        actions={
          <>
            <Badge tone={stateTone(session.state)}>{session.state}</Badge>
            <Badge tone="neutral">{BOWLER_LABELS[session.bowler_source]}</Badge>
          </>
        }
      />
      <div className="mb-4">
        <DegradedBanner reasons={degradedReasons(session)} />
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          {selectedRow === null ? (
            <div data-testid="no-selection">
              <EmptyState
                title="No balls recorded for this session yet."
                description="Balls appear once the pipeline has detected deliveries."
              />
            </div>
          ) : (
            <Card aria-labelledby="selected-ball-heading">
              <CardHeader>
                <CardTitle id="selected-ball-heading">Ball {selectedRow.ballNo}</CardTitle>
                <p className="text-sm text-ink-muted" aria-hidden="true">
                  ↑/↓ ball · ←/→ frame · 1–8 camera
                </p>
              </CardHeader>
              <CardBody className="flex flex-col gap-4">
                <MultiCamPlayer
                  key={selectedRow.ballNo}
                  ballNo={selectedRow.ballNo}
                  cameras={playableClips(selectedRow)}
                  mediaBase={media}
                  fpsByCamera={cameraFps}
                />
                <BallMetricsPanel
                  sessionId={sessionId}
                  ballNo={selectedRow.ballNo}
                  client={api}
                />
              </CardBody>
            </Card>
          )}
        </div>
        <Card aria-label="Balls" className="min-w-0">
          <CardBody className="flex flex-col gap-4">
            <TimelineFilters filter={filter} blocks={blockOptions(rows)} onChange={setFilter} />
            <BallTimeline
              rows={filtered}
              selectedBallNo={selectedBallNo}
              onSelect={setSelectedBallNo}
              scrollToBallNo={deepLinkBallNo}
            />
          </CardBody>
        </Card>
      </div>
    </main>
  );
}

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
 */

import { useEffect, useMemo, useState } from "react";
import { createApiClient, defaultConfig, defaultMediaBase } from "@/lib/api";
import type { ApiClient, SessionOut } from "@/lib/api";
import { formatBallCount } from "@/lib/format";
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
  return error instanceof Error ? error.message : String(error);
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
  const [filter, setFilter] = useState<TimelineFilter>(EMPTY_FILTER);
  const [selectedBallNo, setSelectedBallNo] = useState<number | null>(null);
  const [deepLinkBallNo, setDeepLinkBallNo] = useState<number | undefined>(undefined);
  const [cameraFps, setCameraFps] = useState<Record<string, number> | null>(null);

  useEffect(() => {
    let cancelled = false;
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
  }, [api, sessionId]);

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
    return <p data-testid="detail-error">Could not load session: {error}</p>;
  }
  if (data === null) {
    return <p data-testid="detail-loading">Loading session…</p>;
  }

  const { session } = data;
  const selectedRow = rows.find((row) => row.ballNo === selectedBallNo) ?? null;

  return (
    <main>
      <h1>
        Session {session.session_date} · {session.session_type}
      </h1>
      <p>
        {session.state} · {session.bowler_source} · {formatBallCount(rows.length)}
        {session.degraded ? " · degraded capture" : ""}
      </p>
      <TimelineFilters filter={filter} blocks={blockOptions(rows)} onChange={setFilter} />
      <BallTimeline
        rows={filtered}
        selectedBallNo={selectedBallNo}
        onSelect={setSelectedBallNo}
        scrollToBallNo={deepLinkBallNo}
      />
      {selectedRow === null ? (
        <p data-testid="no-selection">No balls recorded for this session yet.</p>
      ) : (
        <section aria-label={`ball ${selectedRow.ballNo} detail`}>
          <h2>Ball {selectedRow.ballNo}</h2>
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
        </section>
      )}
    </main>
  );
}

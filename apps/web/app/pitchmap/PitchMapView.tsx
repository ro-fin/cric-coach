"use client";

/**
 * Pitch map & zone analytics view (US-K2). Loads the session heatmap, the
 * session (honesty flags), the batter (handedness drives the mirror), the tag
 * list (honest no-bounce accounting) and declared targets, then renders the
 * SVG map, overlay toggles, both end frames, the zone table and click-through
 * ball lists on the design-system primitives. Four honest states: skeleton,
 * empty (with a way back to the timeline), error with retry, degraded banner.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
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
import {
  type BowlingTarget,
  fetchPlayer,
  fetchSession,
  fetchSessionHeatmap,
  fetchTags,
  fetchTargets,
  type PlayerInfo,
  type SessionHeatmap,
  type SessionInfo,
} from "./api";
import BallList from "./BallList";
import { type EndFrame, LENGTHS, ZONE_BG_CLASS } from "./geometry";
import PitchMapSvg, { type CellKeyPair } from "./PitchMapSvg";
import { degradedReasons, isEmptySession, noBounceBalls } from "./view";
import ZoneTable from "./ZoneTable";

interface LoadedData {
  heatmap: SessionHeatmap;
  session: SessionInfo;
  player: PlayerInfo;
  tagBallNos: number[];
  targets: BowlingTarget[] | null;
}

const TITLE = "Pitch map";
const DESCRIPTION = "Where every ball landed, by line and length.";

const LINK_CLASS =
  "inline-flex min-h-11 items-center font-semibold text-accent underline underline-offset-4";

const TOGGLE_CLASS = "flex min-h-11 cursor-pointer items-center gap-3 text-base text-ink";

function TimelineLink({ sessionId }: { sessionId: string }) {
  return (
    <Link data-testid="session-link" href={`/sessions/${sessionId}`} className={LINK_CLASS}>
      Back to session timeline
    </Link>
  );
}

function Legend({ colorByControl }: { colorByControl: boolean }) {
  return (
    <div data-testid="legend" className="flex flex-col gap-2 text-sm text-ink-muted">
      <ul aria-label="length zones" className="flex flex-wrap gap-x-4 gap-y-1">
        {LENGTHS.map((length) => (
          <li key={length} className="flex items-center gap-2">
            <span aria-hidden="true" className={`size-4 rounded-sm ${ZONE_BG_CLASS[length]}`} />
            {length}
          </li>
        ))}
      </ul>
      <p data-testid="legend-line">
        {colorByControl
          ? "Stronger colour = higher control %. Grey cells have no tagged balls."
          : "Stronger colour = more balls in that cell."}
      </p>
      <p>Solid dot = bounce point. Hollow dot = low confidence. Amber ring = flagged for review.</p>
    </div>
  );
}

export default function PitchMapView({ sessionId }: { sessionId: string }) {
  const [data, setData] = useState<LoadedData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [frame, setFrame] = useState<EndFrame>("batting_end");
  const [showBouncePoints, setShowBouncePoints] = useState(true);
  const [colorByControl, setColorByControl] = useState(false);
  const [showTargets, setShowTargets] = useState(false);
  const [selectedCell, setSelectedCell] = useState<CellKeyPair | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [heatmap, session, tags, targets] = await Promise.all([
          fetchSessionHeatmap(sessionId),
          fetchSession(sessionId),
          fetchTags(sessionId),
          fetchTargets(sessionId),
        ]);
        const player = await fetchPlayer(session.player_id);
        if (!cancelled) {
          setData({
            heatmap,
            session,
            player,
            tagBallNos: tags.map((tag) => tag.ball_no),
            targets,
          });
        }
      } catch (exc) {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : String(exc));
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [sessionId, attempt]);

  const retry = useCallback(() => {
    setError(null);
    setData(null);
    setAttempt((value) => value + 1);
  }, []);

  if (error !== null) {
    return (
      <main className="flex flex-col gap-6">
        <PageHeader title={TITLE} description={DESCRIPTION} />
        <div data-testid="load-error">
          <ErrorState message={`Could not load pitch map: ${error}`} onRetry={retry} />
        </div>
      </main>
    );
  }
  if (data === null) {
    return (
      <main className="flex flex-col gap-6">
        <PageHeader title={TITLE} description={DESCRIPTION} />
        <div data-testid="loading">
          <Skeleton lines={6} label="Loading pitch map" />
        </div>
      </main>
    );
  }

  const { heatmap, session, player, tagBallNos, targets } = data;
  const missingBounce = noBounceBalls(tagBallNos, heatmap.points);
  const header = (
    <PageHeader
      title={TITLE}
      description={DESCRIPTION}
      actions={<TimelineLink sessionId={sessionId} />}
    />
  );

  if (isEmptySession(heatmap.total_balls, tagBallNos)) {
    return (
      <main className="flex flex-col gap-6">
        {header}
        <DegradedBanner reasons={degradedReasons(session)} />
        <div data-testid="pitchmap-empty">
          <EmptyState
            title="No balls on the map yet"
            description="Balls appear here once the session is tagged or the pipeline finds bounce points."
            action={<TimelineLink sessionId={sessionId} />}
          />
        </div>
      </main>
    );
  }

  return (
    <main className="flex flex-col gap-6">
      {header}
      <DegradedBanner reasons={degradedReasons(session)} />
      <div className="flex flex-wrap items-center gap-3">
        <p data-testid="player-line" className="text-lg font-semibold text-ink">
          {`${player.name} — ${player.handedness === "left" ? "left" : "right"}-hand batter`}
          {player.is_guest ? " (guest)" : ""}
        </p>
        {player.is_guest && <Badge tone="info">guest</Badge>}
      </div>
      <p data-testid="totals-line" className="text-ink-muted">
        {`${heatmap.total_balls} balls on the map · ${heatmap.flagged_balls.length} flagged for review`}
      </p>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
        <Card>
          <CardHeader>
            <CardTitle>Map</CardTitle>
          </CardHeader>
          <CardBody className="flex flex-col items-center gap-4">
            <PitchMapSvg
              cells={heatmap.cells}
              points={heatmap.points}
              flaggedBalls={heatmap.flagged_balls}
              handedness={player.handedness}
              frame={frame}
              showBouncePoints={showBouncePoints}
              colorByControl={colorByControl}
              targets={targets === null ? [] : targets}
              showTargets={showTargets}
              selectedCell={selectedCell}
              onSelectCell={setSelectedCell}
            />
            <Legend colorByControl={colorByControl} />
          </CardBody>
        </Card>

        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader>
              <CardTitle>View</CardTitle>
            </CardHeader>
            <CardBody>
              <fieldset className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
                <legend className="sr-only">Map options</legend>
                <label className={TOGGLE_CLASS}>
                  <input
                    type="radio"
                    name="frame"
                    value="batting_end"
                    className="size-5 accent-accent"
                    checked={frame === "batting_end"}
                    onChange={() => setFrame("batting_end")}
                  />
                  batting end (bowler far)
                </label>
                <label className={TOGGLE_CLASS}>
                  <input
                    type="radio"
                    name="frame"
                    value="bowling_end"
                    className="size-5 accent-accent"
                    checked={frame === "bowling_end"}
                    onChange={() => setFrame("bowling_end")}
                  />
                  bowling end (batter far)
                </label>
                <label className={TOGGLE_CLASS}>
                  <input
                    type="checkbox"
                    className="size-5 accent-accent"
                    checked={showBouncePoints}
                    onChange={() => setShowBouncePoints(!showBouncePoints)}
                  />
                  bounce points
                </label>
                <label className={TOGGLE_CLASS}>
                  <input
                    type="checkbox"
                    className="size-5 accent-accent"
                    checked={colorByControl}
                    onChange={() => setColorByControl(!colorByControl)}
                  />
                  control coloring
                </label>
                <label className={TOGGLE_CLASS}>
                  <input
                    type="checkbox"
                    className="size-5 accent-accent"
                    checked={showTargets}
                    onChange={() => setShowTargets(!showTargets)}
                  />
                  target zones
                </label>
              </fieldset>
              {showTargets && targets === null && (
                <p data-testid="targets-unavailable" className="mt-3 text-warning">
                  Declared targets are unavailable for this session.
                </p>
              )}
              {showTargets && targets !== null && targets.length === 0 && (
                <p data-testid="targets-empty" className="mt-3 text-ink-muted">
                  No targets were declared for this session.
                </p>
              )}
            </CardBody>
          </Card>

          <ZoneTable
            cells={heatmap.cells}
            selectedCell={selectedCell}
            onSelectCell={setSelectedCell}
          />
          <BallList
            sessionId={sessionId}
            points={heatmap.points}
            flaggedBalls={heatmap.flagged_balls}
            noBounceBalls={missingBounce}
            selectedCell={selectedCell}
          />
        </div>
      </div>
    </main>
  );
}

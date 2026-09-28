"use client";

/**
 * Pitch map & zone analytics view (US-K2). Loads the session heatmap, the
 * batter (handedness drives the mirror), the tag list (honest no-bounce
 * accounting) and declared targets, then renders the SVG map, overlay
 * toggles, both end frames, the zone table and click-through ball lists.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  type BouncePoint,
  type BowlingTarget,
  fetchPlayer,
  fetchSession,
  fetchSessionHeatmap,
  fetchTags,
  fetchTargets,
  type PlayerInfo,
  type SessionHeatmap,
} from "./api";
import type { EndFrame } from "./geometry";
import PitchMapSvg, { type CellKeyPair } from "./PitchMapSvg";
import ZoneTable from "./ZoneTable";
import BallList from "./BallList";

interface LoadedData {
  heatmap: SessionHeatmap;
  player: PlayerInfo;
  tagBallNos: number[];
  targets: BowlingTarget[] | null;
}

function noBounceBalls(tagBallNos: number[], points: BouncePoint[]): number[] {
  const withBounce = new Set(points.map((point) => point.ball_no));
  return tagBallNos.filter((ballNo) => !withBounce.has(ballNo));
}

export default function PitchMapView({ sessionId }: { sessionId: string }) {
  const [data, setData] = useState<LoadedData | null>(null);
  const [error, setError] = useState<string | null>(null);
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
          setData({ heatmap, player, tagBallNos: tags.map((tag) => tag.ball_no), targets });
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
  }, [sessionId]);

  if (error !== null) {
    return (
      <main>
        <h1>Pitch map</h1>
        <p role="alert" data-testid="load-error">{`Could not load pitch map: ${error}`}</p>
      </main>
    );
  }
  if (data === null) {
    return (
      <main>
        <h1>Pitch map</h1>
        <p data-testid="loading">Loading…</p>
      </main>
    );
  }

  const { heatmap, player, tagBallNos, targets } = data;
  const missingBounce = noBounceBalls(tagBallNos, heatmap.points);

  return (
    <main>
      <h1>Pitch map</h1>
      <p data-testid="player-line">
        {`${player.name} — ${player.handedness === "left" ? "left" : "right"}-hand batter`}
        {player.is_guest ? " (guest)" : ""}
      </p>
      <p data-testid="totals-line">
        {`${heatmap.total_balls} balls on the map · ${heatmap.flagged_balls.length} flagged for review`}
      </p>
      <p>
        <Link data-testid="session-link" href={`/sessions/${sessionId}`}>
          Back to session timeline
        </Link>
      </p>

      <fieldset>
        <legend>View</legend>
        <label>
          <input
            type="radio"
            name="frame"
            value="batting_end"
            checked={frame === "batting_end"}
            onChange={() => setFrame("batting_end")}
          />
          batting end (bowler far)
        </label>
        <label>
          <input
            type="radio"
            name="frame"
            value="bowling_end"
            checked={frame === "bowling_end"}
            onChange={() => setFrame("bowling_end")}
          />
          bowling end (batter far)
        </label>
        <label>
          <input
            type="checkbox"
            checked={showBouncePoints}
            onChange={() => setShowBouncePoints(!showBouncePoints)}
          />
          bounce points
        </label>
        <label>
          <input
            type="checkbox"
            checked={colorByControl}
            onChange={() => setColorByControl(!colorByControl)}
          />
          control coloring
        </label>
        <label>
          <input
            type="checkbox"
            checked={showTargets}
            onChange={() => setShowTargets(!showTargets)}
          />
          target zones
        </label>
      </fieldset>

      {showTargets && targets === null && (
        <p data-testid="targets-unavailable">
          Declared targets are unavailable for this session.
        </p>
      )}
      {showTargets && targets !== null && targets.length === 0 && (
        <p data-testid="targets-empty">No targets were declared for this session.</p>
      )}

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
      <p data-testid="legend-line">
        {colorByControl
          ? "Cells shaded by control % (grey = no tagged balls)."
          : "Cells shaded by ball density."}
      </p>

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
    </main>
  );
}

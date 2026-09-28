"use client";

/**
 * Wellness (US-H4 dashboard half): today's state from the pain state machine,
 * the check-in form, and recent check-ins. Every role can check in; only a
 * parent or coach can clear an open pain flag, with a required note (the
 * server enforces both and its answers are shown verbatim). Nothing here is
 * computed: the state tiles are the API's own fields.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Dialog,
  EmptyState,
  ErrorState,
  ForbiddenState,
  PageHeader,
  Skeleton,
  StatTile,
  useToast,
} from "@/components/ui";
import { type ApiConfig, defaultConfig } from "@/lib/api";
import { useRole } from "@/lib/auth/role";
import { failure, type Load, LOADING, ready } from "../pipeline/access";
import {
  type CheckinOut,
  createWellnessApi,
  type PlayerOut,
  type WellnessApi,
  type WellnessStateOut,
} from "./api";
import { CheckinForm } from "./CheckinForm";
import { WELLNESS_COPY as COPY } from "./copy";
import {
  bodyLabel,
  canClear,
  newestFirst,
  pickPlayer,
  playerIdFromSearch,
  sorenessEntries,
  WELLNESS_ROLES,
} from "./view";

interface WellnessData {
  state: WellnessStateOut;
  checkins: CheckinOut[];
}

function StatePanel({ state }: { state: WellnessStateOut }) {
  return (
    <Card aria-labelledby="wellness-state-title">
      <CardHeader>
        <CardTitle id="wellness-state-title">{COPY.stateTitle}</CardTitle>
        <Badge tone={state.checked_in ? "success" : "neutral"}>
          {state.checked_in ? COPY.checkedIn : COPY.notCheckedIn}
        </Badge>
      </CardHeader>
      <CardBody className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <div data-testid="days-since">
            <StatTile
              label={COPY.daysSince}
              value={state.days_since_checkin === null ? null : String(state.days_since_checkin)}
              reason={state.days_since_checkin === null ? COPY.neverCheckedIn : null}
            />
          </div>
          <div data-testid="pain-reports">
            <StatTile label={COPY.painReports} value={String(state.pain_reports_in_window)} />
          </div>
        </div>
        <ul className="flex flex-col gap-2" data-testid="wellness-flags">
          <li>
            <Badge tone={state.pain_active ? "danger" : "success"}>
              {state.pain_active ? COPY.painActive : COPY.noPain}
            </Badge>
          </li>
          {state.bowling_suppressed && (
            <li>
              <Badge tone="warning">{COPY.bowlingPaused}</Badge>
            </li>
          )}
          {state.escalation && (
            <li>
              <Badge tone="danger">{COPY.escalation}</Badge>
            </li>
          )}
        </ul>
      </CardBody>
    </Card>
  );
}

function History({
  checkins,
  openIds,
  canClearFlags,
  onClear,
}: {
  checkins: CheckinOut[];
  openIds: readonly string[];
  canClearFlags: boolean;
  onClear: (checkin: CheckinOut) => void;
}) {
  const open = new Set(openIds);
  return (
    <Card aria-labelledby="wellness-history-title">
      <CardHeader>
        <CardTitle id="wellness-history-title">{COPY.historyTitle}</CardTitle>
      </CardHeader>
      <CardBody>
        {checkins.length === 0 ? (
          <EmptyState title={COPY.historyEmptyTitle} description={COPY.historyEmptyDescription} />
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {newestFirst(checkins).map((row) => (
              <li key={row.id} data-testid={`checkin-${row.id}`} className="flex flex-col gap-2 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold text-ink">{row.checkin_date}</span>
                  {row.pain && <Badge tone="danger">{COPY.painBadge}</Badge>}
                  {open.has(row.id) && <Badge tone="warning">{COPY.openBadge}</Badge>}
                  <span className="text-sm text-ink-muted">
                    {`${COPY.historyBy} ${row.created_by}`}
                  </span>
                </div>
                <p className="text-ink">
                  {`${COPY.historyEnergy} ${row.energy ?? COPY.notRecorded} · ${COPY.historySleep} ${
                    row.sleep_hours === null ? COPY.notRecorded : `${row.sleep_hours}${COPY.historySleepUnit}`
                  }`}
                </p>
                {sorenessEntries(row.soreness).length > 0 && (
                  <p className="text-ink-muted">
                    {sorenessEntries(row.soreness)
                      .map(([key, level]) => `${bodyLabel(key)} ${COPY.sorenessLevels[level] ?? level}`)
                      .join(", ")}
                  </p>
                )}
                {row.pain_note !== null && <p className="text-ink">{row.pain_note}</p>}
                {canClearFlags && open.has(row.id) && (
                  <div>
                    <Button variant="secondary" onClick={() => onClear(row)}>
                      {COPY.clear}
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

function ClearDialog({
  api,
  playerId,
  checkin,
  onClose,
  onCleared,
}: {
  api: WellnessApi;
  playerId: string;
  checkin: CheckinOut;
  onClose: () => void;
  onCleared: () => void;
}) {
  const { toast } = useToast();
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const confirm = () => {
    if (note.trim() === "") {
      setProblem(COPY.clearNoteRequired);
      return;
    }
    setSaving(true);
    api
      .clearPain(playerId, checkin.id, note.trim())
      .then(() => {
        toast({ title: COPY.clearDone, tone: "success" });
        onCleared();
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setProblem(message);
        toast({ title: COPY.clearFailed, description: message, tone: "danger" });
      })
      .finally(() => setSaving(false));
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
      title={COPY.clearTitle}
    >
      <div className="flex flex-col gap-4">
        <p className="text-ink">
          {checkin.checkin_date}
          {checkin.pain_note !== null ? ` — ${checkin.pain_note}` : ""}
        </p>
        {problem !== null && (
          <p role="alert" className="font-semibold text-danger">
            {problem}
          </p>
        )}
        <label className="flex flex-col gap-2 font-semibold text-ink">
          {COPY.clearNote}
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            rows={3}
            className="rounded-lg border-2 border-border bg-surface px-3 py-2 text-base font-normal text-ink"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" loading={saving} onClick={confirm}>
            {COPY.clearConfirm}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {COPY.clearCancel}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

export interface WellnessViewProps {
  /** Injected for tests; defaults to the shared lib/api configuration. */
  config?: ApiConfig;
}

export default function WellnessView({ config }: WellnessViewProps) {
  const role = useRole();
  const api = useMemo(() => createWellnessApi(config ?? defaultConfig()), [config]);
  const [players, setPlayers] = useState<Load<PlayerOut[]>>(LOADING);
  const [playerId, setPlayerId] = useState<string | null>(null);
  const [data, setData] = useState<Load<WellnessData>>(LOADING);
  const [playersAttempt, setPlayersAttempt] = useState(0);
  const [dataAttempt, setDataAttempt] = useState(0);
  const [clearing, setClearing] = useState<CheckinOut | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPlayers(LOADING);
    api.listPlayers().then(
      (rows) => {
        if (!cancelled) {
          setPlayers(ready(rows));
          setPlayerId(pickPlayer(rows, playerIdFromSearch(window.location.search)));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setPlayers(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, playersAttempt]);

  useEffect(() => {
    if (playerId === null) {
      return;
    }
    let cancelled = false;
    setData(LOADING);
    Promise.all([api.state(playerId), api.listCheckins(playerId)]).then(
      ([state, checkins]) => {
        if (!cancelled) {
          setData(ready({ state, checkins }));
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setData(failure(error));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [api, playerId, dataAttempt]);

  const reload = useCallback(() => setDataAttempt((value) => value + 1), []);

  const choosePlayer = (id: string) => {
    window.history.replaceState({}, "", `/wellness?player=${encodeURIComponent(id)}`);
    setPlayerId(id);
  };

  return (
    <main className="flex flex-col gap-6">
      <PageHeader title={COPY.title} description={COPY.description} />
      {players.kind === "loading" && <Skeleton lines={4} label={COPY.loading} />}
      {players.kind === "error" && (
        <ErrorState
          message={`${COPY.loadFailed}: ${players.message}`}
          onRetry={() => setPlayersAttempt((value) => value + 1)}
        />
      )}
      {players.kind === "forbidden" && <ForbiddenState roles={WELLNESS_ROLES} detail={players.message} />}
      {players.kind === "ready" && players.data.length === 0 && (
        <EmptyState title={COPY.noPlayersTitle} description={COPY.noPlayersDescription} />
      )}
      {players.kind === "ready" && playerId !== null && (
        <>
          {players.data.length > 1 && (
            <label className="flex max-w-md flex-col gap-2 font-semibold text-ink">
              {COPY.pickPlayer}
              <select
                value={playerId}
                onChange={(event) => choosePlayer(event.target.value)}
                className="min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-base font-normal text-ink"
              >
                {players.data.map((player) => (
                  <option key={player.id} value={player.id}>
                    {player.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {data.kind === "loading" && <Skeleton lines={5} label={COPY.loading} />}
          {data.kind === "error" && (
            <ErrorState message={`${COPY.loadFailed}: ${data.message}`} onRetry={reload} />
          )}
          {data.kind === "forbidden" && <ForbiddenState roles={WELLNESS_ROLES} detail={data.message} />}
          {data.kind === "ready" && (
            <div className="grid gap-6 lg:grid-cols-2">
              <div className="flex flex-col gap-6">
                <StatePanel state={data.data.state} />
                <History
                  checkins={data.data.checkins}
                  openIds={data.data.state.open_pain_checkin_ids}
                  canClearFlags={canClear(role)}
                  onClear={setClearing}
                />
              </div>
              <CheckinForm key={playerId} api={api} playerId={playerId} onSaved={reload} />
            </div>
          )}
          {clearing !== null && (
            <ClearDialog
              api={api}
              playerId={playerId}
              checkin={clearing}
              onClose={() => setClearing(null)}
              onCleared={() => {
                setClearing(null);
                reload();
              }}
            />
          )}
        </>
      )}
    </main>
  );
}

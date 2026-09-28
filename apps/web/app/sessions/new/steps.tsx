"use client";

/**
 * The five steps of the new-session flow. Each step owns its own API reads
 * (with honest loading / empty / error states) and hands its server result up
 * to the flow; every server verdict (warnings, degraded, missing views,
 * refusals) is shown verbatim.
 */

import { useCallback, useState, type FormEvent, type ReactNode } from "react";
import {
  Badge,
  Button,
  DegradedBanner,
  EmptyState,
  ErrorState,
  LinkButton,
  Skeleton,
} from "@/components/ui";
import type { SessionOut } from "@/lib/api";
import type { PlayerOut } from "@/app/_today/api";
import { describeError, useLoad } from "@/app/_today/useLoad";
import { BOWLER_LABELS, TYPE_OPTIONS } from "../list";
import { LENGTH_OPTIONS } from "../[id]/timeline";
import type { NewSessionApi, StartOut, StopOut } from "./api";
import {
  MACHINE_SPEED_MAX_KPH,
  MACHINE_SPEED_MIN_KPH,
  toSessionIn,
  validateDetails,
} from "./flow";
import type { DetailsErrors, DetailsForm } from "./flow";

const FIELD =
  "min-h-11 w-full rounded-lg border-2 border-border bg-surface px-3 text-ink focus:border-accent aria-[invalid=true]:border-danger";
const LABEL = "flex flex-col gap-1 font-semibold";

/** An action's failure as one sentence; 403 names who may do it instead. */
export function actionError(error: unknown, forbidden: string): string {
  const { message, httpStatus } = describeError(error);
  return httpStatus === 403 ? forbidden : message;
}

function FieldError({ id, text }: { id: string; text: string | undefined }) {
  return text === undefined ? null : (
    <span id={id} className="text-sm font-semibold text-danger">
      {text}
    </span>
  );
}

function ActionFailure({ text }: { text: string | null }) {
  return text === null ? null : <ErrorState message={text} />;
}

// ---------------------------------------------------------------------------
// 1. Details
// ---------------------------------------------------------------------------

export function DetailsStep({
  api,
  form,
  onChange,
  onCreated,
}: {
  api: NewSessionApi;
  form: DetailsForm;
  onChange: (form: DetailsForm) => void;
  onCreated: (session: SessionOut) => void;
}) {
  const loadPlayers = useCallback(() => api.listPlayers(), [api]);
  const players = useLoad(loadPlayers);
  const [errors, setErrors] = useState<DetailsErrors>({});
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const set = (patch: Partial<DetailsForm>) => onChange({ ...form, ...patch });
  const machine = form.bowlerSource === "machine";

  async function submit(event: FormEvent, playerId: string) {
    event.preventDefault();
    const complete = { ...form, playerId };
    const found = validateDetails(complete);
    setErrors(found);
    if (Object.keys(found).length > 0) return;
    setBusy(true);
    setFailure(null);
    try {
      onCreated(await api.createSession(toSessionIn(complete)));
    } catch (error) {
      setFailure(actionError(error, "Only a parent or coach can create a session."));
      setBusy(false);
    }
  }

  if (players.state.status === "loading") return <Skeleton label="Loading players" />;
  if (players.state.status === "error") {
    return (
      <ErrorState
        message={`Could not load players: ${players.state.message}`}
        onRetry={players.retry}
      />
    );
  }
  const list: PlayerOut[] = players.state.data;
  if (list.length === 0) {
    return (
      <EmptyState
        title="No players yet"
        description="Add the player first, then start their session."
        action={
          <LinkButton variant="primary" href="/settings">
            Add a player in settings
          </LinkButton>
        }
      />
    );
  }
  // an unknown or unset player id falls back to the first listed player
  const selected = list.some((player) => player.id === form.playerId)
    ? form.playerId
    : list[0].id;

  return (
    <form
      onSubmit={(event) => submit(event, selected)}
      noValidate
      className="flex flex-col gap-5"
    >
      <div className="grid gap-4 md:grid-cols-2">
        <label className={LABEL}>
          Player
          <select
            className={FIELD}
            value={selected}
            onChange={(e) => set({ playerId: e.target.value })}
          >
            {list.map((player) => (
              <option key={player.id} value={player.id}>
                {player.name}
                {player.is_guest ? " (guest)" : ""}
              </option>
            ))}
          </select>
        </label>
        <div className="flex flex-col gap-1">
          <label className={LABEL}>
            Date
            <input
              type="date"
              className={FIELD}
              value={form.date}
              aria-invalid={errors.date !== undefined}
              aria-describedby="date-error"
              onChange={(e) => set({ date: e.target.value })}
            />
          </label>
          <FieldError id="date-error" text={errors.date} />
        </div>
        <label className={LABEL}>
          Session type
          <select
            className={FIELD}
            value={form.sessionType}
            onChange={(e) => set({ sessionType: e.target.value as DetailsForm["sessionType"] })}
          >
            {TYPE_OPTIONS.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          Bowler
          <select
            className={FIELD}
            value={form.bowlerSource}
            onChange={(e) => set({ bowlerSource: e.target.value as DetailsForm["bowlerSource"] })}
          >
            {(Object.keys(BOWLER_LABELS) as DetailsForm["bowlerSource"][]).map((source) => (
              <option key={source} value={source}>
                {BOWLER_LABELS[source]}
              </option>
            ))}
          </select>
        </label>
      </div>
      {machine && (
        <fieldset className="grid gap-4 rounded-xl border border-border p-4 md:grid-cols-3">
          <legend className="px-1 font-semibold">Machine settings</legend>
          <div className="flex flex-col gap-1">
            <label className={LABEL}>
              Speed (km/h)
              <input
                type="number"
                inputMode="decimal"
                min={MACHINE_SPEED_MIN_KPH}
                max={MACHINE_SPEED_MAX_KPH}
                className={FIELD}
                value={form.speedKph}
                aria-invalid={errors.speedKph !== undefined}
                aria-describedby="speed-error"
                onChange={(e) => set({ speedKph: e.target.value })}
              />
            </label>
            <FieldError id="speed-error" text={errors.speedKph} />
          </div>
          <label className={LABEL}>
            Length
            <select
              className={FIELD}
              value={form.length}
              onChange={(e) => set({ length: e.target.value as DetailsForm["length"] })}
            >
              {LENGTH_OPTIONS.map((length) => (
                <option key={length} value={length}>
                  {length}
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-col gap-1">
            <label className={LABEL}>
              Variation (optional)
              <input
                type="text"
                className={FIELD}
                value={form.variation}
                aria-invalid={errors.variation !== undefined}
                aria-describedby="variation-error"
                onChange={(e) => set({ variation: e.target.value })}
              />
            </label>
            <FieldError id="variation-error" text={errors.variation} />
          </div>
        </fieldset>
      )}
      <div className="flex flex-col gap-1">
        <label className={LABEL}>
          Notes (optional)
          <textarea
            rows={3}
            className={`${FIELD} py-2`}
            value={form.notes}
            aria-invalid={errors.notes !== undefined}
            aria-describedby="notes-error"
            onChange={(e) => set({ notes: e.target.value })}
          />
        </label>
        <FieldError id="notes-error" text={errors.notes} />
      </div>
      <ActionFailure text={failure} />
      <div>
        <Button type="submit" variant="primary" size="lg" loading={busy}>
          Create session
        </Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// 2. Safety checklist (machine sessions only)
// ---------------------------------------------------------------------------

export function ChecklistStep({
  api,
  sessionId,
  onAcked,
}: {
  api: NewSessionApi;
  sessionId: string;
  onAcked: () => void;
}) {
  const loadChecklist = useCallback(() => api.machineChecklist(), [api]);
  const checklist = useLoad(loadChecklist);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [ackedBy, setAckedBy] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  if (checklist.state.status === "loading") return <Skeleton label="Loading the safety checklist" />;
  if (checklist.state.status === "error") {
    return (
      <ErrorState
        message={`Could not load the safety checklist: ${checklist.state.message}`}
        onRetry={checklist.retry}
      />
    );
  }
  const items = checklist.state.data.items;
  const allChecked = items.every((item) => checked[item.id] === true);
  const ready = allChecked && ackedBy.trim() !== "";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFailure(null);
    try {
      await api.ackChecklist(sessionId, {
        items: Object.fromEntries(items.map((item) => [item.id, checked[item.id] === true])),
        acked_by: ackedBy.trim(),
      });
      onAcked();
    } catch (error) {
      setFailure(actionError(error, "Only a parent can acknowledge the safety checklist."));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-5">
      <p className="text-ink-muted">
        A machine session cannot start until every item is checked on site.
      </p>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 font-semibold">Safety checklist</legend>
        {items.map((item) => (
          <label
            key={item.id}
            className="flex min-h-11 items-center gap-3 rounded-lg border border-border px-3 py-2"
          >
            <input
              type="checkbox"
              className="size-6 accent-accent"
              checked={checked[item.id] === true}
              onChange={(e) => setChecked({ ...checked, [item.id]: e.target.checked })}
            />
            {item.label}
          </label>
        ))}
      </fieldset>
      <label className={LABEL}>
        Checked by
        <input
          type="text"
          maxLength={64}
          className={FIELD}
          value={ackedBy}
          onChange={(e) => setAckedBy(e.target.value)}
        />
      </label>
      <ActionFailure text={failure} />
      <div>
        <Button type="submit" variant="primary" size="lg" disabled={!ready} loading={busy}>
          Confirm safety check
        </Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// 3. Cameras + start
// ---------------------------------------------------------------------------

export function CamerasStep({
  api,
  sessionId,
  onStarted,
  onBackToChecklist,
}: {
  api: NewSessionApi;
  sessionId: string;
  onStarted: (start: StartOut) => void;
  onBackToChecklist: (() => void) | null;
}) {
  const loadCameras = useCallback(() => api.listCameras(), [api]);
  const cameras = useLoad(loadCameras);
  const [excluded, setExcluded] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ text: string; status: number | null } | null>(null);

  if (cameras.state.status === "loading") return <Skeleton label="Loading cameras" />;
  if (cameras.state.status === "error") {
    return (
      <ErrorState
        message={`Could not load cameras: ${cameras.state.message}`}
        onRetry={cameras.retry}
      />
    );
  }
  const active = cameras.state.data.filter((camera) => camera.active);
  if (active.length === 0) {
    return (
      <EmptyState
        title="No active cameras"
        description="Register the lab cameras before recording."
        action={
          <LinkButton variant="primary" href="/cameras">
            Open cameras
          </LinkButton>
        }
      />
    );
  }
  const chosen = active.filter((camera) => excluded[camera.camera_id] !== true);

  async function start() {
    setBusy(true);
    setFailure(null);
    try {
      onStarted(await api.start(sessionId, chosen.map((camera) => camera.camera_id)));
    } catch (error) {
      const { httpStatus } = describeError(error);
      setFailure({
        text: actionError(error, "Only a parent or coach can start recording."),
        status: httpStatus,
      });
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <fieldset className="grid gap-2 md:grid-cols-2">
        <legend className="mb-2 font-semibold">Cameras to record</legend>
        {active.map((camera) => (
          <label
            key={camera.camera_id}
            className="flex min-h-11 items-center gap-3 rounded-lg border border-border px-3 py-2"
          >
            <input
              type="checkbox"
              className="size-6 accent-accent"
              checked={excluded[camera.camera_id] !== true}
              onChange={(e) => setExcluded({ ...excluded, [camera.camera_id]: !e.target.checked })}
            />
            <span className="font-semibold">{camera.camera_id}</span>
            <span className="text-ink-muted">
              {camera.position_label} · {camera.fps} fps · {camera.resolution}
            </span>
          </label>
        ))}
      </fieldset>
      {failure !== null && (
        <div className="flex flex-col gap-2">
          <ErrorState message={failure.text} />
          {failure.status === 409 && onBackToChecklist !== null && (
            <div>
              <Button variant="secondary" onClick={onBackToChecklist}>
                Back to the safety check
              </Button>
            </div>
          )}
        </div>
      )}
      <div>
        <Button
          variant="primary"
          size="lg"
          disabled={chosen.length === 0}
          loading={busy}
          onClick={start}
        >
          Start recording
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 4. Recording + stop
// ---------------------------------------------------------------------------

export function RecordingStep({
  api,
  sessionId,
  start,
  onStopped,
}: {
  api: NewSessionApi;
  sessionId: string;
  /** The start result; null when resuming a session that was already recording. */
  start: StartOut | null;
  onStopped: (stop: StopOut) => void;
}) {
  const [failed, setFailed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const roster = start === null ? [] : start.cameras;

  async function stop() {
    setBusy(true);
    setFailure(null);
    try {
      const reporting = Object.fromEntries(
        Object.entries(failed)
          .filter(([, isFailed]) => isFailed)
          .map(([cameraId]) => [cameraId, { ok: false }]),
      );
      onStopped(await api.stop(sessionId, { cameras_reporting: reporting }));
    } catch (error) {
      setFailure(actionError(error, "Only a parent or coach can stop recording."));
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <p className="flex items-center gap-3 text-lg font-semibold" role="status" aria-live="polite">
        <span aria-hidden="true" className="size-4 animate-pulse rounded-full bg-danger" />
        Recording
        {start?.started_at ? (
          <span className="font-normal text-ink-muted">since {start.started_at}</span>
        ) : null}
      </p>
      {start !== null && start.warnings.length > 0 && <DegradedBanner reasons={start.warnings} />}
      {roster.length > 0 && (
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-2 font-semibold">Did any camera fail?</legend>
          {roster.map((cameraId) => (
            <label
              key={cameraId}
              className="flex min-h-11 items-center gap-3 rounded-lg border border-border px-3 py-2"
            >
              <input
                type="checkbox"
                className="size-6 accent-accent"
                checked={failed[cameraId] === true}
                onChange={(e) => setFailed({ ...failed, [cameraId]: e.target.checked })}
              />
              {cameraId} failed
            </label>
          ))}
        </fieldset>
      )}
      <ActionFailure text={failure} />
      <div>
        <Button variant="danger" size="lg" loading={busy} onClick={stop}>
          Stop recording
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 5. Done
// ---------------------------------------------------------------------------

export function DoneStep({
  sessionId,
  state,
  degraded,
  missingViews,
}: {
  sessionId: string;
  state: string;
  degraded: boolean;
  missingViews: string[];
}) {
  const reasons: ReactNode = degraded ? (
    <DegradedBanner
      reasons={
        missingViews.length === 0
          ? ["Capture is degraded"]
          : missingViews.map((view) => `Missing view: ${view}`)
      }
    />
  ) : null;
  return (
    <div className="flex flex-col gap-5">
      <p className="flex items-center gap-3 text-lg font-semibold">
        Session saved <Badge tone={degraded ? "warning" : "success"}>{state}</Badge>
      </p>
      {reasons}
      <p className="text-ink-muted">
        The pipeline analyses the footage next; the report appears on Today once it is
        published.
      </p>
      <div className="flex flex-wrap gap-3">
        <LinkButton variant="primary" href={`/sessions/${sessionId}`}>
          Open the session
        </LinkButton>
        <LinkButton variant="secondary" href="/sessions/new">
          Start another
        </LinkButton>
      </div>
    </div>
  );
}

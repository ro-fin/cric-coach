/**
 * New-session flow model: which step the operator is on, and the details
 * form. Validation mirrors the server's own constraints (routers/sessions.py)
 * so mistakes surface before the round trip; the server stays the authority.
 */

import type { BowlerSource, LengthZone, SessionOut, SessionState, SessionType } from "@/lib/api";
import type { SessionIn } from "./api";

export type Step = "details" | "checklist" | "cameras" | "recording" | "done";

export const STEP_LABELS: Record<Step, string> = {
  details: "Details",
  checklist: "Safety check",
  cameras: "Cameras",
  recording: "Recording",
  done: "Done",
};

export const STEPS: readonly { id: Step; label: string }[] = (
  ["details", "checklist", "cameras", "recording", "done"] as const
).map((id) => ({ id, label: STEP_LABELS[id] }));

/** Steps shown for this bowler source: only machine sessions pass the safety gate. */
export function stepsFor(bowler: BowlerSource): readonly { id: Step; label: string }[] {
  return bowler === "machine" ? STEPS : STEPS.filter((step) => step.id !== "checklist");
}

/** Where a session already on the server resumes (e.g. after a tablet reload). */
export function resumeStep(session: SessionOut, state: SessionState): Step {
  if (state === "created") {
    return session.bowler_source === "machine" ? "checklist" : "cameras";
  }
  return state === "recording" ? "recording" : "done";
}

/** The step after creating a session. */
export function afterCreate(bowler: BowlerSource): Step {
  return bowler === "machine" ? "checklist" : "cameras";
}

export const MACHINE_SPEED_MIN_KPH = 30;
export const MACHINE_SPEED_MAX_KPH = 160;
export const NOTES_MAX = 4000;
export const VARIATION_MAX = 64;

export interface DetailsForm {
  playerId: string;
  date: string;
  sessionType: SessionType;
  bowlerSource: BowlerSource;
  speedKph: string;
  length: LengthZone;
  variation: string;
  notes: string;
}

export function emptyDetails(playerId: string, today: string): DetailsForm {
  return {
    playerId,
    date: today,
    sessionType: "batting",
    bowlerSource: "machine",
    speedKph: "",
    length: "good",
    variation: "",
    notes: "",
  };
}

export type DetailsErrors = Partial<Record<"playerId" | "date" | "speedKph" | "variation" | "notes", string>>;

export function validateDetails(form: DetailsForm): DetailsErrors {
  const errors: DetailsErrors = {};
  if (form.playerId === "") errors.playerId = "Choose a player.";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.date)) errors.date = "Choose a date.";
  if (form.bowlerSource === "machine") {
    const speed = Number(form.speedKph);
    if (
      form.speedKph.trim() === "" ||
      !Number.isFinite(speed) ||
      speed < MACHINE_SPEED_MIN_KPH ||
      speed > MACHINE_SPEED_MAX_KPH
    ) {
      errors.speedKph = `Enter a machine speed from ${MACHINE_SPEED_MIN_KPH} to ${MACHINE_SPEED_MAX_KPH} km/h.`;
    }
    if (form.variation.length > VARIATION_MAX) {
      errors.variation = `Keep the variation under ${VARIATION_MAX} characters.`;
    }
  }
  if (form.notes.length > NOTES_MAX) errors.notes = `Keep notes under ${NOTES_MAX} characters.`;
  return errors;
}

export function toSessionIn(form: DetailsForm): SessionIn {
  const machine = form.bowlerSource === "machine";
  return {
    player_id: form.playerId,
    date: form.date,
    session_type: form.sessionType,
    bowler_source: form.bowlerSource,
    machine_settings: machine
      ? {
          speed_kph: Number(form.speedKph),
          length: form.length,
          variation: form.variation.trim() === "" ? null : form.variation.trim(),
        }
      : null,
    notes: form.notes.trim() === "" ? null : form.notes.trim(),
  };
}

/** Local calendar date as YYYY-MM-DD (the lab records sessions in local time). */
export function localIsoDate(now: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

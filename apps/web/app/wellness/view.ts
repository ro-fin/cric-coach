/**
 * Wellness screen view model (US-H4). The check-in form keeps raw strings;
 * `checkinProblems` explains out-of-range values with the server's own
 * wording before posting, and `toCheckinIn` builds the wire body. The server
 * stays the authority: its 422 problem list is shown verbatim.
 */

import type { Role } from "../pipeline/access";
import {
  type CheckinIn,
  SORENESS_BODY_KEYS,
  type CheckinOut,
  ENERGY_MAX,
  ENERGY_MIN,
  SLEEP_HOURS_MAX,
  SLEEP_HOURS_MIN,
  SORENESS_LEVEL_MAX,
  SORENESS_LEVEL_MIN,
} from "./api";

export const WELLNESS_ROLES: readonly Role[] = ["parent", "coach", "player"];

/** Adult clearance is parent or coach only (US-H4). */
export function canClear(role: Role | null): boolean {
  return role === "parent" || role === "coach";
}

export function playerIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("player");
  return value === null || value === "" ? null : value;
}

export interface CheckinDraft {
  checkinDate: string;
  energy: string;
  sleepHours: string;
  soreness: Record<string, number>;
  pain: boolean;
  painNote: string;
}

export function emptyDraft(today: string): CheckinDraft {
  return { checkinDate: today, energy: "", sleepHours: "", soreness: {}, pain: false, painNote: "" };
}

function parsed(raw: string): number | null {
  return raw.trim() === "" ? null : Number(raw);
}

/** Problems in the server's wording (cricai_coaching.wellness.validate_checkin). */
export function checkinProblems(draft: CheckinDraft): string[] {
  const problems: string[] = [];
  if (draft.checkinDate === "") {
    problems.push("checkin_date is required");
  }
  for (const [key, level] of Object.entries(draft.soreness)) {
    if (!Number.isInteger(level) || level < SORENESS_LEVEL_MIN || level > SORENESS_LEVEL_MAX) {
      problems.push(
        `soreness['${key}'] must be ${SORENESS_LEVEL_MIN}..${SORENESS_LEVEL_MAX}, got ${level}`,
      );
    }
  }
  const energy = parsed(draft.energy);
  if (energy !== null && !(Number.isInteger(energy) && energy >= ENERGY_MIN && energy <= ENERGY_MAX)) {
    problems.push(`energy must be ${ENERGY_MIN}..${ENERGY_MAX}, got ${draft.energy}`);
  }
  const sleep = parsed(draft.sleepHours);
  if (sleep !== null && !(sleep >= SLEEP_HOURS_MIN && sleep <= SLEEP_HOURS_MAX)) {
    problems.push(
      `sleep_hours must be ${SLEEP_HOURS_MIN}.0..${SLEEP_HOURS_MAX}.0, got ${draft.sleepHours}`,
    );
  }
  return problems;
}

/** Wire body: blank numbers become null, zero soreness levels are dropped
 * (0 means "not sore", the server default), the note only rides with pain. */
export function toCheckinIn(draft: CheckinDraft): CheckinIn {
  const soreness: Record<string, number> = {};
  for (const [key, level] of Object.entries(draft.soreness)) {
    if (level !== 0) {
      soreness[key] = level;
    }
  }
  const note = draft.painNote.trim();
  return {
    checkin_date: draft.checkinDate,
    soreness,
    energy: parsed(draft.energy),
    sleep_hours: parsed(draft.sleepHours),
    pain: draft.pain,
    pain_note: draft.pain && note !== "" ? note : null,
  };
}

/** The check-ins behind the state's open pain flags, in served order. */
export function openPainCheckins(
  checkins: readonly CheckinOut[],
  openIds: readonly string[],
): CheckinOut[] {
  const open = new Set(openIds);
  return checkins.filter((row) => open.has(row.id));
}

/** "shoulder_right" -> "shoulder right". */
export function bodyLabel(key: string): string {
  return key.replace(/_/g, " ");
}

/** Today's date in the viewer's own calendar, as the API's YYYY-MM-DD. */
export function todayIso(now: Date = new Date()): string {
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/** The API lists check-ins oldest first; the history reads newest first. */
export function newestFirst(checkins: readonly CheckinOut[]): CheckinOut[] {
  return [...checkins].reverse();
}

/** Soreness entries in head-to-foot order; unknown keys keep served order at the end. */
export function sorenessEntries(soreness: Record<string, number>): [string, number][] {
  const rank = (key: string) => {
    const index = SORENESS_BODY_KEYS.indexOf(key);
    return index === -1 ? SORENESS_BODY_KEYS.length : index;
  };
  return Object.entries(soreness).sort(([a], [b]) => rank(a) - rank(b));
}

/** Default player: the one named in the URL if known, else the first served. */
export function pickPlayer(players: readonly { id: string }[], wanted: string | null): string | null {
  if (wanted !== null && players.some((player) => player.id === wanted)) {
    return wanted;
  }
  return players[0]?.id ?? null;
}

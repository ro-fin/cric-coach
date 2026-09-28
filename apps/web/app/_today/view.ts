/**
 * Today view model: selection and presentation only. It picks WHICH API
 * record to show and turns wire vocabularies into labels; it never derives a
 * number (US-K4 data parity).
 */

import type { EvidenceMap } from "@/app/reports/api";
import type { Role } from "@/lib/auth/roles";
import type { PlayerOut, ReportOut, SafetyCode, WindowSummaryOut } from "./api";

/** The `?player=` id when it names a listed player, else the first listed. */
export function pickPlayer(players: PlayerOut[], requestedId: string | null): PlayerOut | null {
  const requested = players.find((candidate) => candidate.id === requestedId);
  return requested ?? players[0] ?? null;
}

export function playerIdFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("player");
  return value === null || value === "" ? null : value;
}

/** The newest PUBLISHED daily report. The API lists newest period first; a
 * reviewer token also receives drafts and blocked reports, which Today never
 * shows (they are the review queue's business). */
export function latestPublished(reports: ReportOut[]): ReportOut | null {
  return reports.find((report) => report.status === "published") ?? null;
}

export interface EvidenceLink {
  key: string;
  label: string;
  href: string | null;
}

/** One link per evidence clip, ball order then camera order. Each opens the
 * session player at that ball (`/sessions/{id}?ball=N`); a report without a
 * session has nothing to open, so its clips are listed without a link. */
export function evidenceLinks(evidence: EvidenceMap, sessionId: string | null): EvidenceLink[] {
  return Object.keys(evidence)
    .sort((a, b) => Number(a) - Number(b))
    .flatMap((ball) =>
      Object.keys(evidence[ball])
        .sort()
        .map((camera) => ({
          key: `${ball}-${camera}`,
          label: `Ball ${ball} · ${camera}`,
          href: sessionId === null ? null : `/sessions/${sessionId}?ball=${ball}`,
        })),
    );
}

/** The degraded reasons a report carries, verbatim and in render order. */
export function reportCaveats(report: ReportOut): string[] {
  const { honesty_banner, coverage_note } = report.body;
  return [honesty_banner, coverage_note].filter(
    (text): text is string => text !== null && text !== "",
  );
}

const SAFETY_LABELS: Record<SafetyCode, string> = {
  workload_ceiling: "Weekly bowling ceiling reached",
  day_pattern_violation: "Too many bowling days in a row",
  pain_flag: "Pain reported — no bowling until cleared",
};

export function safetyLabel(code: SafetyCode): string {
  return SAFETY_LABELS[code];
}

export type WorkloadTone = "success" | "danger";

/** Any violation the API reports turns the card red; otherwise it is green. */
export function workloadTone(window: WindowSummaryOut): WorkloadTone {
  return window.violations.length > 0 ? "danger" : "success";
}

/** Parent and coach may start a session (route table: `/sessions/new`). */
export function canStartSession(role: Role | null): boolean {
  return role === "parent" || role === "coach";
}

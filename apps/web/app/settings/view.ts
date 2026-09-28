/**
 * Settings screen view model (US-J5/L5). The draft is edited as strings and
 * rebuilt into the full settings object on submit (the API takes whole
 * versions). `approvalsNeeded` mirrors the router's approval gates so the
 * form can say up front which role must post a change; the server still
 * decides and its 403 detail is shown verbatim.
 */

import type { Role } from "../pipeline/access";
import type { AppSettings, AppSettingsIn, ReviewMode } from "./api";

export const SETTINGS_ROLES: readonly Role[] = ["parent"];

export interface SettingsDraft {
  mode: ReviewMode;
  timeoutHours: string;
  liveEnabled: boolean;
  allowlist: string;
  reason: string;
}

export function draftFrom(settings: AppSettings): SettingsDraft {
  return {
    mode: settings.report_review.mode,
    timeoutHours: String(settings.report_review.timeout_hours),
    liveEnabled: settings.live_mode.enabled,
    allowlist: settings.live_mode.allowlist.join("\n"),
    reason: "",
  };
}

/** One key per line, blanks and duplicates dropped, order kept. */
export function allowlistKeys(text: string): string[] {
  const keys: string[] = [];
  for (const line of text.split("\n")) {
    const key = line.trim();
    if (key !== "" && !keys.includes(key)) {
      keys.push(key);
    }
  }
  return keys;
}

export function draftProblems(draft: SettingsDraft): string[] {
  const problems: string[] = [];
  const timeout = Number(draft.timeoutHours);
  if (draft.timeoutHours.trim() === "" || !(timeout > 0)) {
    problems.push("report_review.timeout_hours must be a positive number");
  }
  if (draft.reason.trim() === "") {
    problems.push("a reason is required for every settings change");
  }
  return problems;
}

/** Rebuild the whole version, keeping any extra keys the active one carries. */
export function toSettingsIn(active: AppSettings, draft: SettingsDraft): AppSettingsIn {
  return {
    settings: {
      report_review: {
        ...active.report_review,
        mode: draft.mode,
        timeout_hours: Number(draft.timeoutHours),
      },
      live_mode: {
        ...active.live_mode,
        enabled: draft.liveEnabled,
        allowlist: allowlistKeys(draft.allowlist),
      },
    },
    reason: draft.reason.trim(),
  };
}

export interface Approvals {
  coach: boolean;
  parent: boolean;
}

/** Which approvals the change needs under the router's gates. */
export function approvalsNeeded(active: AppSettings, next: AppSettings): Approvals {
  const modeChanged = next.report_review.mode !== active.report_review.mode;
  const liveEnabling = next.live_mode.enabled && !active.live_mode.enabled;
  const current = new Set(active.live_mode.allowlist);
  const broadening = next.live_mode.allowlist.some((key) => !current.has(key));
  return { coach: modeChanged, parent: liveEnabling || broadening };
}

/** Plain sentence for the gate, or null when either guardian may post it. */
export function approvalText(approvals: Approvals, role: Role | null): string | null {
  if (approvals.coach && approvals.parent) {
    return "This change needs both coach and parent approval; post the review-mode change and the live-mode change as two versions.";
  }
  if (approvals.coach && role !== "coach") {
    return "Changing the review mode needs the coach; sign in as the coach to post it.";
  }
  if (approvals.parent && role !== "parent") {
    return "Enabling live mode or adding live keys needs the parent.";
  }
  return null;
}

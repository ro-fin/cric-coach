"use client";

/**
 * US-K5 report page: /reports?report_id=... — today's report on the tablet,
 * printable for the net wall.
 *
 * Offline tolerance is honest and per-report: a successfully fetched
 * PUBLISHED report is cached under its OWN id with a saved-at timestamp
 * (draft/blocked reports are never cached, so a shared device cannot replay
 * one after it is withdrawn). A NETWORK failure or a 5xx SERVER error may
 * fall back to that same report's cache — the banner says which, and how
 * stale the copy is. A definitive server refusal (401/403/404/410 — blocked,
 * demoted, deleted or foreign report) renders an honest error and NEVER a
 * cached copy, so revocation propagates to the device (US-G3/H5: players only
 * ever see PUBLISHED reports). A cache-write failure (quota, private mode)
 * never demotes a successful fetch. Unpublished reports carry a prominent
 * status badge for reviewers, mirroring the server's HTML render.
 */

import { useSearchParams } from "next/navigation";
import { useEffect, useState, Suspense } from "react";
import { ApiError } from "@/lib/api";
import { fetchReport, Report } from "./api";
import { readCachedReport, writeCachedReport } from "./cache";
import ExportButtons from "./ExportButtons";
import { PRINT_STYLES } from "./printStyles";
import ReportView from "./ReportView";

/** Why a cached copy is being shown instead of a live one. */
type FallbackReason = "offline" | "server-error";

type LoadState =
  | { kind: "loaded"; report: Report; offline: false; cachedAt: null }
  | { kind: "loaded"; report: Report; offline: true; cachedAt: string; reason: FallbackReason }
  | { kind: "refused"; status: number }
  | { kind: "no-cache"; reason: FallbackReason };

/** Statuses that are a definitive "no" from the server — never cache-masked:
 * unauthorized, forbidden, not found, or gone (withdrawn). Any other error
 * (a 5xx) is treated as transient and may fall back to the cache. */
const REFUSAL_STATUSES = new Set([401, 403, 404, 410]);

async function loadReport(id: string): Promise<LoadState> {
  try {
    const report = await fetchReport(id);
    if (report.status === "published") {
      // Only published reports are cached (US-G3/H5). A cache-write failure
      // (quota, private mode) must NOT demote this successful fetch.
      try {
        writeCachedReport(report);
      } catch {
        /* cache unavailable — the live report still renders */
      }
    }
    return { kind: "loaded", report, offline: false, cachedAt: null };
  } catch (error) {
    if (error instanceof ApiError && REFUSAL_STATUSES.has(error.status)) {
      // The server answered a definitive no: never mask a refusal with a cache.
      return { kind: "refused", status: error.status };
    }
    // A transient 5xx or a network failure: the report may still be valid, so
    // fall back to this report's cached copy if one exists (published only).
    const reason: FallbackReason = error instanceof ApiError ? "server-error" : "offline";
    const cached = readCachedReport(id);
    if (cached === null) {
      return { kind: "no-cache", reason };
    }
    return {
      kind: "loaded",
      report: cached.report,
      offline: true,
      cachedAt: cached.cachedAt,
      reason,
    };
  }
}

function ReportsPageInner() {
  const params = useSearchParams();
  const reportId = params.get("report_id");
  const [state, setState] = useState<LoadState | null>(null);

  useEffect(() => {
    if (reportId === null) {
      return;
    }
    void loadReport(reportId).then(setState);
  }, [reportId]);

  if (reportId === null) {
    return (
      <main>
        <h1>Report</h1>
        <p>Pick a report to view (missing report_id).</p>
      </main>
    );
  }
  if (state === null) {
    return (
      <main>
        <h1>Report</h1>
        <p>Loading…</p>
      </main>
    );
  }
  if (state.kind === "refused") {
    return (
      <main>
        <h1>Report</h1>
        <p role="alert" data-testid="refused-error">
          The server declined this report (HTTP {state.status}) — it is not available to you
          right now. It may have been withdrawn for review. No saved copy is shown for a server
          refusal.
        </p>
      </main>
    );
  }
  if (state.kind === "no-cache") {
    return (
      <main>
        <h1>Report</h1>
        <p role="alert">
          {state.reason === "server-error"
            ? "The lab server had an error and no cached copy exists on this device."
            : "Could not load the report and no cached copy exists on this device."}
        </p>
      </main>
    );
  }
  return (
    <main>
      <style>{PRINT_STYLES}</style>
      {state.offline && (
        <p role="alert" data-testid="offline-banner">
          {state.reason === "server-error" ? "The lab server had an error" : "Offline"} — showing
          the copy of this report saved on this device at {state.cachedAt}.
        </p>
      )}
      {state.report.status !== "published" && (
        <p data-testid="status-badge">STATUS: {state.report.status.toUpperCase()}</p>
      )}
      <ExportButtons reportId={state.report.id} />
      <ReportView body={state.report.body} sessionId={state.report.session_id} />
    </main>
  );
}

/** Next.js requires a Suspense boundary around useSearchParams for prerender. */
export default function ReportsPage() {
  return (
    <Suspense fallback={<main aria-busy="true" />}>
      <ReportsPageInner />
    </Suspense>
  );
}

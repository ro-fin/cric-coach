"use client";

/**
 * US-J5 coach review queue: the draft reports held by the review gate, with a
 * per-item Publish action and a link to the report render.
 *
 * Role gate: the server enforces coach-only on GET /settings/review-queue
 * (bearer token, US-L3); this component's client-side gate is honest display —
 * a 401/403 renders "coach token required", never a silently empty queue.
 *
 * Publish runs the US-G3/G6/H5 gate server-side. Its BLOCKED outcome (HTTP
 * 409) is a decision, not an error: the audited reasons list is surfaced
 * verbatim under the item so the coach sees exactly why the report did not
 * ship. Blocked is RETRYABLE — the server gate is authoritative and
 * re-runnable, so once the coach fixes the cause the same button asks again.
 * While a publish is in flight the button is disabled (one click, one POST,
 * one audit row).
 *
 * Server truth wins: every decided attempt re-fetches the queue. A decided
 * report exits DRAFT, so the server drops it from the DRAFT-only queue — the
 * item then stays rendered client-side, marked departed, with its last
 * decision attached so the coach SEES why it vanished. All strings render as
 * React text children (untrusted content stays inert), and every field shows
 * verbatim what the API returned (US-K4).
 *
 * The report link goes to the in-app render (/reports?report_id=...), which
 * mirrors the server's /reports/{id}/html section-for-section and carries the
 * draft status badge; the bearer-gated /html endpoint itself cannot
 * authenticate from a bare anchor (the ExportButtons precedent).
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ApiError,
  listReviewQueue,
  publishReport,
  PublishOut,
  ReviewQueueItemOut,
} from "@/lib/api";

type QueueState =
  | { kind: "loading" }
  | { kind: "forbidden"; status: number }
  | { kind: "error"; message: string }
  | { kind: "loaded"; items: ReviewQueueItemOut[] };

/** A definitive role refusal (US-L3): show the coach-token gate, not an error. */
const FORBIDDEN_STATUSES = new Set([401, 403]);

/** One item's Publish lifecycle: in flight, decided, or never reached the gate. */
type Decision =
  | { kind: "pending" }
  | { kind: "decided"; outcome: PublishOut }
  | { kind: "failed"; message: string };

function loadFailureMessage(error: unknown): string {
  return error instanceof ApiError
    ? `The lab server refused the review queue (HTTP ${error.status}).`
    : "Could not reach the lab server for the review queue.";
}

/** Honest failure copy: a server refusal names its HTTP status (US-K4);
 * anything else is a transport failure that never reached the gate. */
function publishFailureMessage(error: unknown): string {
  return error instanceof ApiError
    ? `The lab server refused the publish (HTTP ${error.status}) - the report is unchanged.`
    : "Publish did not reach the lab server - the report is unchanged.";
}

export default function ReviewQueue() {
  const [state, setState] = useState<QueueState>({ kind: "loading" });
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  // Items this coach decided that the server no longer lists (a decided
  // report exits DRAFT): kept on screen with the decision that explains why.
  const [departed, setDeparted] = useState<ReviewQueueItemOut[]>([]);

  useEffect(() => {
    let cancelled = false;
    listReviewQueue()
      .then((items) => {
        if (!cancelled) setState({ kind: "loaded", items });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiError && FORBIDDEN_STATUSES.has(error.status)) {
          setState({ kind: "forbidden", status: error.status });
        } else {
          setState({ kind: "error", message: loadFailureMessage(error) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function publish(item: ReviewQueueItemOut): Promise<void> {
    setDecisions((prev) => ({ ...prev, [item.id]: { kind: "pending" } }));
    let outcome: PublishOut;
    try {
      outcome = await publishReport(item.id);
    } catch (error) {
      setDecisions((prev) => ({
        ...prev,
        [item.id]: { kind: "failed", message: publishFailureMessage(error) },
      }));
      return;
    }
    setDecisions((prev) => ({ ...prev, [item.id]: { kind: "decided", outcome } }));
    // Server truth wins: re-fetch the queue after every decided attempt.
    try {
      const fresh = await listReviewQueue();
      if (!fresh.some((entry) => entry.id === item.id)) {
        setDeparted((prev) =>
          prev.some((entry) => entry.id === item.id) ? prev : [...prev, item],
        );
      }
      setState({ kind: "loaded", items: fresh });
    } catch {
      // The re-fetch failed: keep the last known queue on screen — the
      // decision banner above already carries the outcome verbatim.
    }
  }

  if (state.kind === "loading") {
    return <p aria-busy="true">Loading the review queue…</p>;
  }
  if (state.kind === "forbidden") {
    return (
      <p role="alert" data-testid="review-forbidden">
        The review queue is a coach surface (US-J5). This device&apos;s token was refused (HTTP{" "}
        {state.status}) — configure the coach bearer token to review held reports.
      </p>
    );
  }
  if (state.kind === "error") {
    return (
      <p role="alert" data-testid="review-error">
        {state.message}
      </p>
    );
  }
  const listed = new Set(state.items.map((entry) => entry.id));
  const rows = [
    ...state.items.map((item) => ({ item, isDeparted: false })),
    ...departed
      .filter((item) => !listed.has(item.id))
      .map((item) => ({ item, isDeparted: true })),
  ];
  if (rows.length === 0) {
    return (
      <p data-testid="review-empty">The review queue is clear — no draft reports are waiting.</p>
    );
  }
  return (
    <ul data-testid="review-items">
      {rows.map(({ item, isDeparted }) => {
        const decision = decisions[item.id];
        const pending = decision !== undefined && decision.kind === "pending";
        const decided = decision !== undefined && decision.kind === "decided";
        const published = decided && decision.outcome.status === "published";
        return (
          <li key={item.id} data-testid="review-item">
            <p>
              <strong>{item.kind}</strong> report · {item.period_start} to {item.period_end} ·
              review due {item.review_due_at ?? "—"}
            </p>
            {isDeparted && (
              <p data-testid="review-departed">
                No longer in the review queue — the publish decision below is why.
              </p>
            )}
            <p>
              <Link href={`/reports?report_id=${item.id}`}>View report</Link>{" "}
              <button
                type="button"
                disabled={pending || published}
                onClick={() => void publish(item)}
              >
                Publish
              </button>
            </p>
            {decision !== undefined && decision.kind === "failed" && (
              <p role="alert" data-testid="publish-error">
                {decision.message}
              </p>
            )}
            {published && (
              <p data-testid="publish-published">Published — the report is live.</p>
            )}
            {decided && decision.outcome.status === "blocked" && (
              <div role="alert" data-testid="publish-blocked">
                <p>Blocked — the publish gate refused this report:</p>
                <ul data-testid="blocked-reasons">
                  {decision.outcome.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

"use client";

/**
 * Session list (US-B5 AC: filterable by date/type; US-K1 entry point).
 * Date bounds and paging hit the sessions API; the type filter narrows the
 * fetched page client-side because the API exposes no session_type query
 * parameter. Each row shows the API's pipeline state and, for a degraded
 * capture, the missing views verbatim.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  DegradedBanner,
  EmptyState,
  ErrorState,
  PageHeader,
  Skeleton,
} from "@/components/ui";
import { ApiError, createApiClient, defaultConfig } from "@/lib/api";
import type { ApiClient, SessionPage, SessionType } from "@/lib/api";
import { useRole } from "@/lib/auth/role";
import {
  BOWLER_LABELS,
  TYPE_OPTIONS,
  degradedReasons,
  filterByType,
  pageWindow,
  stateTone,
} from "./list";

export interface SessionListClientProps {
  client?: ApiClient;
}

/** Rows per API page. */
export const PAGE_SIZE = 20;

const FIELD =
  "min-h-11 rounded-lg border-2 border-border bg-surface px-3 text-ink focus:border-accent";
const PRIMARY_LINK =
  "inline-flex min-h-11 items-center justify-center rounded-lg border-2 border-accent bg-accent px-4 font-semibold text-accent-ink hover:opacity-90";

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return "Your role cannot see sessions.";
  }
  return `Could not load sessions: ${error instanceof Error ? error.message : String(error)}`;
}

type Load =
  | { status: "loading" }
  | { status: "ready"; page: SessionPage }
  | { status: "error"; message: string };

export default function SessionListClient({ client }: SessionListClientProps) {
  const api = useMemo(() => client ?? createApiClient(defaultConfig()), [client]);
  const role = useRole();
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [type, setType] = useState<SessionType | "">("");
  const [offset, setOffset] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [load, setLoad] = useState<Load>({ status: "loading" });

  useEffect(() => {
    let current = true;
    setLoad({ status: "loading" });
    api
      .listSessions({
        dateFrom: dateFrom === "" ? undefined : dateFrom,
        dateTo: dateTo === "" ? undefined : dateTo,
        limit: PAGE_SIZE,
        offset,
      })
      .then(
        (page) => {
          if (current) setLoad({ status: "ready", page });
        },
        (reason: unknown) => {
          if (current) setLoad({ status: "error", message: errorMessage(reason) });
        },
      );
    return () => {
      current = false;
    };
  }, [api, dateFrom, dateTo, offset, attempt]);

  const canCreate = role === "parent" || role === "coach";
  const filtered = dateFrom !== "" || dateTo !== "" || type !== "";

  return (
    <main className="mx-auto w-full max-w-6xl p-4 md:p-6">
      <PageHeader
        title="Sessions"
        description="Every net session, newest first."
        actions={
          canCreate ? (
            <Link href="/sessions/new" className={PRIMARY_LINK}>
              New session
            </Link>
          ) : undefined
        }
      />
      <fieldset className="mb-6 flex flex-wrap items-end gap-4" data-print="hide">
        <legend className="sr-only">Filter sessions</legend>
        <label className="flex flex-col gap-1 font-semibold">
          From
          <input
            type="date"
            className={FIELD}
            value={dateFrom}
            onChange={(event) => {
              setOffset(0);
              setDateFrom(event.target.value);
            }}
          />
        </label>
        <label className="flex flex-col gap-1 font-semibold">
          To
          <input
            type="date"
            className={FIELD}
            value={dateTo}
            onChange={(event) => {
              setOffset(0);
              setDateTo(event.target.value);
            }}
          />
        </label>
        <label className="flex flex-col gap-1 font-semibold">
          Type
          <select
            className={FIELD}
            value={type}
            onChange={(event) => setType(event.target.value as SessionType | "")}
          >
            <option value="">all</option>
            {TYPE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      </fieldset>
      {load.status === "loading" && <Skeleton label="Loading sessions" lines={4} />}
      {load.status === "error" && (
        <ErrorState message={load.message} onRetry={() => setAttempt((n) => n + 1)} />
      )}
      {load.status === "ready" && (
        <SessionRows
          page={load.page}
          type={type}
          filtered={filtered}
          canCreate={canCreate}
          onOffset={setOffset}
        />
      )}
    </main>
  );
}

function SessionRows({
  page,
  type,
  filtered,
  canCreate,
  onOffset,
}: {
  page: SessionPage;
  type: SessionType | "";
  filtered: boolean;
  canCreate: boolean;
  onOffset: (offset: number) => void;
}) {
  const visible = filterByType(page.items, type);
  const window = pageWindow(page);
  if (visible.length === 0) {
    return filtered ? (
      <EmptyState title="No sessions match" description="Clear a filter to see more." />
    ) : (
      <EmptyState
        title="No sessions yet"
        description="Record a net session to see it here."
        action={
          canCreate ? (
            <Link href="/sessions/new" className={PRIMARY_LINK}>
              Start a session
            </Link>
          ) : undefined
        }
      />
    );
  }
  return (
    <>
      <p className="mb-3 text-ink-muted" data-testid="sessions-count">
        Showing {window.first}–{window.last} of {page.total} sessions
        {visible.length < page.items.length ? ` (${visible.length} ${type} on this page)` : ""}
      </p>
      <ul aria-label="Sessions" className="grid gap-3 md:grid-cols-2">
        {visible.map((session) => {
          const reasons = degradedReasons(session);
          return (
            <li key={session.id} className="rounded-xl border border-border bg-surface">
              <Link
                href={`/sessions/${session.id}`}
                className="flex min-h-11 flex-col gap-2 rounded-xl p-4 hover:bg-surface-raised"
              >
                <span className="text-lg font-semibold text-ink">{session.session_date}</span>
                <span className="text-ink-muted">
                  {session.session_type} · {BOWLER_LABELS[session.bowler_source]}
                </span>
                <span className="flex flex-wrap gap-2">
                  <Badge tone={stateTone(session.state)}>{session.state}</Badge>
                  {session.degraded && <Badge tone="warning">degraded</Badge>}
                </span>
              </Link>
              {reasons.length > 0 && (
                <div className="px-4 pb-4">
                  <DegradedBanner reasons={reasons} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {(window.hasPrevious || window.hasNext) && (
        <nav aria-label="Pages" className="mt-6 flex gap-3">
          <Button
            variant="secondary"
            disabled={!window.hasPrevious}
            onClick={() => onOffset(Math.max(0, page.offset - page.limit))}
          >
            Previous
          </Button>
          <Button
            variant="secondary"
            disabled={!window.hasNext}
            onClick={() => onOffset(page.offset + page.limit)}
          >
            Next
          </Button>
        </nav>
      )}
    </>
  );
}

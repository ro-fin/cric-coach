"use client";

/**
 * Session list (US-B5 AC: filterable by date/type; US-K1 entry point).
 * Date bounds hit the sessions API; the type filter narrows client-side
 * because the API exposes no session_type query parameter.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { createApiClient, defaultConfig } from "@/lib/api";
import type { ApiClient, SessionPage, SessionType } from "@/lib/api";
import { TYPE_OPTIONS, filterByType, sessionLabel } from "./list";

export interface SessionListClientProps {
  client?: ApiClient;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function SessionListClient({ client }: SessionListClientProps) {
  const api = useMemo(() => client ?? createApiClient(defaultConfig()), [client]);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [type, setType] = useState<SessionType | "">("");
  const [page, setPage] = useState<SessionPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    api
      .listSessions({
        dateFrom: dateFrom === "" ? undefined : dateFrom,
        dateTo: dateTo === "" ? undefined : dateTo,
      })
      .then(
        (result) => {
          if (!cancelled) {
            setPage(result);
          }
        },
        (reason: unknown) => {
          if (!cancelled) {
            setError(errorMessage(reason));
          }
        },
      );
    return () => {
      cancelled = true;
    };
  }, [api, dateFrom, dateTo]);

  const visible = page === null ? [] : filterByType(page.items, type);

  return (
    <main>
      <h1>Sessions</h1>
      <fieldset data-testid="session-filters">
        <legend>Filter</legend>
        <label>
          From
          <input
            type="date"
            value={dateFrom}
            onChange={(event) => setDateFrom(event.target.value)}
          />
        </label>
        <label>
          To
          <input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
        </label>
        <label>
          Type
          <select
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
      {error !== null ? (
        <p data-testid="sessions-error">Could not load sessions: {error}</p>
      ) : page === null ? (
        <p data-testid="sessions-loading">Loading sessions…</p>
      ) : (
        <>
          <p data-testid="sessions-count">
            {visible.length} of {page.total} sessions
          </p>
          {visible.length === 0 ? (
            <p data-testid="sessions-empty">No sessions match.</p>
          ) : (
            <ul aria-label="sessions">
              {visible.map((session) => (
                <li key={session.id}>
                  <Link href={`/sessions/${session.id}`}>{sessionLabel(session)}</Link>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </main>
  );
}

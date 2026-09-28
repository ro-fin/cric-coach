"use client";

/**
 * One API read with honest states: loading, ready, error (with the HTTP status
 * when the server answered, so 403 can read as "not for your role"), plus a
 * retry that re-runs the read. A stale response never overwrites a newer one.
 */

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";

export type LoadState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string; httpStatus: number | null };

export function describeError(error: unknown): { message: string; httpStatus: number | null } {
  if (error instanceof ApiError) {
    return { message: error.message, httpStatus: error.status };
  }
  return { message: error instanceof Error ? error.message : String(error), httpStatus: null };
}

export function useLoad<T>(
  load: () => Promise<T>,
): { state: LoadState<T>; retry: () => void } {
  const [state, setState] = useState<LoadState<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    setState({ status: "loading" });
    load().then(
      (data) => {
        if (current) setState({ status: "ready", data });
      },
      (error: unknown) => {
        if (current) setState({ status: "error", ...describeError(error) });
      },
    );
    return () => {
      current = false;
    };
  }, [load, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, retry };
}

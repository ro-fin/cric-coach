/**
 * The four honest states, asserted in one call (T1, phase-8-team.md §3.4, §4).
 *
 * Every data-bearing component must show, visibly and accessibly:
 *   loading   — while its primary request is pending
 *   empty     — when the request succeeds with nothing to show
 *   error     — when the request fails (HTTP 500)
 *   forbidden — when the viewer's role may not see it (HTTP 403)
 *
 * `expectHonestStates` drives a FakeApi through those four situations for the
 * given `method` (a client method name or a fetch path, see fakeApi.ts),
 * re-rendering via `render` each time, and asserts each state is on screen.
 *
 * Detection defaults follow the shell primitives' accessibility contract:
 *   loading   → an element with aria-busy="true" (Skeleton), or role="status"
 *               that is not an aria-live region (the ToastProvider's live
 *               region is always present and must never count as loading)
 *   error     → an element with role="alert" (ErrorState)
 *   forbidden → an element with role="alert"
 * Pass `loadingText`, `errorText` or `forbiddenText` to match copy instead.
 * The empty state has no universal marker, so `emptyText` is required.
 *
 * Two entry points:
 *   expectHonestStates({ api, method, empty, ... })  — drive a FakeApi (fakeApi.ts);
 *     feature-local clients can use it too by passing `api.fetch` as their fetchFn
 *     or stubbing the global fetch with it.
 *   expectHonestStatesWith({ driver, ... })          — drive ANY fake through a
 *     StateDriver (reset, hold, respondEmpty, fail), for local fakes.
 */

import { cleanup, screen } from "@testing-library/react";
import { expect } from "vitest";
import type { FakeApi, FakeMethod } from "./fakeApi";

export type TextMatch = RegExp | string;

/** How the checker steers the component's primary request. */
export interface StateDriver {
  /** Clear every control set by the calls below. */
  reset(): void;
  /** Keep the request pending; returns the release function. */
  hold(): () => void;
  /** Make the request succeed with nothing to show. */
  respondEmpty(): void;
  /** Make the request fail with this HTTP status. */
  fail(status: number): void;
}

interface Expectations {
  /** Render the component under test. */
  render: () => void;
  emptyText: TextMatch;
  loadingText?: TextMatch;
  errorText?: TextMatch;
  forbiddenText?: TextMatch;
}

export interface HonestStatesOptions extends Expectations {
  api: FakeApi;
  /** The control key of the component's primary request. */
  method: FakeMethod;
  /** The payload that means "nothing to show" for that request, e.g. `[]`. */
  empty: unknown;
}

export interface HonestStatesDriverOptions extends Expectations {
  driver: StateDriver;
}

/** A StateDriver over a FakeApi request key. */
export function fakeApiDriver(api: FakeApi, method: FakeMethod, empty: unknown): StateDriver {
  return {
    reset: () => api.reset(),
    hold: () => api.hold(method),
    respondEmpty: () => api.respondWith(method, empty),
    fail: (status) =>
      api.failWith(method, status, status === 403 ? "requires one of: ['coach']" : "internal error"),
  };
}

function busyElements(): Element[] {
  return Array.from(
    document.body.querySelectorAll('[aria-busy="true"], [role="status"]:not([aria-live])'),
  );
}

async function expectAlert(text: TextMatch | undefined): Promise<void> {
  if (text === undefined) {
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    return;
  }
  expect(await screen.findByText(text)).toBeInTheDocument();
}

export async function expectHonestStatesWith(options: HonestStatesDriverOptions): Promise<void> {
  const { driver } = options;

  // 1. loading: hold the request open and look for the busy marker.
  driver.reset();
  const release = driver.hold();
  options.render();
  if (options.loadingText === undefined) {
    expect(busyElements().length, "expected an aria-busy or non-live role=status element").toBeGreaterThan(0);
  } else {
    expect(screen.getByText(options.loadingText)).toBeInTheDocument();
  }
  release();
  cleanup();

  // 2. empty: the request succeeds with nothing.
  driver.reset();
  driver.respondEmpty();
  options.render();
  expect(await screen.findByText(options.emptyText)).toBeInTheDocument();
  cleanup();

  // 3. error: the request fails.
  driver.reset();
  driver.fail(500);
  options.render();
  await expectAlert(options.errorText);
  cleanup();

  // 4. forbidden: the role may not see it.
  driver.reset();
  driver.fail(403);
  options.render();
  await expectAlert(options.forbiddenText);
  cleanup();

  driver.reset();
}

export function expectHonestStates(options: HonestStatesOptions): Promise<void> {
  const { api, method, empty, ...expectations } = options;
  return expectHonestStatesWith({ ...expectations, driver: fakeApiDriver(api, method, empty) });
}

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
 *   loading   → an element with role="status" or aria-busy="true" (Skeleton)
 *   error     → an element with role="alert" (ErrorState)
 *   forbidden → an element with role="alert"
 * Pass `loadingText`, `errorText` or `forbiddenText` to match copy instead.
 * The empty state has no universal marker, so `emptyText` is required.
 */

import { cleanup, screen } from "@testing-library/react";
import { expect } from "vitest";
import type { FakeApi, FakeMethod } from "./fakeApi";

export type TextMatch = RegExp | string;

export interface HonestStatesOptions {
  /** Render the component under test; it must use `api` (client or fetch). */
  render: () => void;
  api: FakeApi;
  /** The control key of the component's primary request. */
  method: FakeMethod;
  /** The payload that means "nothing to show" for that request, e.g. `[]`. */
  empty: unknown;
  emptyText: TextMatch;
  loadingText?: TextMatch;
  errorText?: TextMatch;
  forbiddenText?: TextMatch;
}

function busyElements(): Element[] {
  return Array.from(document.body.querySelectorAll('[role="status"], [aria-busy="true"]'));
}

async function expectAlert(text: TextMatch | undefined): Promise<void> {
  if (text === undefined) {
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    return;
  }
  expect(await screen.findByText(text)).toBeInTheDocument();
}

export async function expectHonestStates(options: HonestStatesOptions): Promise<void> {
  const { api, method } = options;

  // 1. loading: hold the request open and look for the busy marker.
  api.reset();
  const release = api.hold(method);
  options.render();
  if (options.loadingText === undefined) {
    expect(busyElements().length, "expected a role=status or aria-busy element").toBeGreaterThan(0);
  } else {
    expect(screen.getByText(options.loadingText)).toBeInTheDocument();
  }
  release();
  cleanup();

  // 2. empty: the request succeeds with nothing.
  api.reset();
  api.respondWith(method, options.empty);
  options.render();
  expect(await screen.findByText(options.emptyText)).toBeInTheDocument();
  cleanup();

  // 3. error: the request fails.
  api.reset();
  api.failWith(method, 500, "internal error");
  options.render();
  await expectAlert(options.errorText);
  cleanup();

  // 4. forbidden: the role may not see it.
  api.reset();
  api.failWith(method, 403, "requires one of: ['coach']");
  options.render();
  await expectAlert(options.forbiddenText);
  cleanup();

  api.reset();
}

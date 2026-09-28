/**
 * The test helpers are test support (excluded from the coverage gate) but they
 * must themselves be proven: a broken fakeApi would let a broken screen pass.
 */

import { cleanup, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { useToast } from "@/components/ui/Toast";
import { ApiError } from "@/lib/api";
import { useRole } from "@/lib/auth/role";
import type { ApiClient, SessionOut } from "@/lib/api";
import { expectNoA11yViolations } from "./axe";
import { createFakeApi } from "./fakeApi";
import { clip, event, metricValue, phaseMetrics, reviewItem, session, tag, video } from "./fixtures";
import { renderWithShell } from "./render";
import { expectHonestStates, expectHonestStatesWith } from "./states";

afterEach(cleanup);

describe("fixtures", () => {
  it("build valid wire rows with overrides applied last", () => {
    expect(session().state).toBe("analyzed");
    expect(session({ state: "failed" }).state).toBe("failed");
    expect(tag(3).ball_no).toBe(3);
    expect(event(2).release_ms).toBe(20_500);
    expect(clip(2, "C1").object_key).toBe("clips/b2_C1.mp4");
    expect(video("C1", { claimed_fps: null }).claimed_fps).toBeNull();
    expect(metricValue(null, { reason: "occluded" }).reason).toBe("occluded");
    expect(phaseMetrics("contact", { x: metricValue(1) }).phase).toBe("contact");
    expect(reviewItem({ kind: "weekly" }).kind).toBe("weekly");
  });
});

describe("createFakeApi as ApiClient", () => {
  it("serves the seed and records calls", async () => {
    const api = createFakeApi({
      sessions: [session()],
      tags: { s1: [tag(1)] },
      ballMetrics: { "s1:1": [phaseMetrics("contact", {})] },
    });
    expect((await api.listSessions({ playerId: "p1" })).items).toHaveLength(1);
    expect((await api.getSession("s1")).id).toBe("s1");
    expect(await api.listTags("s1")).toHaveLength(1);
    expect(await api.listEvents("s1")).toEqual([]);
    expect(await api.listClips("s1")).toEqual([]);
    expect(await api.listSessionVideos("s1")).toEqual([]);
    expect(await api.ballMetrics("s1", 1)).toHaveLength(1);
    expect(await api.ballMetrics("s1", 2)).toEqual([]);
    expect(api.calls[0]).toEqual({ method: "listSessions", args: [{ playerId: "p1" }] });
    expect(api.calls).toHaveLength(8);
  });

  it("answers 404 for an unknown session", async () => {
    const api = createFakeApi();
    await expect(api.getSession("nope")).rejects.toMatchObject({ status: 404 });
  });

  it("fails, overrides, holds and resets", async () => {
    const api = createFakeApi({ sessions: [session()] });
    api.failWith("getSession", 403);
    await expect(api.getSession("s1")).rejects.toBeInstanceOf(ApiError);

    api.respondWith("listTags", [tag(9)]);
    expect((await api.listTags("s1"))[0].ball_no).toBe(9);

    const release = api.hold("listSessions");
    let settled = false;
    const pending = api.listSessions().then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);
    release();
    await pending;
    expect(settled).toBe(true);

    api.reset();
    expect(api.calls).toEqual([]);
    expect((await api.getSession("s1")).id).toBe("s1");
    expect(await api.listTags("s1")).toEqual([]);
  });
});

describe("createFakeApi as fetch", () => {
  it("routes known paths to the seed and extra routes by path or path+query", async () => {
    const api = createFakeApi({
      sessions: [session()],
      events: { s1: [event(1)] },
      reviewQueue: [reviewItem()],
      routes: {
        "/milestones/players/p1": [{ id: "m1" }],
        "/reports?player_id=p1&kind=weekly": [{ id: "w1" }],
      },
    });
    const base = "http://localhost:8000";
    expect(await (await api.fetch(`${base}/sessions?limit=5`)).json()).toMatchObject({ total: 1 });
    expect(await (await api.fetch(`${base}/sessions/s1`)).json()).toMatchObject({ id: "s1" });
    expect(await (await api.fetch(`${base}/sessions/s1/events`)).json()).toHaveLength(1);
    expect(await (await api.fetch(`${base}/sessions/s1/balls/1/metrics`)).json()).toEqual([]);
    expect(await (await api.fetch(`${base}/settings/review-queue`)).json()).toHaveLength(1);
    expect(await (await api.fetch(`${base}/milestones/players/p1`)).json()).toEqual([{ id: "m1" }]);
    expect(await (await api.fetch(`${base}/reports?player_id=p1&kind=weekly`)).json()).toEqual([
      { id: "w1" },
    ]);
    expect(await (await api.fetch(new URL(`${base}/sessions/s1`))).json()).toMatchObject({ id: "s1" });
    expect(await (await api.fetch(new Request(`${base}/sessions/s1`))).json()).toMatchObject({
      id: "s1",
    });
    expect(api.calls.map((call) => call.method)).toEqual([
      "listSessions",
      "getSession",
      "listEvents",
      "ballMetrics",
      "listReviewQueue",
      "/milestones/players/p1",
      "/reports?player_id=p1&kind=weekly",
      "getSession",
      "getSession",
    ]);
  });

  it("answers 404 for unknown routes and honours failWith, respondWith and hold", async () => {
    const api = createFakeApi();
    const base = "http://localhost:8000";
    const missing = await api.fetch(`${base}/nowhere`);
    expect(missing.ok).toBe(false);
    expect(missing.status).toBe(404);
    expect(await missing.text()).toContain("not found");
    expect(missing.headers.get("content-type")).toBe("application/json");

    api.failWith("/reports", 403, "coach only");
    const forbidden = await api.fetch(`${base}/reports?kind=daily`);
    expect(forbidden.status).toBe(403);
    expect(forbidden.statusText).toBe("HTTP 403");
    expect(await forbidden.json()).toEqual({ detail: "coach only" });

    api.respondWith("/reports?kind=daily", [{ id: "d1" }]);
    api.failWith("/reports", 500);
    expect(await (await api.fetch(`${base}/reports?kind=daily`)).json()).toEqual([{ id: "d1" }]);

    api.reset();
    api.respondWith("getSession", { id: "override" });
    expect(await (await api.fetch(`${base}/sessions/x`)).json()).toEqual({ id: "override" });

    const release = api.hold("/notes");
    let settled = false;
    const pending = api.fetch(`${base}/notes`, { method: "POST" }).then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);
    release();
    await pending;
    expect(api.calls.at(-1)).toEqual({ method: "/notes", args: [{}, "POST"] });
  });
});

/** A minimal honest component: the shape every real screen follows. */
function SessionCount({ client }: { client: ApiClient }) {
  const [rows, setRows] = useState<SessionOut[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    let cancelled = false;
    client.listSessions().then(
      (page) => {
        if (!cancelled) {
          setRows(page.items);
        }
      },
      (reason: unknown) => {
        if (!cancelled) {
          setError(reason as ApiError);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client]);
  if (error) {
    return <p role="alert">{error.status === 403 ? "Coach sign-in needed" : "Could not load"}</p>;
  }
  if (rows === null) {
    return <p role="status">Loading sessions…</p>;
  }
  if (rows.length === 0) {
    return <p>No sessions yet</p>;
  }
  return <ul>{rows.map((row) => <li key={row.id}>{row.id}</li>)}</ul>;
}

describe("expectHonestStates", () => {
  it("passes for a component that shows all four states", async () => {
    const api = createFakeApi({ sessions: [session()] });
    await expectHonestStates({
      render: () => renderWithShell(<SessionCount client={api} />, { role: "coach" }),
      api,
      method: "listSessions",
      empty: { items: [], total: 0, limit: 50, offset: 0 },
      emptyText: "No sessions yet",
    });
  });

  it("accepts text matchers for every state", async () => {
    const api = createFakeApi();
    await expectHonestStates({
      render: () => renderWithShell(<SessionCount client={api} />, { route: "/sessions?x=1" }),
      api,
      method: "listSessions",
      empty: { items: [], total: 0, limit: 50, offset: 0 },
      emptyText: /no sessions/i,
      loadingText: /loading sessions/i,
      errorText: "Could not load",
      forbiddenText: /sign-in needed/i,
    });
    expect(window.location.search).toBe("?x=1");
  });

  it("drives a hand-rolled local fake through a StateDriver", async () => {
    type Mode = "ok" | "empty" | number;
    let mode: Mode = "ok";
    let gate: Promise<void> = Promise.resolve();
    const local: ApiClient = {
      ...createFakeApi(),
      listSessions: async () => {
        await gate;
        if (typeof mode === "number") {
          throw new ApiError(mode, "local");
        }
        return { items: mode === "empty" ? [] : [session()], total: 0, limit: 50, offset: 0 };
      },
    };
    await expectHonestStatesWith({
      render: () => renderWithShell(<SessionCount client={local} />),
      driver: {
        reset: () => {
          mode = "ok";
          gate = Promise.resolve();
        },
        hold: () => {
          let release: () => void = () => undefined;
          gate = new Promise((resolve) => {
            release = resolve;
          });
          return release;
        },
        respondEmpty: () => {
          mode = "empty";
        },
        fail: (status) => {
          mode = status;
        },
      },
      emptyText: "No sessions yet",
    });
  });

  it("fails for a component with no loading marker", async () => {
    const api = createFakeApi();
    function Silent() {
      return <p>nothing here</p>;
    }
    await expect(
      expectHonestStates({
        render: () => renderWithShell(<Silent />),
        api,
        method: "listSessions",
        empty: [],
        emptyText: "nothing here",
      }),
    ).rejects.toThrow(/aria-busy/);
  });
});

describe("renderWithShell", () => {
  function Probe() {
    const role = useRole();
    const { toast } = useToast();
    return (
      <button type="button" onClick={() => toast({ title: "Saved" })}>
        role:{role ?? "signed-out"}
      </button>
    );
  }

  it("provides the role and the toast context", async () => {
    renderWithShell(<Probe />, { role: "coach" });
    const button = screen.getByRole("button", { name: "role:coach" });
    button.click();
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("defaults to parent and supports signed out", () => {
    renderWithShell(<Probe />);
    expect(screen.getByText("role:parent")).toBeInTheDocument();
    cleanup();
    renderWithShell(<Probe />, { role: null });
    expect(screen.getByText("role:signed-out")).toBeInTheDocument();
  });
});

describe("expectNoA11yViolations", () => {
  it("passes accessible markup and fails inaccessible markup", async () => {
    const good = renderWithShell(<button type="button">Save</button>);
    await expectNoA11yViolations(good.container);
    cleanup();
    const bad = renderWithShell(<button type="button" />);
    await expect(expectNoA11yViolations(bad.container)).rejects.toThrow(/button-name/);
    cleanup();
    const relaxed = renderWithShell(<button type="button" />);
    await expectNoA11yViolations(relaxed.container, { rules: { "button-name": { enabled: false } } });
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

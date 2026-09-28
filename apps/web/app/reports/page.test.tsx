/** US-K5: report page — fetch + per-report device cache (offline tolerance
 * with a visible staleness timestamp), honest server-refusal states (403/404
 * never render a cached copy), status badge for unpublished reports, print
 * styles and authenticated export controls. */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiBase, authHeaders } from "@/lib/api";
import type { Report } from "./api";
import { reportCacheKey } from "./cache";
import ExportButtons, { printAs } from "./ExportButtons";
import ReportsPage from "./page";
import { PRINT_STYLES } from "./printStyles";

const nav = vi.hoisted(() => ({ params: new URLSearchParams() }));

vi.mock("next/navigation", () => ({ useSearchParams: () => nav.params }));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, fetchReport: vi.fn() };
});

vi.mock("./ReportsIndex", () => ({
  default: ({ search }: { search: string }) => <p>reports index:{search}</p>,
}));

import { fetchReport } from "./api";

const fetchReportMock = vi.mocked(fetchReport);

function report(overrides: Partial<Report> = {}): Report {
  return {
    id: "r1",
    player_id: "p1",
    session_id: "s1",
    kind: "daily",
    period_start: "2026-07-09",
    period_end: "2026-07-09",
    status: "published",
    body: {
      kind: "daily",
      period: { start: "2026-07-09", end: "2026-07-09" },
      main_correction: null,
      drill: null,
      goal: null,
      secondary: [],
      positive: "Great effort today.",
      safety: null,
      honesty_banner: "Clean session - keep the same plan.",
      coverage_note: null,
      fatigue_note: null,
      claims: [],
    },
    created_at: "2026-07-09T18:00:00Z",
    ...overrides,
  };
}

function seedCache(id: string, cachedAt: string, cached: Report): void {
  window.localStorage.setItem(reportCacheKey(id), JSON.stringify({ cachedAt, report: cached }));
}

/** In-memory Storage: the test runtime's window.localStorage lacks the API. */
function stubLocalStorage(): void {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    },
  });
}

/** A Storage whose setItem always throws (quota exhausted / private mode). */
function stubThrowingSetItem(): void {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
      removeItem: (key: string) => void store.delete(key),
      clear: () => store.clear(),
    },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  stubLocalStorage();
  nav.params = new URLSearchParams("report_id=r1");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("ReportsPage", () => {
  it("lists the reports when report_id is missing", () => {
    nav.params = new URLSearchParams("player=p2");
    render(<ReportsPage />);
    expect(screen.getByText("reports index:?player=p2")).toBeInTheDocument();
    expect(fetchReportMock).not.toHaveBeenCalled();
  });

  it("shows loading, then the report, and caches it under its own id", async () => {
    fetchReportMock.mockResolvedValue(report());
    render(<ReportsPage />);
    expect(screen.getByText("Loading the report")).toBeInTheDocument();
    expect(await screen.findByTestId("positive")).toHaveTextContent("Great effort today.");
    expect(fetchReportMock).toHaveBeenCalledWith("r1");
    expect(screen.queryByTestId("offline-banner")).not.toBeInTheDocument();
    expect(screen.queryByTestId("status-badge")).not.toBeInTheDocument();
    // per-report cache entry: {cachedAt, report} under cricai.report.<id>
    const cached = window.localStorage.getItem(reportCacheKey("r1"));
    expect(cached).not.toBeNull();
    const entry = JSON.parse(cached as string) as { cachedAt: string; report: Report };
    expect(entry.report.id).toBe("r1");
    expect(new Date(entry.cachedAt).getTime()).not.toBeNaN();
    // the print stylesheet ships with the rendered report
    expect(document.querySelector("style")?.textContent).toBe(PRINT_STYLES);
  });

  it("flags unpublished reports with a status badge", async () => {
    fetchReportMock.mockResolvedValue(report({ status: "draft" }));
    render(<ReportsPage />);
    expect(await screen.findByTestId("status-badge")).toHaveTextContent("STATUS: DRAFT");
  });

  it("falls back to the same report's cache on NETWORK failure, showing staleness (US-K5)", async () => {
    seedCache("r1", "2026-07-08T09:30:00Z", report());
    fetchReportMock.mockRejectedValue(new TypeError("fetch failed"));
    render(<ReportsPage />);
    const banner = await screen.findByTestId("offline-banner");
    expect(banner).toHaveTextContent("Offline");
    expect(banner).toHaveTextContent("2026-07-08T09:30:00Z");
    expect(screen.getByTestId("positive")).toHaveTextContent("Great effort today.");
  });

  it("never serves a DIFFERENT report's cache under the requested id", async () => {
    // Cache holds sibling r2; the URL asks for r1: honest miss, not r2's body.
    seedCache("r2", "2026-07-08T09:30:00Z", report({ id: "r2" }));
    fetchReportMock.mockRejectedValue(new TypeError("fetch failed"));
    render(<ReportsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("no cached copy");
    expect(screen.queryByTestId("positive")).not.toBeInTheDocument();
  });

  it.each([403, 404])(
    "renders an honest error on HTTP %i and never a cached copy (server refusal)",
    async (status) => {
      // A cached copy for the SAME id exists, but the server actively refused:
      // revocation must propagate (US-G3/H5: players only ever see PUBLISHED).
      seedCache("r1", "2026-07-08T09:30:00Z", report());
      fetchReportMock.mockRejectedValue(new ApiError(status, "report not found"));
      render(<ReportsPage />);
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(`HTTP ${status}`);
      expect(alert).toHaveTextContent(/no longer available|not available/i);
      expect(screen.queryByTestId("positive")).not.toBeInTheDocument();
      expect(screen.queryByTestId("offline-banner")).not.toBeInTheDocument();
    },
  );

  it("treats a corrupt cache entry as no-cache and self-heals the key", async () => {
    window.localStorage.setItem(reportCacheKey("r1"), '{"cachedAt":"2026-07-08T09:30');
    fetchReportMock.mockRejectedValue(new TypeError("fetch failed"));
    render(<ReportsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("no cached copy");
    expect(window.localStorage.getItem(reportCacheKey("r1"))).toBeNull();
  });

  it("reports failure when offline with no cached copy", async () => {
    fetchReportMock.mockRejectedValue(new TypeError("fetch failed"));
    render(<ReportsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("no cached copy");
  });

  it("never caches a non-published report (shared-device draft-replay leg)", async () => {
    fetchReportMock.mockResolvedValue(report({ status: "draft" }));
    render(<ReportsPage />);
    expect(await screen.findByTestId("status-badge")).toHaveTextContent("STATUS: DRAFT");
    expect(window.localStorage.getItem(reportCacheKey("r1"))).toBeNull();
  });

  it("does not replay a PRE-EXISTING cached draft on network failure (filter on read)", async () => {
    // A draft was cached before it lost publication; a later offline load must
    // not resurrect it — the entry is a miss and is healed away.
    seedCache("r1", "2026-07-08T09:30:00Z", report({ status: "draft" }));
    fetchReportMock.mockRejectedValue(new TypeError("fetch failed"));
    render(<ReportsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("no cached copy");
    expect(screen.queryByTestId("positive")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(reportCacheKey("r1"))).toBeNull();
  });

  it("keeps a successfully fetched report when the cache write throws (quota)", async () => {
    stubThrowingSetItem();
    fetchReportMock.mockResolvedValue(report());
    render(<ReportsPage />);
    expect(await screen.findByTestId("positive")).toHaveTextContent("Great effort today.");
    expect(screen.queryByTestId("offline-banner")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("serves the cached copy on a 5xx server error with neutral copy, not a refusal", async () => {
    seedCache("r1", "2026-07-08T09:30:00Z", report());
    fetchReportMock.mockRejectedValue(new ApiError(500, "internal error"));
    render(<ReportsPage />);
    const banner = await screen.findByTestId("offline-banner");
    expect(banner).toHaveTextContent(/lab server had an error/i);
    expect(banner).toHaveTextContent("2026-07-08T09:30:00Z");
    expect(banner).not.toHaveTextContent(/offline/i);
    expect(screen.getByTestId("positive")).toHaveTextContent("Great effort today.");
    expect(screen.queryByTestId("refused-error")).not.toBeInTheDocument();
  });

  it("shows a server-error message on 5xx with no cached copy", async () => {
    fetchReportMock.mockRejectedValue(new ApiError(503, "unavailable"));
    render(<ReportsPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/lab server had an error/i);
    expect(alert).toHaveTextContent("no cached copy");
  });

  it("retries after a failure with no cached copy", async () => {
    fetchReportMock.mockRejectedValueOnce(new ApiError(503, "unavailable"));
    fetchReportMock.mockResolvedValue(report());
    const user = userEvent.setup();
    render(<ReportsPage />);
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("positive")).toBeInTheDocument();
    expect(fetchReportMock).toHaveBeenCalledTimes(2);
  });

  it("badges a blocked report in danger", async () => {
    fetchReportMock.mockResolvedValue(report({ status: "blocked" }));
    render(<ReportsPage />);
    const badge = await screen.findByText("STATUS: BLOCKED");
    expect(badge).toHaveAttribute("data-tone", "danger");
  });
});

describe("ExportButtons", () => {
  const createObjectURL = vi.fn(() => "blob:cricai-export");
  const revokeObjectURL = vi.fn();
  const exportFetch = vi.fn();

  beforeEach(() => {
    createObjectURL.mockClear();
    revokeObjectURL.mockClear();
    exportFetch.mockReset();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    vi.stubGlobal("fetch", exportFetch);
  });

  it("downloads the PDF via an authenticated fetch and a blob object URL (US-K5)", async () => {
    exportFetch.mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["%PDF"], { type: "application/pdf" }),
    } as unknown as Response);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    render(<ExportButtons reportId="r1" />);
    await user.click(screen.getByRole("button", { name: "Export PDF" }));
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:cricai-export"));
    // the request carries the shared auth headers a bare <a href> could never send
    expect(exportFetch).toHaveBeenCalledWith(`${apiBase()}/reports/r1/export?format=pdf`, {
      headers: authHeaders(),
    });
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const anchor = clickSpy.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toBe("report-r1.pdf");
    expect(anchor.getAttribute("href")).toBe("blob:cricai-export");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    clickSpy.mockRestore();
  });

  it("downloads the PNG format the same way", async () => {
    exportFetch.mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["png"], { type: "image/png" }),
    } as unknown as Response);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    render(<ExportButtons reportId="r1" />);
    await user.click(screen.getByRole("button", { name: "Export PNG" }));
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalled());
    expect(exportFetch).toHaveBeenCalledWith(`${apiBase()}/reports/r1/export?format=png`, {
      headers: authHeaders(),
    });
    const anchor = clickSpy.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toBe("report-r1.png");
    clickSpy.mockRestore();
  });

  it("shows an honest error when the export request fails, and clears it on success", async () => {
    exportFetch.mockResolvedValueOnce({ ok: false, status: 401 } as unknown as Response);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    render(<ExportButtons reportId="r1" />);
    await user.click(screen.getByRole("button", { name: "Export PDF" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Export failed");
    // next attempt succeeds: the stale error is withdrawn
    exportFetch.mockResolvedValueOnce({
      ok: true,
      blob: async () => new Blob(["%PDF"]),
    } as unknown as Response);
    await user.click(screen.getByRole("button", { name: "Export PDF" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    clickSpy.mockRestore();
  });

  it("prints the net-wall sheet or the full report, then clears the mode", () => {
    const modes: (string | undefined)[] = [];
    window.print = vi.fn(() => {
      modes.push(document.documentElement.dataset.printMode);
    });
    render(<ExportButtons reportId="r1" />);
    fireEvent.click(screen.getByRole("button", { name: "Print net-wall sheet" }));
    fireEvent.click(screen.getByRole("button", { name: "Print full report" }));
    expect(modes).toEqual(["wall", "full"]);
    expect(document.documentElement.dataset.printMode).toBeUndefined();
  });

  it("clears the print mode even when printing throws", () => {
    window.print = vi.fn(() => {
      throw new Error("no printer");
    });
    expect(() => printAs("wall")).toThrow("no printer");
    expect(document.documentElement.dataset.printMode).toBeUndefined();
  });
});

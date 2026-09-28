import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { expectNoA11yViolations } from "@/test/axe";
import { dailyReport, player } from "@/test/fixtures.core";
import type { Report } from "./api";
import ReportsIndex, { statusTone } from "./ReportsIndex";
import type { ReportsIndexApi } from "./ReportsIndex";

function asReport(o: Parameters<typeof dailyReport>[0] = {}): Report {
  return dailyReport(o) as unknown as Report;
}

function fakeApi(o: Partial<ReportsIndexApi> = {}): ReportsIndexApi {
  return {
    listPlayers: vi.fn(async () => [player(), player({ id: "p2", name: "Meera" })]),
    listReports: vi.fn(async () => [
      asReport({ id: "r2", status: "draft", period_start: "2026-09-28", period_end: "2026-09-28" }),
      asReport({
        id: "w1",
        kind: "weekly",
        period_start: "2026-09-21",
        period_end: "2026-09-27",
      }),
    ]),
    ...o,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ReportsIndex", () => {
  it("lists the player's reports newest first with status badges", async () => {
    const api = fakeApi();
    render(<ReportsIndex search="?player=p2" api={api} />);
    const list = await screen.findByRole("list", { name: "Reports" });
    const links = within(list).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/reports?report_id=r2",
      "/reports?report_id=w1",
    ]);
    expect(links[0]).toHaveTextContent("daily report");
    expect(links[0]).toHaveTextContent("2026-09-28");
    expect(within(links[0]).getByText("draft")).toHaveAttribute("data-tone", "info");
    expect(links[1]).toHaveTextContent("2026-09-21 to 2026-09-27");
    expect(screen.getByText("Meera")).toBeInTheDocument();
    expect(api.listReports).toHaveBeenCalledWith("p2", null);
  });

  it("filters by kind through the API", async () => {
    const api = fakeApi();
    const user = userEvent.setup();
    render(<ReportsIndex search="" api={api} />);
    await screen.findByRole("list", { name: "Reports" });
    await user.click(screen.getByRole("button", { name: "Weekly" }));
    expect(screen.getByRole("button", { name: "Weekly" })).toHaveAttribute("aria-pressed", "true");
    await screen.findByRole("list", { name: "Reports" });
    expect(api.listReports).toHaveBeenLastCalledWith("p1", "weekly");
  });

  it("shows the empty, error, forbidden and loading states", async () => {
    const user = userEvent.setup();
    const empty = render(<ReportsIndex search="" api={fakeApi({ listReports: vi.fn(async () => []) })} />);
    expect(await screen.findByText("No reports yet")).toBeInTheDocument();
    empty.unmount();

    const failing = fakeApi({
      listReports: vi
        .fn()
        .mockRejectedValueOnce(new ApiError(500, "boom"))
        .mockResolvedValue([asReport()]),
    });
    const second = render(<ReportsIndex search="" api={failing} />);
    expect(await screen.findByText("Could not load reports: API 500: boom")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Reports" })).toBeInTheDocument();
    second.unmount();

    const forbidden = render(
      <ReportsIndex
        search=""
        api={fakeApi({ listPlayers: vi.fn().mockRejectedValue(new ApiError(403, "no")) })}
      />,
    );
    expect(await screen.findByText("Your role cannot see players.")).toBeInTheDocument();
    forbidden.unmount();

    let release!: () => void;
    render(
      <ReportsIndex
        search=""
        api={fakeApi({
          listPlayers: () =>
            new Promise((resolve) => {
              release = () => resolve([]);
            }),
        })}
      />,
    );
    expect(screen.getByText("Loading players")).toBeInTheDocument();
    await act(async () => release());
    expect(await screen.findByText("No players yet")).toBeInTheDocument();
  });

  it("maps statuses to tones", () => {
    expect(statusTone("published")).toBe("success");
    expect(statusTone("blocked")).toBe("danger");
    expect(statusTone("mystery")).toBe("neutral");
  });

  it("builds the default client from the shared API helpers", async () => {
    const fetchFn = vi.fn(async (url: string) =>
      new Response(JSON.stringify(url.includes("/players") ? [player()] : []), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchFn);
    render(<ReportsIndex search="" />);
    expect(await screen.findByText("No reports yet")).toBeInTheDocument();
    expect(fetchFn).toHaveBeenCalledWith(
      expect.stringMatching(/\/reports\?player_id=p1$/),
      expect.anything(),
    );
  });

  it("is axe clean", async () => {
    const { container } = render(<ReportsIndex search="" api={fakeApi()} />);
    await screen.findByRole("list", { name: "Reports" });
    await expectNoA11yViolations(container);
  });
});

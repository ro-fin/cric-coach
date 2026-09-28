/** US-K2: zone analytics table renders server metrics and selects cells. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { HeatmapCell } from "./api";
import ZoneTable from "./ZoneTable";

const CELLS: HeatmapCell[] = [
  {
    line: "off",
    length: "good",
    balls: 6,
    control_pct: 60,
    false_shot_pct: 20,
    sources: { manual: 4, auto: 2 },
    hollow: 1,
  },
  {
    line: "off",
    length: "full",
    balls: 1,
    control_pct: null,
    false_shot_pct: null,
    sources: { manual: 1, auto: 0 },
    hollow: 0,
  },
  {
    line: "leg",
    length: "good",
    balls: 2,
    control_pct: 50,
    false_shot_pct: 0,
    sources: { manual: 0, auto: 2 },
    hollow: 0,
  },
];

describe("ZoneTable", () => {
  it("renders one row per server cell with counts, percentages and provenance", () => {
    render(<ZoneTable cells={CELLS} selectedCell={null} onSelectCell={vi.fn()} />);
    const row = screen.getByTestId("zone-row-off-good");
    expect(row).toHaveTextContent("off");
    expect(row).toHaveTextContent("6 balls");
    expect(row).toHaveTextContent("60%");
    expect(row).toHaveTextContent("20%");
    expect(row).toHaveTextContent("4 / 2");
  });

  it("says unknowable percentages in words, never as 0%", () => {
    render(<ZoneTable cells={CELLS} selectedCell={null} onSelectCell={vi.fn()} />);
    const row = screen.getByTestId("zone-row-off-full");
    expect(row).toHaveTextContent("1 ball");
    expect(row).toHaveTextContent("no tagged balls");
    expect(row).not.toHaveTextContent("0%");
  });

  it("keeps rows as valid table rows and marks the selected cell's button", () => {
    const onSelectCell = vi.fn();
    render(
      <ZoneTable
        cells={CELLS}
        selectedCell={{ line: "off", length: "good" }}
        onSelectCell={onSelectCell}
      />,
    );
    // The interactive control is a real <button> in the first cell — the row
    // itself carries no role, so the tbody's children stay valid rows (ARIA).
    expect(screen.getByTestId("zone-row-off-good")).not.toHaveAttribute("role");
    expect(screen.getByTestId("zone-select-off-good")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("zone-select-off-full")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("zone-select-leg-good")).toHaveAttribute("aria-pressed", "false");
  });

  it("selects a cell from the keyboard: the label is a native button (WCAG 2.1.1)", async () => {
    const onSelectCell = vi.fn();
    const user = userEvent.setup();
    render(<ZoneTable cells={CELLS} selectedCell={null} onSelectCell={onSelectCell} />);
    const button = screen.getByTestId("zone-select-leg-good");
    // a real <button>: implicit role, natively focusable, Enter/Space operable
    expect(button.tagName).toBe("BUTTON");
    expect(button).toHaveAttribute("type", "button");
    button.focus();
    expect(document.activeElement).toBe(button);
    await user.keyboard("{Enter}");
    expect(onSelectCell).toHaveBeenCalledWith({ line: "leg", length: "good" });
    await user.keyboard(" ");
    expect(onSelectCell).toHaveBeenCalledTimes(2);
    // pointer selection resolves the same cell
    await user.click(screen.getByTestId("zone-select-off-full"));
    expect(onSelectCell).toHaveBeenLastCalledWith({ line: "off", length: "full" });
  });

  it("says so when the map has no placed bounces yet", () => {
    render(<ZoneTable cells={[]} selectedCell={null} onSelectCell={vi.fn()} />);
    expect(screen.getByTestId("zone-table-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("zone-table")).not.toBeInTheDocument();
  });
});

describe("ZoneTable scroll region", () => {
  it("is a named, keyboard-focusable region", () => {
    render(<ZoneTable cells={CELLS} selectedCell={null} onSelectCell={vi.fn()} />);
    expect(screen.getByRole("region", { name: "Zone analytics table" })).toHaveAttribute("tabindex", "0");
  });
});

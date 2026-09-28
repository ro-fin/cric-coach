import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import TimelineFilters from "./TimelineFilters";
import { EMPTY_FILTER } from "./timeline";
import type { TimelineFilter } from "./timeline";

function setup(filter: TimelineFilter = EMPTY_FILTER, blocks: number[] = [1, 2]) {
  const onChange = vi.fn();
  render(<TimelineFilters filter={filter} blocks={blocks} onChange={onChange} />);
  return onChange;
}

describe("TimelineFilters (US-K1 AC: block/line/length/shot/outcome/control)", () => {
  it("names each select by its label alone, not by its option texts", () => {
    setup();
    for (const name of ["Block", "Line", "Length", "Shot", "Outcome", "Control"]) {
      const select = screen.getByRole("combobox", { name });
      expect(select).toHaveAccessibleName(name);
    }
  });

  it("renders every filter control with its vocabulary", () => {
    setup();
    expect(screen.getByLabelText("Block")).toBeInTheDocument();
    expect(screen.getByLabelText("Line")).toBeInTheDocument();
    expect(screen.getByLabelText("Length")).toBeInTheDocument();
    expect(screen.getByLabelText("Shot")).toBeInTheDocument();
    expect(screen.getByLabelText("Outcome")).toBeInTheDocument();
    expect(screen.getByLabelText("Control")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "outside_off" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "yorker" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "sweep" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "beaten" })).toBeInTheDocument();
  });

  it("emits block as a number and clears it back to null", async () => {
    const user = userEvent.setup();
    const onChange = setup();
    await user.selectOptions(screen.getByLabelText("Block"), "2");
    expect(onChange).toHaveBeenLastCalledWith({ ...EMPTY_FILTER, block: 2 });
    const active = { ...EMPTY_FILTER, block: 2 };
    onChange.mockClear();
    render(<TimelineFilters filter={active} blocks={[1, 2]} onChange={onChange} />);
    const [, blockSelect] = screen.getAllByLabelText("Block");
    await user.selectOptions(blockSelect, "");
    expect(onChange).toHaveBeenLastCalledWith(EMPTY_FILTER);
  });

  it.each([
    ["Line", "leg", { line: "leg" }],
    ["Length", "short", { length: "short" }],
    ["Shot", "pull", { shot: "pull" }],
    ["Outcome", "beaten", { outcome: "beaten" }],
  ] as const)("emits %s selections", async (label, value, expected) => {
    const user = userEvent.setup();
    const onChange = setup();
    await user.selectOptions(screen.getByLabelText(label), value);
    expect(onChange).toHaveBeenLastCalledWith({ ...EMPTY_FILTER, ...expected });
  });

  it("clears an enum filter back to null and shows the current value", async () => {
    const user = userEvent.setup();
    const filter = { ...EMPTY_FILTER, line: "leg" as const };
    const onChange = setup(filter);
    expect(screen.getByLabelText("Line")).toHaveValue("leg");
    await user.selectOptions(screen.getByLabelText("Line"), "");
    expect(onChange).toHaveBeenLastCalledWith(EMPTY_FILTER);
  });

  it("emits control as a boolean in both directions and clears it", async () => {
    const user = userEvent.setup();
    const onChange = setup();
    await user.selectOptions(screen.getByLabelText("Control"), "true");
    expect(onChange).toHaveBeenLastCalledWith({ ...EMPTY_FILTER, control: true });
    await user.selectOptions(screen.getByLabelText("Control"), "false");
    expect(onChange).toHaveBeenLastCalledWith({ ...EMPTY_FILTER, control: false });
    onChange.mockClear();
    render(
      <TimelineFilters
        filter={{ ...EMPTY_FILTER, control: false }}
        blocks={[]}
        onChange={onChange}
      />,
    );
    const [, controlSelect] = screen.getAllByLabelText("Control");
    expect(controlSelect).toHaveValue("false");
    await user.selectOptions(controlSelect, "");
    expect(onChange).toHaveBeenLastCalledWith(EMPTY_FILTER);
  });

  it("clear-filters resets everything and is disabled when nothing is active", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <TimelineFilters
        filter={{ ...EMPTY_FILTER, shot: "pull", block: 1 }}
        blocks={[1]}
        onChange={onChange}
      />,
    );
    const clear = screen.getByRole("button", { name: "Clear filters" });
    expect(clear).toBeEnabled();
    await user.click(clear);
    expect(onChange).toHaveBeenCalledWith(EMPTY_FILTER);
    onChange.mockClear();
    render(<TimelineFilters filter={EMPTY_FILTER} blocks={[]} onChange={onChange} />);
    const [, disabledClear] = screen.getAllByRole("button", { name: "Clear filters" });
    expect(disabledClear).toBeDisabled();
  });
});

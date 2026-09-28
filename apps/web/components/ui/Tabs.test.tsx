import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { expectAxeClean } from "@/lib/testing/axe";
import { Tab, TabList, TabPanel, Tabs } from "./Tabs";

function Example(props: { value?: string; onValueChange?: (value: string) => void }) {
  return (
    <Tabs defaultValue="front" {...props}>
      <TabList label="Camera">
        <Tab value="front">Front</Tab>
        <Tab value="side on">Side on</Tab>
        <Tab value="off" disabled>
          Off
        </Tab>
        <Tab value="rear">Rear</Tab>
      </TabList>
      <TabPanel value="front">front panel</TabPanel>
      <TabPanel value="side on" className="p">
        side panel
      </TabPanel>
      <TabPanel value="rear">rear panel</TabPanel>
    </Tabs>
  );
}

describe("Tabs", () => {
  it("links tabs and panels and shows the selected panel only", async () => {
    const { container } = render(<Example />);
    const front = screen.getByRole("tab", { name: "Front" });
    expect(screen.getByRole("tablist", { name: "Camera" })).toBeInTheDocument();
    expect(front).toHaveAttribute("aria-selected", "true");
    expect(front).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "Side on" })).toHaveAttribute("tabindex", "-1");
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveTextContent("front panel");
    expect(panel).toHaveAttribute("aria-labelledby", front.id);
    expect(front).toHaveAttribute("aria-controls", panel.id);
    expect(front.id).not.toMatch(/ /);
    await expectAxeClean(container);
  });

  it("selects on click", () => {
    render(<Example />);
    fireEvent.click(screen.getByRole("tab", { name: "Side on" }));
    expect(screen.getByRole("tabpanel")).toHaveTextContent("side panel");
    expect(screen.getByRole("tabpanel")).toHaveClass("p");
  });

  it("moves with arrow keys, Home and End, skipping disabled tabs and wrapping", () => {
    render(<Example />);
    const list = screen.getByRole("tablist");
    const front = screen.getByRole("tab", { name: "Front" });
    front.focus();
    fireEvent.keyDown(list, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Side on" })).toHaveFocus();
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(screen.getByRole("tab", { name: "Rear" })).toHaveFocus();
    expect(screen.getByRole("tabpanel")).toHaveTextContent("rear panel");
    fireEvent.keyDown(list, { key: "ArrowRight" });
    expect(front).toHaveFocus();
    fireEvent.keyDown(list, { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Rear" })).toHaveFocus();
    fireEvent.keyDown(list, { key: "ArrowUp" });
    expect(screen.getByRole("tab", { name: "Side on" })).toHaveFocus();
    fireEvent.keyDown(list, { key: "Home" });
    expect(front).toHaveFocus();
    fireEvent.keyDown(list, { key: "End" });
    expect(screen.getByRole("tab", { name: "Rear" })).toHaveFocus();
  });

  it("ignores other keys and starts from the first tab when focus is elsewhere", () => {
    render(<Example />);
    const list = screen.getByRole("tablist");
    fireEvent.keyDown(list, { key: "a" });
    expect(document.body).toHaveFocus();
    fireEvent.keyDown(list, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Side on" })).toHaveFocus();
  });

  it("can be controlled", () => {
    const onValueChange = vi.fn();
    const { rerender } = render(<Example value="rear" onValueChange={onValueChange} />);
    expect(screen.getByRole("tabpanel")).toHaveTextContent("rear panel");
    fireEvent.click(screen.getByRole("tab", { name: "Front" }));
    expect(onValueChange).toHaveBeenCalledWith("front");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("rear panel");
    rerender(<Example value="front" onValueChange={onValueChange} />);
    expect(screen.getByRole("tabpanel")).toHaveTextContent("front panel");
  });

  it("selects nothing without a default", () => {
    render(
      <Tabs className="wrap">
        <TabList label="Empty" className="l">
          <Tab value="a">A</Tab>
        </TabList>
        <TabPanel value="a">a</TabPanel>
      </Tabs>,
    );
    expect(screen.queryByRole("tabpanel")).toBeNull();
    expect(screen.getByRole("tablist")).toHaveClass("l");
  });

  it("refuses to render parts outside <Tabs>", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Tab value="x">x</Tab>)).toThrow("<Tab> must be used inside <Tabs>");
    expect(() => render(<TabList label="x">x</TabList>)).toThrow("<TabList>");
    expect(() => render(<TabPanel value="x">x</TabPanel>)).toThrow("<TabPanel>");
    vi.restoreAllMocks();
  });
});

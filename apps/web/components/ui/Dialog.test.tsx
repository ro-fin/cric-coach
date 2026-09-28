import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { expectAxeClean } from "@/lib/testing/axe";
import { Dialog } from "./Dialog";

function Harness({ withField = true }: { withField?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <Dialog open={open} onOpenChange={setOpen} title="Edit note">
        {withField && <input aria-label="Note" />}
        <p>body</p>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("renders nothing while closed", () => {
    render(<Dialog open={false} onOpenChange={vi.fn()} title="Hidden">x</Dialog>);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is a labelled modal that takes focus and returns it on close", async () => {
    const { container } = render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = screen.getByRole("dialog", { name: "Edit note" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    await expectAxeClean(container);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("closes on Escape and on the backdrop", () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    fireEvent.click(screen.getByTestId("dialog-backdrop"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("traps Tab inside the dialog in both directions", () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    const dialog = screen.getByRole("dialog");
    const close = screen.getByRole("button", { name: "Close" });
    const field = screen.getByRole("textbox", { name: "Note" });
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(field).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(close).toHaveFocus();
    // Tabbing between inner elements is left to the browser.
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(close).toHaveFocus();
    field.focus();
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(field).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Enter" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });
});

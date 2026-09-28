import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { expectAxeClean } from "@/lib/testing/axe";
import { TOAST_DURATION_MS, ToastProvider, useToast, type ToastInput } from "./Toast";

function Trigger({ input }: { input: ToastInput }) {
  const { toast } = useToast();
  return (
    <button type="button" onClick={() => toast(input)}>
      Fire
    </button>
  );
}

describe("useToast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("announces a toast politely and dismisses it after the duration", () => {
    render(
      <ToastProvider>
        <Trigger input={{ title: "Report published", description: "Sent to the wall", tone: "success" }} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Fire" }));
    const region = screen.getByRole("region", { name: "Notifications" });
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("Report published");
    expect(region).toHaveTextContent("Sent to the wall");
    expect(region.querySelector('[data-tone="success"]')).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(TOAST_DURATION_MS);
    });
    expect(region).not.toHaveTextContent("Report published");
  });

  it("defaults to the neutral tone and can be dismissed by hand", () => {
    render(
      <ToastProvider>
        <Trigger input={{ title: "Saved" }} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Fire" }));
    fireEvent.click(screen.getByRole("button", { name: "Fire" }));
    const region = screen.getByRole("region", { name: "Notifications" });
    expect(region.querySelectorAll('[data-tone="neutral"]')).toHaveLength(2);
    fireEvent.click(screen.getAllByRole("button", { name: "Dismiss: Saved" })[0]);
    expect(region.querySelectorAll('[data-tone="neutral"]')).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(TOAST_DURATION_MS);
    });
    expect(region.querySelectorAll('[data-tone="neutral"]')).toHaveLength(0);
  });

  it("clears pending timers on unmount", () => {
    const clear = vi.spyOn(globalThis, "clearTimeout");
    const { unmount } = render(
      <ToastProvider>
        <Trigger input={{ title: "Bye" }} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Fire" }));
    unmount();
    expect(clear).toHaveBeenCalled();
    clear.mockRestore();
  });

  it("throws outside the provider", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Trigger input={{ title: "x" }} />)).toThrow(/ToastProvider/);
    vi.restoreAllMocks();
  });
});

describe("toast accessibility", () => {
  it("is axe clean with a toast showing", async () => {
    const { container } = render(
      <ToastProvider>
        <Trigger input={{ title: "Heads up", tone: "warning" }} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Fire" }));
    await expectAxeClean(container);
  });
});

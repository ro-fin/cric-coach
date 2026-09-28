import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expectAxeClean } from "@/lib/testing/axe";
import { LoginForm } from "./LoginForm";
import LoginPage, { metadata } from "./page";

const browser = vi.hoisted(() => ({ hardNavigate: vi.fn() }));
vi.mock("@/lib/browser", () => browser);

afterEach(() => {
  vi.unstubAllGlobals();
  browser.hardNavigate.mockReset();
});

function reply(status: number, body: unknown) {
  return vi.fn(async () =>
    typeof body === "string" ? new Response(body, { status }) : Response.json(body, { status }),
  );
}

function fill(role: string | null, token: string) {
  if (role) {
    fireEvent.click(screen.getByRole("radio", { name: new RegExp(role) }));
  }
  fireEvent.change(screen.getByLabelText("Role token"), { target: { value: token } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("LoginPage", () => {
  it("renders the sign-in form and passes a safe next path", async () => {
    const fetchFn = reply(200, { role: "coach" });
    vi.stubGlobal("fetch", fetchFn);
    const page = await LoginPage({ searchParams: Promise.resolve({ next: "//evil.example" }) });
    const { container } = render(page);
    expect(screen.getByRole("heading", { level: 1, name: "Sign in to cricAI" })).toBeInTheDocument();
    await expectAxeClean(container);
    fill("Coach", "c-tok");
    await waitFor(() => expect(browser.hardNavigate).toHaveBeenCalledWith("/"));
    expect(metadata.title).toBe("Sign in — cricAI");
  });
});

describe("LoginForm", () => {
  it("offers the three roles with the token field", () => {
    render(<LoginForm next="/" />);
    expect(screen.getByRole("group", { name: "Who is using cricAI?" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    expect(screen.getByLabelText("Role token")).toHaveAttribute("type", "password");
  });

  it("posts role and token to the session endpoint and goes to next", async () => {
    const fetchFn = reply(200, { role: "player" });
    vi.stubGlobal("fetch", fetchFn);
    render(<LoginForm next="/progress" />);
    fill("Player", "k-tok");
    await waitFor(() => expect(browser.hardNavigate).toHaveBeenCalledWith("/progress"));
    expect(fetchFn).toHaveBeenCalledWith("/api/cricai/_session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "player", token: "k-tok" }),
    });
    expect(screen.getByRole("radio", { name: /Player/ })).toBeChecked();
  });

  it("asks for both fields before calling the server", () => {
    const fetchFn = reply(200, {});
    vi.stubGlobal("fetch", fetchFn);
    render(<LoginForm next="/" />);
    fill(null, "k-tok");
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a role and enter its token.");
    fill("Parent", "   ");
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a role and enter its token.");
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it("shows the server's reason and lets the user try again", async () => {
    vi.stubGlobal("fetch", reply(403, { detail: "That token is not a coach token." }));
    render(<LoginForm next="/" />);
    fill("Coach", "k-tok");
    expect(await screen.findByRole("alert")).toHaveTextContent("That token is not a coach token.");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
    expect(browser.hardNavigate).not.toHaveBeenCalled();
  });

  it("falls back to the HTTP status when the reason is missing or not JSON", async () => {
    vi.stubGlobal("fetch", reply(502, { nope: 1 }));
    render(<LoginForm next="/" />);
    fill("Coach", "c");
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in failed (HTTP 502).");
    vi.stubGlobal("fetch", reply(500, "<html>"));
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Sign-in failed (HTTP 500)."),
    );
  });

  it("explains a network failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("offline");
      }),
    );
    render(<LoginForm next="/" />);
    fill("Parent", "p");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This device could not reach the dashboard server.",
    );
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RoleProvider, type Role } from "@/lib/auth/role";
import { player } from "@/test/fixtures.core";
import NotesPageBody from "./NotesPageBody";

vi.mock("./NotesPanel", () => ({
  default: ({
    playerId,
    sessionId,
    role,
  }: {
    playerId: string;
    sessionId?: string;
    role: string | null;
  }) => (
    <p>
      panel:{playerId}:{String(sessionId)}:{String(role)}
    </p>
  ),
}));

function withRole(ui: ReactElement, role: Role | null = "coach") {
  return render(<RoleProvider role={role}>{ui}</RoleProvider>);
}

const TWO = async () => [player(), player({ id: "p2", name: "Meera" })];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("NotesPageBody", () => {
  it("opens on the linked player with the signed-in role and switches players", async () => {
    const user = userEvent.setup();
    withRole(<NotesPageBody playerIdParam="p2" sessionId="s1" listPlayers={TWO} />, "parent");
    expect(await screen.findByText("panel:p2:s1:parent")).toBeInTheDocument();
    expect(screen.getByText("Notes pinned to session s1.")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Player"), "p1");
    expect(await screen.findByText("panel:p1:s1:parent")).toBeInTheDocument();
  });

  it("falls back to the first player and hides the picker for one player", async () => {
    withRole(
      <NotesPageBody playerIdParam={null} sessionId={null} listPlayers={async () => [player()]} />,
      "player",
    );
    expect(await screen.findByText("panel:p1:undefined:player")).toBeInTheDocument();
    expect(screen.queryByLabelText("Player")).not.toBeInTheDocument();
    expect(screen.getByText(/beside the machine numbers/)).toBeInTheDocument();
  });

  it("shows loading, error with retry, and empty players", async () => {
    const user = userEvent.setup();
    const listPlayers = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue([]);
    withRole(<NotesPageBody playerIdParam={null} sessionId={null} listPlayers={listPlayers} />);
    expect(screen.getByText("Loading players")).toBeInTheDocument();
    expect(await screen.findByText("Could not load players: offline")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No players yet")).toBeInTheDocument();
  });

  it("builds the default player client", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify([player()]), { status: 200 })),
    );
    withRole(<NotesPageBody playerIdParam={null} sessionId={null} />);
    expect(await screen.findByText("panel:p1:undefined:coach")).toBeInTheDocument();
  });
});

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectNoA11yViolations } from "@/test/axe";
import { ForbiddenState, rolesText } from "./index";

describe("ForbiddenState", () => {
  it("names the roles the screen serves and shows the server reason", async () => {
    const { container } = render(
      <ForbiddenState roles={["parent", "coach"]} detail="requires one of: [coach, parent]" />,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("This screen is for the parent or coach.");
    expect(alert).toHaveTextContent("Sign in with that role to see it.");
    expect(alert).toHaveTextContent("requires one of: [coach, parent]");
    await expectNoA11yViolations(container);
  });

  it("omits the detail line without one", () => {
    const { container } = render(<ForbiddenState roles={["parent"]} />);
    expect(screen.getByRole("alert")).toHaveTextContent("This screen is for the parent.");
    expect(container.querySelectorAll("p")).toHaveLength(2);
  });
});

describe("rolesText", () => {
  it("reads naturally for any count", () => {
    expect(rolesText([])).toBe("");
    expect(rolesText(["coach"])).toBe("coach");
    expect(rolesText(["parent", "coach"])).toBe("parent or coach");
    expect(rolesText(["player", "parent", "coach"])).toBe("player, parent or coach");
  });
});

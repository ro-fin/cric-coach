import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectNoA11yViolations } from "@/test/axe";
import { Forbidden } from "./Forbidden";

describe("Forbidden", () => {
  it("names the roles the screen serves as an alert", async () => {
    const { container } = render(<Forbidden roles={["parent"]} />);
    expect(screen.getByRole("alert")).toHaveTextContent("This screen is for the parent.");
    await expectNoA11yViolations(container);
  });

  it("adds the server detail verbatim when there is one", () => {
    render(<Forbidden roles={["parent", "coach"]} detail="API 403: requires one of: parent" />);
    expect(screen.getByRole("alert")).toHaveTextContent("API 403: requires one of: parent");
  });
});

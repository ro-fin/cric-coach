import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RoleProvider, useRole } from "./role";
import { isRole, ROLE_LABELS, ROLES } from "./roles";

function ShowRole() {
  return <p>{useRole() ?? "none"}</p>;
}

describe("RoleProvider / useRole", () => {
  it("provides the signed-in role", () => {
    render(
      <RoleProvider role="coach">
        <ShowRole />
      </RoleProvider>,
    );
    expect(screen.getByText("coach")).toBeInTheDocument();
  });

  it("is null when signed out or outside a provider", () => {
    render(
      <>
        <RoleProvider role={null}>
          <ShowRole />
        </RoleProvider>
        <ShowRole />
      </>,
    );
    expect(screen.getAllByText("none")).toHaveLength(2);
  });
});

describe("roles", () => {
  it("has a label for every role", () => {
    expect(ROLES.map((role) => ROLE_LABELS[role])).toEqual(["Player", "Parent", "Coach"]);
  });

  it("recognises only the three wire roles", () => {
    expect(isRole("parent")).toBe(true);
    expect(isRole("coach")).toBe(true);
    expect(isRole("player")).toBe(true);
    expect(isRole("admin")).toBe(false);
    expect(isRole(undefined)).toBe(false);
    expect(isRole(3)).toBe(false);
  });
});

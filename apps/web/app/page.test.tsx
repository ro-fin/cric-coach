import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import HomePage from "./page";
import RootLayout from "./layout";

describe("HomePage", () => {
  it("renders the lab heading and demo ball count", () => {
    render(<HomePage />);
    expect(screen.getByRole("heading", { name: "cricAI" })).toBeInTheDocument();
    expect(screen.getByTestId("demo-count")).toHaveTextContent("500 balls");
  });
});

describe("RootLayout", () => {
  it("wraps children in html/body", () => {
    const markup = RootLayout({ children: "content" });
    expect(markup.props.lang).toBe("en");
  });
});

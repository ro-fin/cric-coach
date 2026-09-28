import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "@/test/axe";
import {
  Badge,
  Button,
  buttonClassName,
  LinkButton,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  DegradedBanner,
  EmptyState,
  ErrorState,
  PageHeader,
  Skeleton,
  StatTile,
} from "./index";

describe("Button", () => {
  it.each(["primary", "secondary", "ghost", "danger"] as const)(
    "renders the %s variant as a real button",
    async (variant) => {
      const onClick = vi.fn();
      const { container } = render(
        <Button variant={variant} onClick={onClick}>
          Save
        </Button>,
      );
      const button = screen.getByRole("button", { name: "Save" });
      expect(button).toHaveAttribute("type", "button");
      expect(button).toHaveAttribute("data-variant", variant);
      expect(button.className).toContain("min-h-11");
      fireEvent.click(button);
      expect(onClick).toHaveBeenCalledOnce();
      await expectNoA11yViolations(container);
    },
  );

  it("uses the large size and passes native props through", () => {
    render(
      <Button variant="primary" size="lg" type="submit" className="w-full" name="go">
        Go
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Go" });
    expect(button).toHaveAttribute("type", "submit");
    expect(button).toHaveAttribute("name", "go");
    expect(button.className).toContain("min-h-14");
    expect(button.className).toContain("w-full");
  });

  it("is busy and disabled while loading, keeping its label", async () => {
    const onClick = vi.fn();
    const { container } = render(
      <Button variant="primary" loading onClick={onClick}>
        Publishing
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Publishing" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
    await expectNoA11yViolations(container);
  });

  it("honours disabled without loading", () => {
    render(
      <Button variant="secondary" disabled>
        Off
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Off" });
    expect(button).toBeDisabled();
    expect(button).not.toHaveAttribute("aria-busy");
  });
});

describe("buttonClassName / LinkButton", () => {
  it("gives links the button look while they stay links", async () => {
    const { container } = render(
      <LinkButton href="/sessions/new" variant="primary" size="lg" className="w-full">
        Start session
      </LinkButton>,
    );
    const link = screen.getByRole("link", { name: "Start session" });
    expect(link).toHaveAttribute("href", "/sessions/new");
    expect(link).toHaveAttribute("data-variant", "primary");
    expect(link.className).toContain("min-h-14");
    expect(link.className).toContain("bg-accent");
    expect(link.className).toContain("w-full");
    await expectNoA11yViolations(container);
  });

  it("defaults to the medium size", () => {
    expect(buttonClassName("secondary")).toContain("min-h-11");
    render(
      <LinkButton href="/" variant="ghost">
        Home
      </LinkButton>,
    );
    expect(screen.getByRole("link", { name: "Home" }).className).toContain("min-h-11");
  });
});

describe("Card", () => {
  it("composes header, title and body", async () => {
    const { container } = render(
      <Card className="extra">
        <CardHeader className="h">
          <CardTitle className="t">Workload</CardTitle>
        </CardHeader>
        <CardBody className="b">42 balls</CardBody>
      </Card>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Workload" })).toHaveClass("t");
    expect(screen.getByText("42 balls")).toHaveClass("b");
    expect(container.firstElementChild).toHaveClass("extra");
    await expectNoA11yViolations(container);
  });

  it("becomes a named region when labelled by its title", async () => {
    const { container } = render(
      <>
        <Card aria-labelledby="wl-title">
          <CardHeader>
            <CardTitle id="wl-title">Workload</CardTitle>
          </CardHeader>
        </Card>
        <Card aria-label="Goal">
          <CardBody>Hit 10 drives</CardBody>
        </Card>
      </>,
    );
    expect(screen.getByRole("region", { name: "Workload" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Goal" })).toHaveTextContent("Hit 10 drives");
    await expectNoA11yViolations(container);
  });

  it("works without extra classes", () => {
    render(
      <Card>
        <CardHeader>
          <CardTitle>Plain</CardTitle>
        </CardHeader>
        <CardBody>body</CardBody>
      </Card>,
    );
    expect(screen.getByText("Plain")).toBeInTheDocument();
  });
});

describe("Badge", () => {
  it.each(["neutral", "success", "warning", "danger", "info"] as const)(
    "renders the %s tone",
    async (tone) => {
      const { container } = render(
        <Badge tone={tone} className="x">
          {tone}
        </Badge>,
      );
      const badge = screen.getByText(tone);
      expect(badge).toHaveAttribute("data-tone", tone);
      expect(badge).toHaveClass("x");
      await expectNoA11yViolations(container);
    },
  );
});

describe("Skeleton", () => {
  it("announces its label and draws three lines by default", async () => {
    const { container } = render(<Skeleton label="Loading sessions" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading sessions");
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    const lines = screen.getAllByTestId("skeleton-line");
    expect(lines).toHaveLength(3);
    expect(lines[2]).toHaveClass("w-2/3");
    expect(lines[0]).toHaveClass("w-full");
    await expectNoA11yViolations(container);
  });

  it("draws a single full-width line", () => {
    render(<Skeleton label="Loading" lines={1} className="c" />);
    const lines = screen.getAllByTestId("skeleton-line");
    expect(lines).toHaveLength(1);
    expect(lines[0]).toHaveClass("w-full");
    expect(screen.getByRole("status")).toHaveClass("c");
  });
});

describe("EmptyState", () => {
  it("shows title, description and the next action", async () => {
    const { container } = render(
      <EmptyState
        title="No sessions yet"
        description="Record a session to see it here."
        action={<button type="button">New session</button>}
      />,
    );
    expect(screen.getByRole("heading", { name: "No sessions yet" })).toBeInTheDocument();
    expect(screen.getByText("Record a session to see it here.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New session" })).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("renders the title alone", () => {
    const { container } = render(<EmptyState title="Nothing here" />);
    expect(container.querySelectorAll("p")).toHaveLength(0);
    expect(container.querySelector(".mt-2")).toBeNull();
  });
});

describe("ErrorState", () => {
  it("alerts the message and retries", async () => {
    const onRetry = vi.fn();
    const { container } = render(<ErrorState message="API 500: boom" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("API 500: boom");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
    await expectNoA11yViolations(container);
  });

  it("omits the retry button without a handler", () => {
    render(<ErrorState message="Forbidden" />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("DegradedBanner", () => {
  it("lists the API reasons verbatim", async () => {
    const reasons = ["missing_views: side_on", "pose confidence below 0.6 for 3 balls"];
    const { container } = render(<DegradedBanner reasons={reasons} />);
    const region = screen.getByRole("region", { name: "Incomplete data" });
    const items = region.querySelectorAll("li");
    expect(Array.from(items, (item) => item.textContent)).toEqual(reasons);
    await expectNoA11yViolations(container);
  });

  it("renders nothing when there is no reason", () => {
    const { container } = render(<DegradedBanner reasons={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("PageHeader", () => {
  it("renders the page heading, description and actions", async () => {
    const { container } = render(
      <PageHeader
        title="Sessions"
        description="Every recorded session"
        actions={<button type="button">New</button>}
      />,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Sessions" })).toBeInTheDocument();
    expect(screen.getByText("Every recorded session")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New" }).parentElement).toHaveAttribute(
      "data-print",
      "hide",
    );
    await expectNoA11yViolations(container);
  });

  it("renders the title alone", () => {
    const { container } = render(<PageHeader title="Only" />);
    expect(container.querySelectorAll("p")).toHaveLength(0);
    expect(container.querySelector('[data-print="hide"]')).toBeNull();
  });
});

describe("StatTile", () => {
  it("shows value, unit and confidence", async () => {
    const { container } = render(
      <StatTile label="Bat speed" value="21.4" unit="m/s" confidence={0.82} />,
    );
    expect(screen.getByText("Bat speed")).toBeInTheDocument();
    expect(screen.getByText("21.4")).toBeInTheDocument();
    expect(screen.getByText("m/s")).toBeInTheDocument();
    expect(screen.getByText("Confidence 82%")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("shows the reason instead of a dash when the value is null", () => {
    render(<StatTile label="Head position" value={null} reason="front camera missing" />);
    expect(screen.getByText("front camera missing")).toBeInTheDocument();
    expect(screen.queryByText("—")).toBeNull();
    expect(screen.queryByText(/Confidence/)).toBeNull();
  });

  it("says not measured when a null value has no reason", () => {
    render(<StatTile label="Head position" value={null} reason={null} />);
    expect(screen.getByText("Not measured")).toBeInTheDocument();
  });

  it("qualifies a present value with its reason", () => {
    render(<StatTile label="Balls" value="12" reason="2 balls excluded" />);
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("2 balls excluded")).toBeInTheDocument();
  });
});

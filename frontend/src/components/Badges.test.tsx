import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CategoryBadge, OutcomeBadge, PassFail, SeverityBadge } from "./Badges";

describe("OutcomeBadge", () => {
  it.each([
    ["VERIFIED", "✓"],
    ["PARTIAL", "◐"],
    ["FAILED", "✗"],
  ] as const)("renders %s as text plus a symbol", (outcome, symbol) => {
    const { container } = render(<OutcomeBadge outcome={outcome} />);
    expect(screen.getByText(outcome)).toBeInTheDocument();
    expect(container.textContent).toContain(symbol);
    expect(screen.getByTitle(`Verification outcome: ${outcome}`)).toBeInTheDocument();
  });

  it("renders a neutral label when there is no outcome yet", () => {
    render(<OutcomeBadge outcome={null} />);
    expect(screen.getByText("NOT VERIFIED")).toBeInTheDocument();
  });

  it("uses distinct visual classes per outcome", () => {
    const cls = (["VERIFIED", "PARTIAL", "FAILED"] as const).map((o) => {
      const { container, unmount } = render(<OutcomeBadge outcome={o} />);
      const c = container.firstElementChild?.className ?? "";
      unmount();
      return c;
    });
    expect(new Set(cls).size).toBe(3);
  });
});

describe("other badges carry text", () => {
  it("PassFail, Severity and Category", () => {
    render(
      <>
        <PassFail passed />
        <PassFail passed={false} />
        <SeverityBadge severity="BLOCKING" />
        <CategoryBadge category="MANUAL_REVIEW" />
      </>,
    );
    expect(screen.getByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("BLOCKING")).toBeInTheDocument();
    expect(screen.getByText("Manual review")).toBeInTheDocument();
  });
});

import { readFileSync } from "node:fs";

import { describe, expect, it, vi } from "vitest";

import {
  applyMobileViewportMetrics,
  createViewportResyncScheduler,
} from "./mobile-viewport";

describe("applyMobileViewportMetrics", () => {
  it("uses the visual viewport while the iOS keyboard changes the visible area", () => {
    const setProperty = vi.fn();

    applyMobileViewportMetrics(
      {
        innerHeight: 844,
        visualViewport: {
          height: 513.4,
          offsetTop: 2.2,
        },
      },
      { setProperty },
    );

    expect(setProperty).toHaveBeenCalledWith(
      "--hermes-viewport-height",
      "513px",
    );
    expect(setProperty).toHaveBeenCalledWith(
      "--hermes-viewport-offset-top",
      "2px",
    );
  });

  it("falls back to the layout viewport when visualViewport is unavailable", () => {
    const setProperty = vi.fn();

    applyMobileViewportMetrics(
      {
        innerHeight: 932,
        visualViewport: null,
      },
      { setProperty },
    );

    expect(setProperty).toHaveBeenCalledWith(
      "--hermes-viewport-height",
      "932px",
    );
    expect(setProperty).toHaveBeenCalledWith(
      "--hermes-viewport-offset-top",
      "0px",
    );
  });
});

describe("createViewportResyncScheduler", () => {
  it("remeasures immediately and after iOS keyboard-dismiss settling delays", () => {
    vi.useFakeTimers();
    const sync = vi.fn();
    const scheduler = createViewportResyncScheduler(sync);

    scheduler.settle();
    expect(sync).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(700);
    expect(sync).toHaveBeenCalledTimes(4);

    scheduler.cancel();
    vi.useRealTimers();
  });

  it("cancels stale delayed measurements before starting a new settle cycle", () => {
    vi.useFakeTimers();
    const sync = vi.fn();
    const scheduler = createViewportResyncScheduler(sync);

    scheduler.settle();
    vi.advanceTimersByTime(100);
    scheduler.settle();
    vi.advanceTimersByTime(700);

    expect(sync).toHaveBeenCalledTimes(5);
    scheduler.cancel();
    vi.useRealTimers();
  });
});

describe("mobile viewport CSS contract", () => {
  it("keeps the document rooted to the measured viewport instead of body scrolling", () => {
    const stylesheet = readFileSync(
      new URL("../index.css", import.meta.url),
      "utf8",
    );

    expect(stylesheet).toContain(
      "height: var(--hermes-viewport-height, 100dvh);",
    );
    expect(stylesheet).toContain("overscroll-behavior: none;");
  });

  it("anchors the fixed mobile chat surface to the iOS visual viewport", () => {
    const stylesheet = readFileSync(
      new URL(
        "../../../plugins/collaboration/dashboard/dist/style.css",
        import.meta.url,
      ),
      "utf8",
    );

    expect(stylesheet).toContain(
      "top: var(--hermes-viewport-offset-top, 0px);",
    );
    expect(stylesheet).toContain("bottom: auto;");
  });
});

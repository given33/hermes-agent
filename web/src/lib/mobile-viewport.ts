export interface MobileViewportSource {
  innerHeight: number;
  visualViewport?: {
    height: number;
    offsetTop: number;
  } | null;
}

export interface CssVariableTarget {
  setProperty(name: string, value: string): void;
}

export interface MobileViewportMetrics {
  height: number;
  offsetTop: number;
  keyboardOpen: boolean;
}

const VIEWPORT_SETTLE_DELAYS_MS = [120, 360, 700] as const;

function roundedPositive(value: number, fallback: number): number {
  const finite = Number.isFinite(value) ? value : fallback;
  return Math.max(1, Math.round(finite));
}

export function applyMobileViewportMetrics(
  source: MobileViewportSource,
  target: CssVariableTarget,
): MobileViewportMetrics {
  const viewport = source.visualViewport;
  const height = roundedPositive(
    viewport?.height ?? source.innerHeight,
    source.innerHeight,
  );
  const offsetTop = Math.max(0, Math.round(viewport?.offsetTop ?? 0));
  const occludedHeight = Math.max(
    0,
    Math.round(source.innerHeight - height - offsetTop),
  );
  const keyboardOpen = Boolean(viewport && occludedHeight >= 120);

  target.setProperty("--hermes-viewport-height", `${height}px`);
  target.setProperty("--hermes-viewport-offset-top", `${offsetTop}px`);
  return { height, offsetTop, keyboardOpen };
}

export function createViewportResyncScheduler(sync: () => void) {
  let pending: Array<ReturnType<typeof setTimeout>> = [];

  const cancel = () => {
    pending.forEach((timer) => clearTimeout(timer));
    pending = [];
  };

  const settle = () => {
    cancel();
    sync();
    pending = VIEWPORT_SETTLE_DELAYS_MS.map((delay) =>
      setTimeout(sync, delay),
    );
  };

  return {
    cancel,
    settle,
    syncNow: sync,
  };
}

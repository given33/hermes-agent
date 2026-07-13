import { useEffect } from "react";

import {
  applyMobileViewportMetrics,
  createViewportResyncScheduler,
} from "@/lib/mobile-viewport";

export function MobileViewportCompat() {
  useEffect(() => {
    let layoutHeight = Math.max(
      window.innerHeight,
      window.visualViewport?.height ?? 0,
    );
    const sync = () => {
      layoutHeight = Math.max(
        layoutHeight,
        window.innerHeight,
        window.visualViewport?.height ?? 0,
      );
      const metrics = applyMobileViewportMetrics(
        {
          innerHeight: layoutHeight,
          visualViewport: window.visualViewport,
        },
        document.documentElement.style,
      );
      document.documentElement.dataset.hermesKeyboard = metrics.keyboardOpen
        ? "open"
        : "closed";
    };
    const scheduler = createViewportResyncScheduler(sync);
    const settle = () => scheduler.settle();
    const syncWhenVisible = () => {
      if (document.visibilityState === "visible") settle();
    };
    const viewport = window.visualViewport;

    scheduler.settle();
    window.addEventListener("resize", settle);
    const handleOrientationChange = () => {
      layoutHeight = Math.max(
        window.innerHeight,
        window.visualViewport?.height ?? 0,
      );
      settle();
    };
    window.addEventListener("orientationchange", handleOrientationChange);
    window.addEventListener("pageshow", settle);
    document.addEventListener("focusin", settle);
    document.addEventListener("focusout", settle);
    document.addEventListener("visibilitychange", syncWhenVisible);
    viewport?.addEventListener("resize", settle);
    viewport?.addEventListener("scroll", scheduler.syncNow);

    return () => {
      scheduler.cancel();
      window.removeEventListener("resize", settle);
      window.removeEventListener("orientationchange", handleOrientationChange);
      window.removeEventListener("pageshow", settle);
      document.removeEventListener("focusin", settle);
      document.removeEventListener("focusout", settle);
      document.removeEventListener("visibilitychange", syncWhenVisible);
      viewport?.removeEventListener("resize", settle);
      viewport?.removeEventListener("scroll", scheduler.syncNow);
      delete document.documentElement.dataset.hermesKeyboard;
    };
  }, []);

  return null;
}

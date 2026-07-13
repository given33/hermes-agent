import { useEffect } from "react";

import {
  applyMobileViewportMetrics,
  createViewportResyncScheduler,
} from "@/lib/mobile-viewport";

export function MobileViewportCompat() {
  useEffect(() => {
    const sync = () => {
      applyMobileViewportMetrics(window, document.documentElement.style);
    };
    const scheduler = createViewportResyncScheduler(sync);
    const settle = () => scheduler.settle();
    const syncWhenVisible = () => {
      if (document.visibilityState === "visible") settle();
    };
    const viewport = window.visualViewport;

    scheduler.settle();
    window.addEventListener("resize", settle);
    window.addEventListener("orientationchange", settle);
    window.addEventListener("pageshow", settle);
    document.addEventListener("focusin", settle);
    document.addEventListener("focusout", settle);
    document.addEventListener("visibilitychange", syncWhenVisible);
    viewport?.addEventListener("resize", settle);
    viewport?.addEventListener("scroll", scheduler.syncNow);

    return () => {
      scheduler.cancel();
      window.removeEventListener("resize", settle);
      window.removeEventListener("orientationchange", settle);
      window.removeEventListener("pageshow", settle);
      document.removeEventListener("focusin", settle);
      document.removeEventListener("focusout", settle);
      document.removeEventListener("visibilitychange", syncWhenVisible);
      viewport?.removeEventListener("resize", settle);
      viewport?.removeEventListener("scroll", scheduler.syncNow);
    };
  }, []);

  return null;
}

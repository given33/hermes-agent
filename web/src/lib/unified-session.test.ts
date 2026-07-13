import { describe, expect, it } from "vitest";

import {
  PENDING_UNIFIED_SESSION_KEY,
  queueUnifiedSessionResume,
} from "./unified-session";

describe("queueUnifiedSessionResume", () => {
  it("persists the stored session id across navigation", () => {
    const values = new Map<string, string>();
    const storage = {
      setItem(key: string, value: string) {
        values.set(key, value);
      },
    };

    expect(queueUnifiedSessionResume(storage, " stored-session-1 ")).toBe(true);
    expect(values.get(PENDING_UNIFIED_SESSION_KEY)).toBe("stored-session-1");
  });

  it("ignores an empty session id", () => {
    let called = false;
    const storage = {
      setItem() {
        called = true;
      },
    };

    expect(queueUnifiedSessionResume(storage, "  ")).toBe(false);
    expect(called).toBe(false);
  });
});

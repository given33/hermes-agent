import { describe, expect, it } from "vitest";

import { isoTimeAgo, timeAgo } from "./utils";

describe("relative time localization", () => {
  it("formats recent Unix timestamps in Simplified Chinese", () => {
    const nowSeconds = Date.now() / 1000;

    expect(timeAgo(nowSeconds - 30, "zh")).toBe("刚刚");
    expect(timeAgo(nowSeconds - 120, "zh")).toBe("2分钟前");
    expect(timeAgo(nowSeconds - 7200, "zh")).toBe("2小时前");
  });

  it("formats ISO timestamps using the requested locale", () => {
    const twoDaysAgo = new Date(Date.now() - 2 * 86400 * 1000).toISOString();

    expect(isoTimeAgo(twoDaysAgo, "zh")).toBe("2天前");
  });
});

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

interface WebManifest {
  icons?: Array<{ src?: string }>;
  scope?: string;
  start_url?: string;
}

describe("PWA manifest", () => {
  it("keeps navigation and icons inside a reverse-proxy base path", () => {
    const manifest = JSON.parse(
      readFileSync(new URL("../../public/manifest.webmanifest", import.meta.url), "utf8"),
    ) as WebManifest;

    expect(manifest.start_url).toBe("./chat");
    expect(manifest.scope).toBe("./");
    expect(manifest.icons?.[0]?.src).toBe("./apple-touch-icon.png");
  });
});

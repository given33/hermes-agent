import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
const pluginLoaderSource = readFileSync(
  resolve(process.cwd(), "src/plugins/usePlugins.ts"),
  "utf8",
);

describe("dashboard startup performance", () => {
  it("loads non-chat management pages on demand", () => {
    expect(appSource).toContain('lazy(() => import("@/pages/ModelsPage"))');
    expect(appSource).toContain('lazy(() => import("@/pages/SkillsPage"))');
    expect(appSource).toContain('lazy(() => import("@/pages/AnalyticsPage"))');
    expect(appSource).not.toContain('import ModelsPage from "@/pages/ModelsPage"');
    expect(appSource).not.toContain('import SkillsPage from "@/pages/SkillsPage"');
    expect(appSource).not.toContain('import AnalyticsPage from "@/pages/AnalyticsPage"');
  });

  it("renders lazy route chunks inside a suspense boundary", () => {
    expect(appSource).toMatch(/<Suspense[\s\S]*<Routes>/);
  });

  it("loads only slot and active-route plugins during the first paint", () => {
    expect(pluginLoaderSource).toContain("manifest.slots.length > 0");
    expect(pluginLoaderSource).toContain("normalizedPath");
    expect(pluginLoaderSource).toContain("normalizePluginPath(manifest.tab.path)");
    expect(pluginLoaderSource).toContain("requiredManifests");
    expect(pluginLoaderSource).not.toContain("setLoading(true)");
  });
});

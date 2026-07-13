import { describe, expect, it } from "vitest";

import {
  localizedPluginDescription,
  localizedSkillDescription,
  localizedToolsetDescription,
  localizedToolsetLabel,
} from "./localized-metadata";

describe("localizedSkillDescription", () => {
  it("preserves the source description outside Chinese locales", () => {
    expect(
      localizedSkillDescription(
        { name: "codex", description: "Run coding tasks with Codex." },
        "en",
      ),
    ).toBe("Run coding tasks with Codex.");
  });

  it("preserves descriptions that are already Chinese", () => {
    expect(
      localizedSkillDescription(
        { name: "research", description: "用于检索和整理研究资料。" },
        "zh",
      ),
    ).toBe("用于检索和整理研究资料。");
  });

  it("summarizes English skill metadata by capability", () => {
    expect(
      localizedSkillDescription(
        {
          name: "codex",
          description: "Delegate software development tasks to Codex CLI.",
          category: "software-development",
        },
        "zh",
      ),
    ).toBe("codex 技能，用于软件开发与代码处理；Hermes 可在相关任务中调用。");

    expect(
      localizedSkillDescription(
        {
          name: "pptx",
          description: "Create and edit presentation files.",
          tags: ["slides", "documents"],
        },
        "zh-hant",
      ),
    ).toBe("pptx 技能，用于演示文稿与办公文件处理；Hermes 可在相关任务中调用。");
  });

  it("does not mistake English metadata with a few Chinese words for a Chinese description", () => {
    expect(
      localizedSkillDescription(
        {
          name: "baoyu-infographic",
          description: "Infographics: 21 layouts x 21 styles (信息图, 可视化).",
          category: "creative",
        },
        "zh",
      ),
    ).toBe("baoyu-infographic 技能，用于相关专业任务；Hermes 可在相关任务中调用。");

    expect(
      localizedSkillDescription(
        {
          name: "powerpoint",
          description: "Create polished presentation decks.",
        },
        "zh",
      ),
    ).toBe("powerpoint 技能，用于演示文稿与办公文件处理；Hermes 可在相关任务中调用。");
  });
});

describe("localizedPluginDescription", () => {
  it.each([
    ["feishu-platform", "飞书消息渠道接入与收发扩展。"],
    ["openrouter-provider", "OpenRouter 模型提供商接入扩展。"],
    ["hindsight-memory", "Hindsight 持久记忆提供商扩展。"],
    ["browser-tools", "浏览器自动化与网页操作扩展。"],
    ["langfuse-observability", "运行追踪、分析与可观测性扩展。"],
    ["custom-addon", "custom-addon Hermes 系统能力扩展。"],
  ])("summarizes %s in Chinese", (name, expected) => {
    expect(
      localizedPluginDescription(
        { name, description: "An English plugin description." },
        "zh",
      ),
    ).toBe(expected);
  });

  it("uses the plugin label when it is more readable than its package name", () => {
    expect(
      localizedPluginDescription(
        {
          name: "platforms/discord",
          label: "Discord",
          description: "Connect Hermes to Discord.",
        },
        "zh",
      ),
    ).toBe("Discord 消息渠道接入与收发扩展。");

    expect(
      localizedPluginDescription(
        {
          name: "achievements",
          label: "成就",
          description: "Track agent achievements.",
        },
        "zh",
      ),
    ).toBe("成就 Hermes 系统能力扩展。");
  });
});

describe("toolset localization", () => {
  it("localizes user-facing toolset labels and summaries", () => {
    expect(localizedToolsetLabel("Web Search & Scraping", "zh")).toBe(
      "网页搜索与采集",
    );
    expect(localizedToolsetLabel("Computer Use (macOS/Windows/Linux)", "zh")).toBe(
      "电脑操作（macOS/Windows/Linux）",
    );
    expect(
      localizedToolsetDescription(
        "create/list/update/pause/resume/run, with optional attached skills",
        "zh",
      ),
    ).toBe("创建、查看、更新、暂停、恢复和运行定时任务，可附加技能");
  });

  it("does not change toolset metadata in English", () => {
    expect(localizedToolsetLabel("Task Delegation", "en")).toBe("Task Delegation");
    expect(localizedToolsetDescription("delegate_task", "en")).toBe("delegate_task");
  });
});

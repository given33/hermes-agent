import { describe, expect, it } from "vitest";

import { translateDashboardText } from "./dashboard-zh-compat";

describe("translateDashboardText", () => {
  it("translates hard-coded dashboard controls", () => {
    expect(translateDashboardText("Files")).toBe("文件");
    expect(translateDashboardText("MODEL SETTINGS")).toBe("模型设置");
    expect(translateDashboardText("NEW SUBSCRIPTION")).toBe("新建订阅");
    expect(translateDashboardText("No pending pairing requests")).toBe(
      "暂无待处理的配对请求",
    );
    expect(translateDashboardText("No MCP servers configured.")).toBe(
      "尚未配置 MCP 服务器。",
    );
  });

  it("translates dynamic labels without changing their values", () => {
    expect(translateDashboardText("Subscriptions (12)")).toBe("订阅（12）");
    expect(translateDashboardText("Open .hermes")).toBe("打开 .hermes");
    expect(translateDashboardText("Delete .bashrc")).toBe("删除 .bashrc");
    expect(translateDashboardText("Active profile: default")).toBe(
      "当前配置：default",
    );
    expect(translateDashboardText("6 cores · 77%")).toBe("6 核心 · 77%");
  });

  it("keeps technical identifiers unchanged", () => {
    expect(translateDashboardText("grok-4.5")).toBe("grok-4.5");
    expect(translateDashboardText("/home/hermes")).toBe("/home/hermes");
    expect(translateDashboardText("AGENT.LOG")).toBe("AGENT.LOG");
    expect(translateDashboardText("EXA_API_KEY")).toBe("EXA_API_KEY");
  });

  it("translates title-case controls that remain in official pages", () => {
    expect(translateDashboardText("Go")).toBe("前往");
    expect(translateDashboardText("Upload")).toBe("上传");
    expect(translateDashboardText("Rename session")).toBe("重命名会话");
    expect(translateDashboardText("Model Settings")).toBe("模型设置");
    expect(translateDashboardText("Configure")).toBe("配置");
    expect(translateDashboardText("Browse hub")).toBe("浏览技能中心");
    expect(translateDashboardText("Install")).toBe("安装");
    expect(translateDashboardText("New hook")).toBe("新建钩子");
    expect(translateDashboardText("(unset)")).toBe("（未设置）");
    expect(translateDashboardText("not loaded")).toBe("未加载");
    expect(translateDashboardText("inference provider:")).toBe("推理提供商：");
    expect(translateDashboardText("every 168h")).toBe("每 168 小时");
    expect(translateDashboardText("· last run 2026/7/10")).toBe(
      "· 上次运行 2026/7/10",
    );
    expect(translateDashboardText("External provider:")).toBe("外部提供商：");
    expect(translateDashboardText("built-in only")).toBe("仅使用内置提供商");
    expect(translateDashboardText("session(s) ·")).toBe("个会话 ·");
    expect(translateDashboardText("Log in with")).toBe("请登录");
  });

  it("translates split dynamic labels rendered around React expressions", () => {
    expect(translateDashboardText("Your MCP servers (")).toBe(
      "你的 MCP 服务器（",
    );
    expect(translateDashboardText("Pending requests (")).toBe("待处理请求（");
    expect(translateDashboardText("cores · 22%")).toBe("核心 · 22%");
    expect(translateDashboardText("h · last run")).toBe("小时 · 上次运行");
    expect(translateDashboardText("11 tasks · all auto")).toBe(
      "11 项任务 · 全部自动",
    );
    expect(translateDashboardText("2 references · openrouter/model")).toBe(
      "2 个参考模型 · openrouter/model",
    );
  });

  it("translates accessibility labels while preserving channel names and keys", () => {
    expect(translateDashboardText("Enable Feishu / Lark")).toBe(
      "启用 Feishu / Lark",
    );
    expect(translateDashboardText("Edit codex")).toBe("编辑 codex");
    expect(translateDashboardText("Reveal SHURANIMA_API_KEY")).toBe(
      "显示 SHURANIMA_API_KEY",
    );
    expect(translateDashboardText("Open Anthropic docs")).toBe(
      "打开 Anthropic 文档",
    );
  });

  it("translates system, config, and provider-management labels", () => {
    expect(translateDashboardText("Tool Gateway routing")).toBe(
      "工具网关路由",
    );
    expect(translateDashboardText("External provider: built-in only")).toBe(
      "外部提供商：仅使用内置提供商",
    );
    expect(translateDashboardText("Model Context Length")).toBe(
      "模型上下文长度",
    );
    expect(translateDashboardText("Save memory provider")).toBe(
      "保存记忆提供商",
    );
    expect(translateDashboardText("Configure memory providers and runtime context engine selection.")).toBe(
      "配置记忆提供商和运行时上下文引擎。",
    );
    expect(
      translateDashboardText(
        "1 of 31 channels configured.\n Credentials are written to",
      ),
    ).toBe("已配置 1/31 个消息渠道。凭据写入");
    expect(translateDashboardText("of")).toBe("/");
    expect(
      translateDashboardText("channels configured. Credentials are written to"),
    ).toBe("个消息渠道已配置。凭据写入");
    expect(
      translateDashboardText(
        "; the gateway connects each enabled channel on its next restart.",
      ),
    ).toBe("；网关会在下次重启时连接每个已启用的渠道。");
    expect(translateDashboardText("Bootstrap commands (")).toBe(
      "初始化命令（",
    );
    expect(translateDashboardText(")", "目录（3)")).toBe("）");
    expect(
      translateDashboardText(
        "Token & cost analytics are hidden because the local counts exclude auxiliary calls\n(compression, vision, web extract, …) and provider retries, so they diverge from your provider bill. Enable dashboard.show_token_analytics in",
      ),
    ).toBe(
      "Token 与费用分析已隐藏，因为本地统计不包含压缩、视觉、网页提取等辅助调用及提供商重试，与实际账单可能不同。请在",
    );
    expect(
      translateDashboardText(
        "Token & cost analytics are hidden because the local counts exclude auxiliary calls (compression, vision, web extract, …) and provider retries, so they diverge from your provider bill. Enable",
      ),
    ).toBe(
      "Token 与费用分析已隐藏，因为本地统计不包含压缩、视觉、网页提取等辅助调用及提供商重试，与实际账单可能不同。请启用",
    );
    expect(
      translateDashboardText(
        "in",
        "请启用 dashboard.show_token_analytics in 配置",
      ),
    ).toBe("，位置：");
    expect(
      translateDashboardText(
        "to show the local debug estimate anyway.",
      ),
    ).toBe("即可显示本地调试估算。");
  });

  it("translates the complete skills hub and plugin setup surface", () => {
    expect(translateDashboardText("Search the skill hub (GitHub, official, community)…")).toBe(
      "搜索技能中心（GitHub、官方、社区）…",
    );
    expect(translateDashboardText("Update all")).toBe("全部更新");
    expect(translateDashboardText("Featured skills")).toBe("精选技能");
    expect(translateDashboardText("Details")).toBe("详情");
    expect(translateDashboardText("Installed")).toBe("已安装");
    expect(translateDashboardText("Security scan")).toBe("安全扫描");
    expect(translateDashboardText("Setup results")).toBe("设置结果");
    expect(translateDashboardText("Python dependencies")).toBe("Python 依赖项");
    expect(translateDashboardText("needs setup")).toBe("需要设置");
    expect(translateDashboardText("enabled")).toBe("已启用");
    expect(translateDashboardText("bundled")).toBe("内置");
    expect(translateDashboardText("user")).toBe("用户安装");
    expect(translateDashboardText("Copy")).toBe("复制");
    expect(translateDashboardText("3 results")).toBe("3 个结果");
    expect(translateDashboardText("github, official timed out")).toBe(
      "github, official 响应超时",
    );
    expect(translateDashboardText("Open pptx")).toBe("打开 pptx");
  });
});

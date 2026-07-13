interface SkillMetadata {
  name: string;
  description?: string | null;
  category?: string | null;
  tags?: string[] | null;
}

interface PluginMetadata {
  name: string;
  label?: string | null;
  description?: string | null;
}

const HAN_RE = /[\u3400-\u9fff]/;

function isChineseLocale(locale: string): boolean {
  return locale.toLowerCase().startsWith("zh");
}

function hasChineseText(value: string | null | undefined): boolean {
  const text = value?.trim() ?? "";
  if (!HAN_RE.test(text)) return false;
  if (/^[\u3400-\u9fff]/.test(text)) return true;

  const hanCount = (text.match(/[\u3400-\u9fff]/g) ?? []).length;
  const latinCount = (text.match(/[A-Za-z]/g) ?? []).length;
  return hanCount >= 4 && hanCount * 2 >= latinCount;
}

function skillCapability(metadata: SkillMetadata): string {
  const value = [
    metadata.name,
    metadata.category,
    ...(metadata.tags ?? []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  if (/pptx?|powerpoint|slides?|presentation|keynote/.test(value)) {
    return "演示文稿与办公文件处理";
  }
  if (/docx?|xlsx?|spreadsheet|document|pdf|office/.test(value)) {
    return "文档与办公文件处理";
  }
  if (/codex|coding|code|developer|software|github|git\b/.test(value)) {
    return "软件开发与代码处理";
  }
  if (/browser|research|search|crawl|scrape|web\b/.test(value)) {
    return "网页浏览、检索与资料整理";
  }
  if (/image|vision|photo|design|canvas/.test(value)) {
    return "图像生成与视觉处理";
  }
  if (/video|audio|speech|media/.test(value)) {
    return "音视频与多媒体处理";
  }
  if (/mcp|integration|connector/.test(value)) {
    return "MCP 与外部工具集成";
  }
  if (/security|audit|red.team/.test(value)) {
    return "安全检查与分析";
  }
  if (/automation|workflow|cron|schedule/.test(value)) {
    return "自动化工作流";
  }
  return "相关专业任务";
}

export function localizedSkillDescription(
  metadata: SkillMetadata,
  locale: string,
): string {
  const source = metadata.description?.trim() ?? "";
  if (!isChineseLocale(locale) || hasChineseText(source)) return source;

  return `${metadata.name} 技能，用于${skillCapability(metadata)}；Hermes 可在相关任务中调用。`;
}

function pluginDisplayName(metadata: PluginMetadata): string {
  if (metadata.label?.trim()) return metadata.label.trim();

  const value = metadata.name.toLowerCase();
  if (value.includes("feishu") || value.includes("lark")) return "飞书";
  if (value.includes("openrouter")) return "OpenRouter";
  if (value.includes("hindsight")) return "Hindsight";
  if (value.includes("discord")) return "Discord";
  if (value.includes("telegram")) return "Telegram";
  if (value.includes("whatsapp")) return "WhatsApp";
  if (value.includes("slack")) return "Slack";
  return metadata.name;
}

function pluginSummary(metadata: PluginMetadata): string {
  const value = `${metadata.name} ${metadata.label ?? ""}`.toLowerCase();
  const displayName = pluginDisplayName(metadata);
  const named = (suffix: string) =>
    `${displayName}${HAN_RE.test(displayName.slice(-1)) && HAN_RE.test(suffix[0]) ? "" : " "}${suffix}`;

  if (/feishu|lark|discord|telegram|whatsapp|slack|wechat|wecom|platform|channel|messag/.test(value)) {
    return named("消息渠道接入与收发扩展。");
  }
  if (/memory|hindsight|honcho|mem0|retaindb|supermemory|openviking/.test(value)) {
    return named("持久记忆提供商扩展。");
  }
  if (/browser|playwright|cua/.test(value)) {
    return "浏览器自动化与网页操作扩展。";
  }
  if (/image|video|fal|krea/.test(value)) {
    return "图像与视频生成扩展。";
  }
  if (/observability|langfuse|analytics|telemetry|trace/.test(value)) {
    return "运行追踪、分析与可观测性扩展。";
  }
  if (/auth|oauth|identity/.test(value)) {
    return named("身份验证扩展。");
  }
  if (/provider|model|inference|openrouter/.test(value)) {
    return named("模型提供商接入扩展。");
  }
  if (/kanban|collaboration|workflow|task/.test(value)) {
    return "任务协作、执行跟踪与工作流扩展。";
  }
  return named("Hermes 系统能力扩展。");
}

export function localizedPluginDescription(
  metadata: PluginMetadata,
  locale: string,
): string {
  const source = metadata.description?.trim() ?? "";
  if (!isChineseLocale(locale) || hasChineseText(source)) return source;
  return pluginSummary(metadata);
}

const TOOLSET_LABELS_ZH: Record<string, string> = {
  "Web Search & Scraping": "网页搜索与采集",
  "Browser Automation": "浏览器自动化",
  "Terminal & Processes": "终端与进程",
  "File Operations": "文件操作",
  "Code Execution": "代码执行",
  "Vision / Image Analysis": "视觉与图像分析",
  "Video Analysis": "视频分析",
  "Image Generation": "图像生成",
  "Video Generation": "视频生成",
  "X (Twitter) Search": "X（Twitter）搜索",
  "Text-to-Speech": "文字转语音",
  Skills: "技能管理",
  "Task Planning": "任务规划",
  Memory: "记忆",
  "Context Engine": "上下文引擎",
  "Session Search": "会话搜索",
  "Clarifying Questions": "澄清问题",
  "Task Delegation": "任务委派",
  "Cron Jobs": "定时任务",
  "Home Assistant": "Home Assistant",
  Spotify: "Spotify",
  "Discord (read/participate)": "Discord（读取/参与）",
  "Discord Server Admin": "Discord 服务器管理",
  Yuanbao: "元宝",
  "Computer Use (macOS/Windows/Linux)": "电脑操作（macOS/Windows/Linux）",
};

const TOOLSET_DESCRIPTIONS_ZH: Record<string, string> = {
  "web_search, web_extract": "搜索网页并提取内容",
  "navigate, click, type, scroll": "导航、点击、输入和滚动网页",
  "terminal, process": "运行终端命令并管理进程",
  "read, write, patch, search": "读取、写入、修改和搜索文件",
  execute_code: "执行代码",
  vision_analyze: "分析图像内容",
  "video_analyze (requires video-capable model)": "分析视频内容（需要支持视频的模型）",
  image_generate: "生成图像",
  "video_generate (text/image/reference)": "根据文本、图像或参考素材生成视频",
  "x_search (requires xAI OAuth or XAI_API_KEY)":
    "搜索 X（需要 xAI OAuth 或 XAI_API_KEY）",
  text_to_speech: "将文字转换为语音",
  "list, view, manage": "列出、查看和管理技能",
  todo: "规划并跟踪任务步骤",
  "persistent memory across sessions": "跨会话持久记忆",
  "runtime tools from the active context engine": "使用当前上下文引擎提供的运行时工具",
  "search past conversations": "搜索历史会话",
  clarify: "向用户提出澄清问题",
  delegate_task: "拆分并委派任务",
  "create/list/update/pause/resume/run, with optional attached skills":
    "创建、查看、更新、暂停、恢复和运行定时任务，可附加技能",
  "smart home device control": "控制智能家居设备",
  "playback, search, playlists, library": "播放控制、搜索、播放列表和音乐库",
  "fetch messages, search members, create thread": "获取消息、搜索成员并创建话题",
  "list channels/roles, pin, assign roles": "列出频道和角色、置顶消息并分配角色",
  "group info, member queries, DM": "查询群组和成员信息并发送私信",
  "background desktop control via cua-driver": "通过 cua-driver 在后台控制桌面",
};

export function localizedToolsetLabel(label: string, locale: string): string {
  if (!isChineseLocale(locale)) return label;
  return TOOLSET_LABELS_ZH[label] ?? label;
}

export function localizedToolsetDescription(
  description: string,
  locale: string,
): string {
  if (!isChineseLocale(locale) || hasChineseText(description)) return description;
  return TOOLSET_DESCRIPTIONS_ZH[description] ?? description;
}

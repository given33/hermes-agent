const EXACT_TRANSLATIONS: Record<string, string> = {
  Files: "文件",
  Channels: "消息渠道",
  Webhooks: "网络钩子",
  Pairing: "设备配对",
  System: "系统监控",
  Mcp: "MCP",
  Total: "总计",
  "Active in store": "存储中的活跃会话",
  Archived: "已归档",
  Messages: "消息",
  "Prune old sessions": "清理旧会话",
  "Prune old sessions?": "清理旧会话？",
  Prune: "清理",
  "Older than (days)": "早于指定天数",
  "Permanently remove archived sessions whose last activity is older than the given number of days. Active sessions are never pruned.":
    "永久删除最后活动时间早于指定天数的已归档会话。活跃会话不会被清理。",

  Path: "路径",
  GO: "前往",
  UPLOAD: "上传",
  CREATE: "新建",
  "DROP FILES HERE": "将文件拖放到这里",
  "CHOOSE FILES": "选择文件",
  NAME: "名称",
  SIZE: "大小",
  MODIFIED: "修改时间",
  ACTIONS: "操作",
  "Create folder": "新建文件夹",
  "Upload files": "上传文件",
  "Folder name": "文件夹名称",
  Go: "前往",
  Upload: "上传",
  Create: "新建",
  "Drop files here": "将文件拖放到这里",
  "Choose files": "选择文件",
  Name: "名称",
  Size: "大小",
  Modified: "修改时间",
  Actions: "操作",
  "No files": "暂无文件",
  "(unset)": "（未设置）",
  "not loaded": "未加载",
  "Rename session": "重命名会话",
  "Export session": "导出会话",
  "Export session JSON": "导出会话 JSON",

  "MODEL SETTINGS": "模型设置",
  "Model Settings": "模型设置",
  "applies to new sessions": "应用于新会话",
  "MAIN MODEL": "主模型",
  "Main model": "主模型",
  "AUXILIARY TASKS": "辅助任务",
  "Auxiliary tasks": "辅助任务",
  "MIXTURE OF AGENTS": "多智能体混合",
  "Mixture of Agents": "多智能体混合",
  CHANGE: "更改",
  Change: "更改",
  CONFIGURE: "配置",
  Configure: "配置",
  "USE AS": "设为",
  "Use as": "设为",
  "All auxiliary tasks": "所有辅助任务",
  "Configure Mixture of Agents presets": "配置多智能体混合预设",
  "Set default": "设为默认",
  Delete: "删除",
  "Add preset": "添加预设",
  "Reference models": "参考模型",
  Remove: "移除",
  "Add reference model": "添加参考模型",
  Aggregator: "聚合模型",
  Cancel: "取消",
  Config: "配置",
  "Token & cost analytics are hidden because the local counts exclude auxiliary calls (compression, vision, web extract ...) and provider retries, so they diverge from your provider bill. Enable dashboard.show_token_analytics in Config to show the local debug estimate anyway.":
    "Token 与费用分析已隐藏，因为本地统计不包含压缩、视觉、网页提取等辅助调用及供应商重试，与实际账单可能不同。可在配置中启用 dashboard.show_token_analytics 查看本地调试估算。",

  "Token & cost analytics are hidden because the local counts exclude auxiliary calls (compression, vision, web extract, …) and provider retries, so they diverge from your provider bill. Enable dashboard.show_token_analytics in":
    "Token 与费用分析已隐藏，因为本地统计不包含压缩、视觉、网页提取等辅助调用及提供商重试，与实际账单可能不同。请在",
  "Token & cost analytics are hidden because the local counts exclude auxiliary calls (compression, vision, web extract, …) and provider retries, so they diverge from your provider bill. Enable":
    "Token 与费用分析已隐藏，因为本地统计不包含压缩、视觉、网页提取等辅助调用及提供商重试，与实际账单可能不同。请启用",
  "to show the local debug estimate anyway.": "即可显示本地调试估算。",

  AGENT: "代理",
  ERRORS: "错误日志",
  GATEWAY: "网关",
  ALL: "全部",
  DEBUG: "调试",
  INFO: "信息",
  WARNING: "警告",
  ERROR: "错误",
  TOOLS: "工具",
  CRON: "定时任务",

  "Your MCP servers": "你的 MCP 服务器",
  "Your MCP servers (": "你的 MCP 服务器（",
  "No MCP servers configured.": "尚未配置 MCP 服务器。",
  Catalog: "目录",
  "Catalog (": "目录（",
  "Browse Nous-approved MCP servers and install them with one click.":
    "浏览 Nous 审核的 MCP 服务器，并可一键安装。",
  "Setup notes": "设置说明",
  INSTALL: "安装",
  Install: "安装",
  "Installing...": "安装中...",
  "source ↗": "来源 ↗",
  "Endpoint:": "端点：",
  "Runs:": "运行命令：",
  "Installs from:": "安装来源：",
  "Bootstrap commands (": "初始化命令（",
  "Find, create, and update Linear issues, projects, and comments.":
    "查找、创建和更新 Linear 议题、项目与评论。",
  "Manage and inspect n8n workflows from Hermes (stdio bridge, no public port).":
    "通过 Hermes 管理和检查 n8n 工作流（stdio 桥接，无需开放端口）。",
  "Drive the Unreal Engine 5.8 editor over its local MCP server.":
    "通过本地 MCP 服务器控制 Unreal Engine 5.8 编辑器。",

  "channels configured": "个渠道已配置",
  of: "/",
  "channels configured. Credentials are written to":
    "个消息渠道已配置。凭据写入",
  Connected: "已连接",
  Disabled: "已禁用",
  Test: "测试",
  Mode: "模式",
  "Allowed WhatsApp numbers": "允许的 WhatsApp 号码",
  "Set up with QR": "使用二维码设置",
  "Pair with QR": "使用二维码配对",
  "; the gateway connects each enabled channel on its next restart.":
    "；网关会在下次重启时连接每个已启用的渠道。",
  "SET UP WITH QR": "使用二维码设置",
  "PAIR WITH QR": "使用二维码配对",
  MODE: "模式",
  Bot: "机器人",
  "Self-chat": "自聊",
  "ALLOWED WHATSAPP NUMBERS": "允许的 WhatsApp 号码",
  "Run Hermes from Telegram DMs, groups, and topics.":
    "通过 Telegram 私聊、群组和话题使用 Hermes。",
  "Connect Hermes to Discord DMs, channels, and threads.":
    "将 Hermes 连接到 Discord 私聊、频道和线程。",
  "Use Hermes from Slack via Socket Mode. Add allowed Slack member IDs so connected bots can respond.":
    "通过 Socket Mode 在 Slack 中使用 Hermes，并添加允许响应的 Slack 成员 ID。",
  "Connect Hermes to Mattermost channels and direct messages.":
    "将 Hermes 连接到 Mattermost 频道和私聊。",
  "Use Hermes in Matrix rooms and direct messages.":
    "在 Matrix 房间和私聊中使用 Hermes。",
  "Use Hermes through the bundled WhatsApp bridge with QR-based auth.":
    "通过内置 WhatsApp 桥接和二维码认证使用 Hermes。",
  "Connect through a signal-cli REST bridge.":
    "通过 signal-cli REST 桥接连接。",
  "Use Hermes through iMessage via a BlueBubbles server.":
    "通过 BlueBubbles 服务器在 iMessage 中使用 Hermes。",
  "Control your smart home from Hermes via Home Assistant.":
    "通过 Home Assistant 使用 Hermes 控制智能家居。",
  "Talk to Hermes through an IMAP/SMTP mailbox.":
    "通过 IMAP/SMTP 邮箱与 Hermes 对话。",
  "Send and receive text messages via Twilio.":
    "通过 Twilio 收发短信。",
  "Connect Hermes to DingTalk groups (钉钉).":
    "将 Hermes 连接到钉钉群组。",
  "Use Hermes inside Feishu / Lark.": "在飞书 / Lark 中使用 Hermes。",
  "Connect Hermes to Google Chat via Cloud Pub/Sub.":
    "通过 Cloud Pub/Sub 将 Hermes 连接到 Google Chat。",
  "Send-only WeCom group bot via webhook.":
    "通过 Webhook 使用仅发送的企业微信群机器人。",
  "Two-way WeCom integration via callback app.":
    "通过回调应用实现双向企业微信集成。",
  "Connect a personal WeChat account through Tencent's iLink Bot API.":
    "通过腾讯 iLink Bot API 连接个人微信账号。",
  "Connect Hermes to a QQ Bot from the QQ Open Platform.":
    "将 Hermes 连接到 QQ 开放平台机器人。",
  "Connect Hermes to Tencent Yuanbao.": "将 Hermes 连接到腾讯元宝。",
  "Expose Hermes as an OpenAI-compatible HTTP API for tools like Open WebUI.":
    "将 Hermes 暴露为兼容 OpenAI 的 HTTP API，供 Open WebUI 等工具使用。",
  "Receive events from GitHub, GitLab, and other webhook sources.":
    "接收来自 GitHub、GitLab 和其他 Webhook 来源的事件。",

  "NEW SUBSCRIPTION": "新建订阅",
  "Subscriptions (": "订阅（",
  "ENABLE WEBHOOKS": "启用网络钩子",
  "Enable webhooks": "启用网络钩子",
  "Enabling...": "启用中...",
  "Webhook receiver disabled": "网络钩子接收器未启用",
  "Webhooks are their own gateway platform. Enable them here to accept incoming HTTP events; chat channels are only needed when a subscription delivers to Telegram, Discord, Slack, or another channel.":
    "网络钩子是独立的网关平台。请在此启用以接收 HTTP 事件；仅当订阅需要投递到 Telegram、Discord、Slack 或其他渠道时，才需要配置聊天渠道。",
  "Subscription changes hot-reload once the webhook receiver is running. Disabled subscriptions reject incoming events.":
    "网络钩子接收器运行后，订阅变更会热加载。已禁用的订阅会拒绝传入事件。",
  "No webhook subscriptions yet.": "暂无网络钩子订阅。",

  "Pending requests": "待处理请求",
  "Pending requests (": "待处理请求（",
  "Approved users": "已批准用户",
  "Approved users (": "已批准用户（",
  "No pending pairing requests": "暂无待处理的配对请求",
  "No approved users": "暂无已批准用户",
  Approve: "批准",
  Reject: "拒绝",
  Revoke: "撤销",

  "Active profile": "当前配置",
  active: "活跃",
  "Gateway running": "网关运行中",
  "Gateway stopped": "网关已停止",

  Host: "主机",
  OS: "操作系统",
  Arch: "架构",
  ARCH: "架构",
  HOST: "主机名",
  PYTHON: "Python",
  CPU: "处理器",
  MEMORY: "内存",
  Disk: "磁盘",
  DISK: "磁盘",
  Uptime: "运行时间",
  UPTIME: "运行时间",
  "Load avg": "平均负载",
  "LOAD AVG": "平均负载",
  "Check for updates": "检查更新",
  "Couldn't reach the update source — try again later.":
    "无法连接更新源，请稍后重试。",
  latest: "最新",
  "Nous Portal": "Nous 门户",
  "not logged in": "未登录",
  "Manage subscription": "管理订阅",
  "TOOL GATEWAY ROUTING": "工具网关路由",
  "Tool Gateway routing": "工具网关路由",
  "Web tools": "网页工具",
  "Image generation": "图像生成",
  "Video generation": "视频生成",
  "OpenAI TTS": "OpenAI 文字转语音",
  "Edge TTS": "Edge 文字转语音",
  "Speech-to-text": "语音转文字",
  "Browser automation": "浏览器自动化",
  "Modal execution": "Modal 执行",
  "not configured": "未配置",
  local: "本地",
  "Log in with hermes portal.": "请通过 Hermes 门户登录。",
  "Log in with": "请登录",
  "inference provider:": "推理提供商：",
  "Skill curator": "技能维护器",
  Pause: "暂停",
  "Run now": "立即运行",
  Gateway: "网关",
  running: "运行中",
  Start: "启动",
  START: "启动",
  Restart: "重启",
  RESTART: "重启",
  Stop: "停止",
  STOP: "停止",
  Memory: "记忆",
  "External provider: built-in only": "外部提供商：仅使用内置提供商",
  "External provider:": "外部提供商：",
  "built-in only": "仅使用内置提供商",
  "session(s) ·": "个会话 ·",
  "Provider setup:": "提供商设置：",
  "configure in Plugins": "在插件页面配置",
  "Change in Plugins →": "前往插件页面更改 →",
  "Provider setup: configure in Plugins": "提供商设置：在插件页面配置",
  "Reset MEMORY.md": "重置 MEMORY.md",
  "Reset USER.md": "重置 USER.md",
  "Reset all": "全部重置",
  "Credential pool": "凭据池",
  Provider: "提供商",
  PROVIDER: "提供商",
  "API key": "API 密钥",
  "API KEY": "API 密钥",
  Label: "标签",
  LABEL: "标签",
  "Add key": "添加密钥",
  "ADD KEY": "添加密钥",
  "No pooled credentials. Add one above to enable key rotation.":
    "暂无池化凭据。请在上方添加，以启用密钥轮换。",
  Operations: "运维操作",
  "Open console": "打开控制台",
  "Run doctor": "运行诊断",
  "Security audit": "安全审计",
  "Update skills": "更新技能",
  "Prompt size": "提示词大小",
  "Support dump": "生成支持信息",
  "Migrate config": "迁移配置",
  "FULL BACKUP": "完整备份",
  "Full backup": "完整备份",
  "Create backup": "创建备份",
  "Download backup": "下载备份",
  "No backup created yet": "尚未创建备份",
  "RESTORE FROM BACKUP UPLOAD": "从上传的备份恢复",
  "Restore from backup upload": "从备份文件恢复",
  "Choose restore zip": "选择恢复压缩包",
  "No backup archive selected": "尚未选择备份文件",
  "Restore upload": "从上传文件恢复",
  "RESTORE FROM BACKUPS PATH": "从备份路径恢复",
  "Restore from backups path": "从备份路径恢复",
  "Restore path": "恢复路径",
  "Share debug report": "分享调试报告",
  "Uploads system info + logs to a public paste service and returns links to send the Hermes team. Pastes auto-delete after 6 hours.":
    "将系统信息和日志上传到公共粘贴服务，并返回可发送给 Hermes 团队的链接。内容会在 6 小时后自动删除。",
  "Generate share link": "生成分享链接",
  "Redact credential-shaped tokens before upload (recommended)":
    "上传前隐藏疑似凭据的内容（推荐）",
  Checkpoints: "检查点",
  "Shell hooks": "Shell 钩子",
  "NEW HOOK": "新建钩子",
  "New hook": "新建钩子",
  "No shell hooks configured.": "尚未配置 Shell 钩子。",
  uploaded: "已上传",
  redacted: "已脱敏",
  "not redacted": "未脱敏",
  "not executable": "不可执行",

  YAML: "YAML",
  MODEL: "模型",
  Model: "模型",
  "Model Context Length": "模型上下文长度",
  "Default model (e.g. anthropic/claude-sonnet-4.6)":
    "默认模型（例如 anthropic/claude-sonnet-4.6）",
  "MODEL CONTEXT LENGTH": "模型上下文长度",
  "Context window override (0 = auto-detect from model metadata)":
    "上下文窗口覆盖值（0 表示根据模型元数据自动检测）",
  "FALLBACK PROVIDERS": "备用提供商",
  "Fallback Providers": "备用提供商",
  TOOLSETS: "工具集",
  Toolsets: "工具集",
  "MAX CONCURRENT SESSIONS": "最大并发会话数",
  "Max Concurrent Sessions": "最大并发会话数",
  "MAX LIVE SESSIONS": "最大存活会话数",
  "Max Live Sessions": "最大存活会话数",
  "CONTEXT FILE MAX CHARS": "上下文文件最大字符数",
  "Context File Max Chars": "上下文文件最大字符数",
  "FILE READ MAX CHARS": "文件读取最大字符数",
  "File Read Max Chars": "文件读取最大字符数",
  "MCP DISCOVERY TIMEOUT": "MCP 发现超时",
  "Mcp Discovery Timeout": "MCP 发现超时",
  "PREFILL MESSAGES FILE": "预填消息文件",
  "Prefill Messages File": "预填消息文件",
  TIMEZONE: "时区",
  Timezone: "时区",
  "COMMAND ALLOWLIST": "命令白名单",
  "Command Allowlist": "命令白名单",
  "HOOKS AUTO ACCEPT": "自动接受钩子",
  "Hooks Auto Accept": "自动接受钩子",
  UPDATES: "更新",
  "PRE UPDATE BACKUP": "更新前备份",
  "Updates → Pre Update Backup": "更新 → 更新前备份",
  "BACKUP KEEP": "保留备份数量",
  "Updates → Backup Keep": "更新 → 保留备份数量",
  "NON INTERACTIVE LOCAL CHANGES": "非交互更新时的本地修改处理",
  "When the chat app / gateway updates Hermes (no terminal prompt), what to do with uncommitted local source edits. 'stash' keeps them and re-applies them after the update; 'discard' throws them away. Terminal updates always ask, regardless of this setting.":
    "聊天应用或网关无终端提示地更新 Hermes 时，如何处理尚未提交的本地源码修改。stash 会保留并在更新后重新应用，discard 会直接丢弃。终端更新始终会询问。",
  "REFRESH CUA DRIVER": "刷新 CUA 驱动",
  "Refresh an already-installed cua-driver during hermes update. Disable this on non-admin macOS accounts where /Applications is not writable.":
    "更新 Hermes 时刷新已安装的 cua-driver。非管理员 macOS 账号无法写入 /Applications 时请关闭。",
  "PASTE COLLAPSE THRESHOLD": "粘贴折叠阈值",
  "Paste Collapse Threshold": "粘贴折叠阈值",
  "PASTE COLLAPSE THRESHOLD FALLBACK": "粘贴折叠备用阈值",
  "Paste Collapse Threshold Fallback": "粘贴折叠备用阈值",
  "PASTE COLLAPSE CHAR THRESHOLD": "粘贴折叠字符阈值",
  "Paste Collapse Char Threshold": "粘贴折叠字符阈值",

  "Messaging platforms, the API server and webhooks are configured on the Channels page. These are gateway-wide settings (proxy/relay mode and the global allowlist).":
    "消息平台、API 服务器和网络钩子请在“消息渠道”页面配置。这里是网关级设置，包括代理/中继模式和全局白名单。",
  "Configure memory providers and runtime context engine selection.":
    "配置记忆提供商和运行时上下文引擎。",
  "Hermes will use the built-in MEMORY.md and USER.md files.":
    "Hermes 将使用内置的 MEMORY.md 和 USER.md 文件。",
  "Save memory provider": "保存记忆提供商",
  "Save context engine": "保存上下文引擎",
  inactive: "未启用",
  enabled: "已启用",
  bundled: "内置",
  user: "用户安装",
  Copy: "复制",
  required: "必填",
  set: "已设置",
  "Browse hub": "浏览技能中心",
  "Learn a skill": "学习技能",
  "Learn it": "开始学习",
  "New skill": "新建技能",
  "Point Hermes at anything and it will distill a reusable skill — following the house authoring standards. Fill in any combination below; the agent gathers the sources and writes the skill in chat.":
    "向 Hermes 提供任意资料，它会按照内置编写规范提炼为可复用技能。可填写下方任意内容，代理会收集资料并在会话中生成技能。",
  "Local file or directory": "本地文件或目录",
  "Anything else — describe the workflow, paste notes, or say \"what we just did\"":
    "其他内容：描述工作流、粘贴笔记，或说明“我们刚才完成的操作”",
  "e.g. how I file an expense report: open the portal, …":
    "例如：如何提交报销申请，包括打开门户等步骤…",
  "Search the skill hub (GitHub, official, community)…":
    "搜索技能中心（GitHub、官方、社区）…",
  Search: "搜索",
  "Update all": "全部更新",
  Dismiss: "关闭",
  Starting: "正在启动",
  "Starting…": "正在启动…",
  done: "已完成",
  "Featured skills": "精选技能",
  "from the Hermes index — search above for thousands more":
    "来自 Hermes 索引，可在上方搜索更多技能",
  "Search the hub above to browse installable skills from the connected sources.":
    "在上方搜索已连接来源中的可安装技能。",
  "No matching skills found in the hub.": "技能中心没有匹配结果。",
  "Connecting to skill hubs…": "正在连接技能中心…",
  "Results come from the same sources as": "结果来源与以下命令一致：",
  "Connected hubs:": "已连接的技能中心：",
  "GitHub API rate-limited — set GITHUB_TOKEN to raise the limit":
    "GitHub API 已限流，可设置 GITHUB_TOKEN 提高限额",
  "Centralized index unavailable — falling back to live sources":
    "中央索引不可用，已回退到实时来源",
  "(rate-limited)": "（已限流）",
  trusted: "可信",
  builtin: "内置",
  community: "社区",
  unknown: "未知",
  installed: "已安装",
  Details: "详情",
  Installed: "已安装",
  "Read SKILL.md": "查看 SKILL.md",
  "Security scan": "安全扫描",
  "Re-scan": "重新扫描",
  Safe: "安全",
  Caution: "需注意",
  Dangerous: "危险",
  "Files:": "文件：",
  "(SKILL.md is empty)": "（SKILL.md 为空）",
  "Couldn't load the skill source.": "无法加载技能源文件。",
  "Fetching, quarantining, and scanning…": "正在获取、隔离并扫描…",
  "Run a security scan to inspect this skill for risky patterns before installing.":
    "安装前运行安全扫描，检查此技能是否包含风险模式。",
  "Verdict:": "结论：",
  "Install allowed": "允许安装",
  "Needs confirmation": "需要确认",
  "Install blocked": "已阻止安装",
  "No risky patterns detected": "未检测到风险模式",
  "Setup results": "设置结果",
  "Python dependencies": "Python 依赖项",
  "owner/repo, owner/repo/subdir, or https://...":
    "owner/repo、owner/repo/子目录或 https://...",
  ready: "就绪",
  "needs setup": "需要设置",
  unavailable: "不可用",
  missing: "缺失",
  "Autonomous Ai Agents": "自主智能体",
  Creative: "创意",
  "Data Science": "数据科学",
  Email: "电子邮件",
  Github: "GitHub",
  Media: "媒体",
  "Note Taking": "笔记",
  Productivity: "效率",
  Research: "研究",
  "Smart Home": "智能家居",
  "Social Media": "社交媒体",
  "Software Development": "软件开发",
  Curator: "维护器",
  Desktop: "桌面端",
  Kanban: "看板",
  Lsp: "LSP",
  Moa: "多智能体混合",
  Model_catalog: "模型目录",
  Openrouter: "OpenRouter",
  Secrets: "密钥",
  Sessions: "会话",
  Streaming: "流式输出",
  Tool_loop_guardrails: "工具循环保护",
  Tool_output: "工具输出",
  Tools: "工具",
  Web: "网页工具",
  X_search: "X 搜索",
  updates: "更新",
  "Pre Update Backup": "更新前备份",
  "Backup Keep": "保留备份数量",
  "Non Interactive Local Changes": "非交互更新时的本地修改",
  "Refresh Cua Driver": "刷新 CUA 驱动",
  "comma-separated values": "逗号分隔的值",
  "Allow all users to interact with messaging bots (true/false). Default: false.":
    "允许所有用户与消息机器人交互（true/false），默认 false。",
  "URL of a remote Hermes API server to forward messages to (proxy mode). When set, the gateway handles platform I/O only — all agent work is delegated to the remote server. Use for Docker E2EE containers that relay to a host agent. Also configurable via gateway.proxy_url in config.yaml.":
    "远程 Hermes API 服务器地址（代理模式）。设置后网关仅处理平台 I/O，所有代理工作都委托给远程服务器。适用于将消息中继到主机代理的 Docker E2EE 容器，也可在 config.yaml 的 gateway.proxy_url 中配置。",
  "Bearer token for authenticating with the remote Hermes API server (proxy mode). Must match the API_SERVER_KEY on the remote host.":
    "用于认证远程 Hermes API 服务器的 Bearer Token（代理模式），必须与远端 API_SERVER_KEY 一致。",
  "Sudo password for terminal commands requiring root access; set to an explicit empty string to try empty without prompting":
    "需要 root 权限的终端命令所使用的 sudo 密码；显式设置为空字符串可在不提示的情况下尝试空密码。",
  "Path to JSON file with ephemeral prefill messages for few-shot priming":
    "用于少样本预填的临时消息 JSON 文件路径。",
  "Ephemeral system prompt injected at API-call time (never persisted to sessions)":
    "在 API 调用时注入的临时系统提示词，不会保存到会话。",
  "Raft agent profile slug — auto-enables the adapter when set":
    "Raft 代理配置标识，设置后自动启用适配器。",

  "Exact Firecrawl tool-gateway origin override for Nous Subscribers only (optional)":
    "仅供 Nous 订阅用户使用的 Firecrawl 工具网关源地址覆盖值（可选）。",
  "Shared tool-gateway domain suffix for Nous Subscribers only, used to derive vendor hosts, e.g. nousresearch.com -> firecrawl-gateway.nousresearch.com":
    "仅供 Nous 订阅用户使用的共享工具网关域名后缀，用于推导各服务商主机地址，例如 nousresearch.com -> firecrawl-gateway.nousresearch.com。",
  "Shared tool-gateway URL scheme for Nous Subscribers only, used to derive vendor hosts (`https` by default, set `http` for local gateway testing)":
    "仅供 Nous 订阅用户使用的共享工具网关 URL 协议，用于推导服务商主机地址（默认 https，本地网关测试可设为 http）。",
  "Explicit Nous Subscriber access token for tool-gateway requests (optional; otherwise read from the Hermes auth store)":
    "工具网关请求使用的 Nous 订阅访问令牌（可选；留空时从 Hermes 认证存储读取）。",
  "Base URL for the Hindsight API (default: https://api.hindsight.vectorize.io)":
    "Hindsight API 基础地址（默认：https://api.hindsight.vectorize.io）。",
  "Base URL for self-hosted RetainDB instances (default: https://api.retaindb.com)":
    "自托管 RetainDB 实例的基础地址（默认：https://api.retaindb.com）。",
  "OpenViking server URL (default: http://127.0.0.1:1933)":
    "OpenViking 服务器地址（默认：http://127.0.0.1:1933）。",
  "WeCom Smart Robot WebSocket URL": "企业微信智能机器人 WebSocket 地址。",
  "Default chat ID for cron / notification delivery":
    "定时任务和通知投递使用的默认聊天 ID。",
  "Comma-separated WeCom user IDs allowed to talk to the bot":
    "允许与机器人交互的企业微信用户 ID，使用逗号分隔。",
  "Comma-separated phone numbers allowed to talk to the bot":
    "允许与机器人交互的电话号码，使用逗号分隔。",
  "Default phone number for cron / notification delivery":
    "定时任务和通知投递使用的默认电话号码。",
  "Quiet-period seconds (default: 0.8) used to concatenate rapid-fire inbound text messages into a single MessageEvent — same pattern as Telegram's text batching.":
    "用于将连续收到的文本消息合并为单个 MessageEvent 的静默等待秒数（默认 0.8），规则与 Telegram 文本批处理一致。",
  "Anthropic OAuth: Required Extra Usage Credits to Use Subscription":
    "Anthropic OAuth：使用订阅需要额外使用额度",
  "all auto": "全部自动",
  "tasks · all auto": "项任务 · 全部自动",
  "references ·": "个参考模型 ·",
  every: "每",
  "h · last run": "小时 · 上次运行",

  "Exa API key for AI-native web search and contents":
    "用于 AI 原生网页搜索和内容获取的 Exa API 密钥。",
  "Parallel API key for AI-native web search and extract":
    "用于 AI 原生网页搜索和提取的 Parallel API 密钥。",
  "Firecrawl API key for web search and scraping":
    "用于网页搜索和抓取的 Firecrawl API 密钥。",
  "Firecrawl API URL for self-hosted instances (optional)":
    "自托管 Firecrawl 实例的 API 地址（可选）。",
  "Tavily API key for AI-native web search and extract":
    "用于 AI 原生网页搜索和提取的 Tavily API 密钥。",
  "URL of your SearXNG instance for free self-hosted web search":
    "用于免费自托管网页搜索的 SearXNG 实例地址。",
  "Brave Search API subscription token (free tier: 2,000 queries/mo)":
    "Brave Search API 订阅令牌（免费额度：每月 2,000 次查询）。",
  "Browserbase API key for cloud browser (optional — local browser works without this)":
    "Browserbase 云浏览器 API 密钥（可选，本地浏览器无需配置）。",
  "Browserbase project ID (optional — only needed for cloud browser)":
    "Browserbase 项目 ID（可选，仅云浏览器需要）。",
  "Browser Use API key for cloud browser (optional — local browser works without this)":
    "Browser Use 云浏览器 API 密钥（可选，本地浏览器无需配置）。",
  "Firecrawl browser session TTL in seconds (optional, default 300)":
    "Firecrawl 浏览器会话存活时间，单位秒（可选，默认 300）。",
  "Browser engine for local mode: auto (default Chrome), lightpanda (faster, no screenshots), chrome":
    "本地模式浏览器引擎：auto（默认 Chrome）、lightpanda（更快但不支持截图）或 chrome。",
  "Camofox browser server URL for local anti-detection browsing (e.g. http://localhost:9377)":
    "本地反检测浏览所使用的 Camofox 服务器地址（例如 http://localhost:9377）。",
  "Optional bearer token sent as Authorization header to a remote/authenticated Camofox server":
    "发送给远程或需认证 Camofox 服务器的可选 Bearer Token。",
  "FAL API key for image and video generation":
    "用于图像和视频生成的 FAL API 密钥。",
  "Krea API key for Krea 2 image generation (Medium + Large)":
    "用于 Krea 2 中型和大型图像生成的 Krea API 密钥。",
  "OpenAI API key for voice transcription (Whisper) and OpenAI TTS":
    "用于语音转写（Whisper）和 OpenAI 文字转语音的 API 密钥。",
  "ElevenLabs API key for premium text-to-speech voices and Scribe transcription":
    "用于高级文字转语音和 Scribe 转写的 ElevenLabs API 密钥。",
  "Mistral API key for Voxtral TTS and transcription (STT)":
    "用于 Voxtral 文字转语音和语音转写的 Mistral API 密钥。",
  "GitHub token for Skills Hub (higher API rate limits, skill publish)":
    "Skills Hub 使用的 GitHub Token，可提高 API 限额并发布技能。",
  "Honcho API key for AI-native persistent memory":
    "用于 AI 原生持久记忆的 Honcho API 密钥。",
  "Base URL for self-hosted Honcho instances (no API key needed)":
    "自托管 Honcho 实例的基础地址（无需 API 密钥）。",
  "Hindsight API key for graph-aware persistent memory":
    "用于图感知持久记忆的 Hindsight API 密钥。",
  "Supermemory API key for conversation-scoped persistent memory":
    "用于会话级持久记忆的 Supermemory API 密钥。",
  "Mem0 Platform API key for semantic persistent memory":
    "用于语义持久记忆的 Mem0 Platform API 密钥。",
  "RetainDB API key for persistent memory": "用于持久记忆的 RetainDB API 密钥。",
  "ByteRover API key (optional, for cloud sync — local-first by default)":
    "ByteRover API 密钥（可选，用于云同步；默认本地优先）。",
  "OpenViking API key (leave blank for local dev mode)":
    "OpenViking API 密钥（本地开发模式可留空）。",
  "Langfuse project public key (pk-lf-...)": "Langfuse 项目公钥（pk-lf-...）。",
  "Langfuse project secret key (sk-lf-...)": "Langfuse 项目密钥（sk-lf-...）。",
  "Langfuse server URL (default: https://cloud.langfuse.com)":
    "Langfuse 服务器地址（默认 https://cloud.langfuse.com）。",
};

const COUNT_LABELS: Record<string, string> = {
  Subscriptions: "订阅",
  "Pending requests": "待处理请求",
  "Approved users": "已批准用户",
  Catalog: "目录",
  "Your MCP servers": "你的 MCP 服务器",
};

function translatePattern(text: string): string | null {
  if (
    text.startsWith(
      "Token & cost analytics are hidden because the local counts exclude auxiliary calls",
    ) &&
    text.endsWith("dashboard.show_token_analytics in")
  ) {
    return "Token 与费用分析已隐藏，因为本地统计不包含压缩、视觉、网页提取等辅助调用及提供商重试，与实际账单可能不同。请在";
  }

  let match = text.match(/^Open (.+) docs$/);
  if (match) return `打开 ${match[1]} 文档`;

  match = text.match(/^(Enable|Disable|Edit|Reveal|Hide) (.+)$/);
  if (match) {
    const verbs: Record<string, string> = {
      Enable: "启用",
      Disable: "禁用",
      Edit: "编辑",
      Reveal: "显示",
      Hide: "隐藏",
    };
    return `${verbs[match[1]]} ${match[2]}`;
  }

  match = text.match(/^(Open|Delete|Download) (.+)$/);
  if (match) {
    const verbs: Record<string, string> = {
      Open: "打开",
      Delete: "删除",
      Download: "下载",
    };
    return `${verbs[match[1]]} ${match[2]}`;
  }

  match = text.match(/^(\d+) results?$/);
  if (match) return `${match[1]} 个结果`;

  match = text.match(/^(.+) timed out$/);
  if (match) return `${match[1]} 响应超时`;

  match = text.match(/^Installing (.+)…$/);
  if (match) return `正在安装 ${match[1]}…`;

  match = text.match(/^(.+) source · (\d+) findings?$/);
  if (match) return `${match[1]} 来源 · ${match[2]} 项发现`;

  match = text.match(/^(Subscriptions|Pending requests|Approved users|Catalog|Your MCP servers) \((\d+)\)$/);
  if (match) return `${COUNT_LABELS[match[1]]}（${match[2]}）`;

  match = text.match(/^Active profile: (.+)$/);
  if (match) return `当前配置：${match[1]}`;

  match = text.match(/^(\d+) cores(.*)$/);
  if (match) return `${match[1]} 核心${match[2]}`;

  match = text.match(/^every (\d+)h$/);
  if (match) return `每 ${match[1]} 小时`;

  match = text.match(/^· last run (.+)$/);
  if (match) return `· 上次运行 ${match[1]}`;

  match = text.match(/^cores(.*)$/);
  if (match) return `核心${match[1]}`;

  match = text.match(/^(\d+) tasks · all auto$/);
  if (match) return `${match[1]} 项任务 · 全部自动`;

  match = text.match(/^(\d+) references · (.+)$/);
  if (match) return `${match[1]} 个参考模型 · ${match[2]}`;

  match = text.match(/^(\d+) of (\d+) channels configured\.(.*)$/);
  if (match) {
    const suffix =
      match[3] === " Credentials are written to"
        ? "凭据写入"
        : match[3];
    return `已配置 ${match[1]}/${match[2]} 个消息渠道。${suffix}`;
  }

  match = text.match(/^Endpoint: (.+)$/);
  if (match) return `端点：${match[1]}`;

  match = text.match(/^Runs: (.+)$/);
  if (match) return `运行命令：${match[1]}`;

  match = text.match(/^Installs from: (.+)$/);
  if (match) return `安装来源：${match[1]}`;

  match = text.match(/^Bootstrap commands \((\d+)\)$/);
  if (match) return `初始化命令（${match[1]}）`;

  match = text.match(/^auth: (.+)$/);
  if (match) return `认证：${match[1]}`;

  match = text.match(/^(\d+) session\(s\) · (.+)$/);
  if (match) return `${match[1]} 个会话 · ${match[2]}`;

  match = text.match(/^inference provider: (.+)$/);
  if (match) return `推理提供商：${match[1]}`;

  match = text.match(/^Built-in files — (.+)$/);
  if (match) return `内置文件 — ${match[1]}`;

  match = text.match(/^every (\d+)h · last run (.+)$/);
  if (match) return `每 ${match[1]} 小时 · 上次运行 ${match[2]}`;

  return null;
}

export function translateDashboardText(
  value: string,
  parentText?: string,
): string {
  const rawText = value.trim();
  if (!rawText) return value;
  const text = rawText.replace(/\s+/g, " ");
  if (text === ")" && parentText?.includes("（")) {
    return value.replace(rawText, "）");
  }
  if (
    text === "in" &&
    parentText?.includes("dashboard.show_token_analytics")
  ) {
    return "，位置：";
  }
  const exact = EXACT_TRANSLATIONS[text];
  if (exact) return exact;
  const patterned = translatePattern(text);
  if (patterned) return patterned;
  return value;
}

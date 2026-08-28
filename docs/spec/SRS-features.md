# Hermes 后端 — 详细功能清单(SRS 附录)

本文件是 `docs/spec/SRS.md` 的附录,收录体量过大、不宜放入主文档的完整清单:Provider 插件表、CLI 子命令、斜杠命令、HTTP 服务面与路由、移动端 Owner API 契约、协作插件 API、cron 任务存储结构、kanban 存储模型等。

所有内容按 2026-07-26 的代码实况提取(版本 0.19.0)。无法确证之处标注 `待确认`。

---

## A. 模型 Provider 插件全表

来源:`plugins/model-providers/<name>/__init__.py`(32 个内置插件)。发现顺序见 `providers/__init__.py`:①内置 `plugins/model-providers/` → ②用户 `$HERMES_HOME/plugins/model-providers/` → ③遗留单文件 `providers/<name>.py`;同名后注册者覆盖先注册者(last-writer-wins),用户插件可整体替换内置 profile。

`ProviderProfile`(`providers/base.py`)为纯声明式:身份(name/aliases/display_name)、auth(auth_type/env_vars)、端点(base_url/models_url/hostname)、怪癖(fixed_temperature 支持 `OMIT_TEMPERATURE` 哨兵、default_headers、default_max_tokens、default_aux_model)、能力(supports_vision、supports_vision_tool_messages、supports_health_check、fallback_models)与钩子(prepare_messages、build_extra_body、build_api_kwargs_extras、default_vision_model、get_max_tokens、fetch_models)。客户端构造、凭据轮换、流式传输均不在 profile 内,归 `AIAgent` 所有。

| name | api_mode | auth_type | base_url | env_vars | aliases |
|---|---|---|---|---|---|
| alibaba | chat_completions | api_key | https://dashscope-intl.aliyuncs.com/compatible-mode/v1 | DASHSCOPE_API_KEY | dashscope, alibaba-cloud, qwen-dashscope |
| alibaba-coding-plan | chat_completions | api_key | https://coding-intl.dashscope.aliyuncs.com/v1 | ALIBABA_CODING_PLAN_API_KEY, DASHSCOPE_API_KEY, ALIBABA_CODING_PLAN_BASE_URL | alibaba_coding, alibaba-coding, dashscope-coding |
| anthropic | anthropic_messages | api_key | https://api.anthropic.com | ANTHROPIC_API_KEY, ANTHROPIC_TOKEN, CLAUDE_CODE_OAUTH_TOKEN | claude, claude-oauth, claude-code |
| arcee | chat_completions | api_key | https://api.arcee.ai/api/v1 | ARCEEAI_API_KEY | arcee-ai, arceeai |
| azure-foundry | chat_completions | api_key | (由 AZURE_FOUNDRY_BASE_URL 提供) | AZURE_FOUNDRY_API_KEY, AZURE_FOUNDRY_BASE_URL | azure, azure-ai-foundry, azure-ai |
| bedrock | bedrock_converse | aws_sdk | https://bedrock-runtime.us-east-1.amazonaws.com | (走 AWS SDK 凭据链) | aws, aws-bedrock, amazon-bedrock, amazon |
| copilot | chat_completions | copilot | https://api.githubcopilot.com | COPILOT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN | github-copilot, github-models, github-model, github |
| copilot-acp | copilot_acp | external_process | acp://copilot | — | github-copilot-acp, copilot-acp-agent |
| custom | chat_completions | api_key | (用户提供) | — | ollama, local, vllm, llamacpp, llama.cpp, llama-cpp |
| deepinfra | chat_completions | api_key | https://api.deepinfra.com/v1/openai | DEEPINFRA_API_KEY, DEEPINFRA_BASE_URL | deep-infra, deepinfra-ai |
| deepseek | chat_completions | api_key | https://api.deepseek.com/v1 | DEEPSEEK_API_KEY | deepseek-chat |
| fireworks | chat_completions | api_key | https://api.fireworks.ai/inference/v1 | FIREWORKS_API_KEY | fireworks-ai, fw |
| gemini | chat_completions | api_key | https://generativelanguage.googleapis.com/v1beta | GOOGLE_API_KEY, GEMINI_API_KEY | google, google-gemini, google-ai-studio |
| gmi | chat_completions | api_key | https://api.gmi-serving.com/v1 | GMI_API_KEY, GMI_BASE_URL | gmi-cloud, gmicloud |
| huggingface | chat_completions | api_key | https://router.huggingface.co/v1 | HF_TOKEN | hf, hugging-face, huggingface-hub |
| kilocode | chat_completions | api_key | https://api.kilo.ai/api/gateway | KILOCODE_API_KEY | kilo-code, kilo, kilo-gateway |
| kimi-coding | chat_completions | api_key | https://api.moonshot.ai/v1 | KIMI_API_KEY, KIMI_CODING_API_KEY | kimi, moonshot, kimi-for-coding |
| minimax | anthropic_messages | api_key | https://api.minimax.io/anthropic | MINIMAX_API_KEY | mini-max |
| nous | chat_completions | oauth_device_code | https://inference-api.nousresearch.com/v1 | NOUS_API_KEY | nous-portal, nousresearch |
| novita | chat_completions | api_key | https://api.novita.ai/openai/v1 | NOVITA_API_KEY, NOVITA_BASE_URL | novita-ai, novitaai |
| nvidia | chat_completions | api_key | https://integrate.api.nvidia.com/v1 | NVIDIA_API_KEY | nvidia-nim |
| ollama-cloud | chat_completions | api_key | https://ollama.com/v1 | OLLAMA_API_KEY | ollama_cloud |
| openai-codex | codex_responses | oauth_external | https://chatgpt.com/backend-api/codex | (ChatGPT OAuth,凭据在 auth.json) | codex, openai_codex |
| opencode-zen | chat_completions | api_key | https://opencode.ai/zen/v1 | OPENCODE_ZEN_API_KEY | opencode, opencode_zen, zen |
| openrouter | chat_completions | api_key | https://openrouter.ai/api/v1 | OPENROUTER_API_KEY | or |
| qwen-oauth | chat_completions | oauth_external | https://portal.qwen.ai/v1 | QWEN_API_KEY | qwen, qwen-portal, qwen-cli |
| stepfun | chat_completions | api_key | https://api.stepfun.ai/step_plan/v1 | STEPFUN_API_KEY | step, stepfun-coding-plan |
| upstage | chat_completions | api_key | https://api.upstage.ai/v1 | UPSTAGE_API_KEY, UPSTAGE_BASE_URL | solar |
| vertex | chat_completions | vertex | https://aiplatform.googleapis.com | (google-auth ADC / VERTEX_CREDENTIALS_PATH) | google-vertex, vertex-ai, gcp-vertex |
| xai | codex_responses | api_key | https://api.x.ai/v1 | XAI_API_KEY | grok, x-ai, x.ai |
| xiaomi | chat_completions | api_key | https://api.xiaomimimo.com/v1 | XIAOMI_API_KEY | mimo, xiaomi-mimo |
| zai | chat_completions | api_key | https://api.z.ai/api/paas/v4 | GLM_API_KEY, ZAI_API_KEY, Z_AI_API_KEY | glm, z-ai, z.ai, zhipu |

要点:

- **api_mode 五种**:`chat_completions`(OpenAI 兼容,默认)、`anthropic_messages`(anthropic 与 minimax)、`codex_responses`(openai-codex 与 xai)、`bedrock_converse`(bedrock)、`copilot_acp`(copilot-acp,经外部 ACP 进程)。各自对应 `agent/` 下的 adapter(`anthropic_adapter.py`、`codex_responses_adapter.py`、`bedrock_adapter.py`、`copilot_acp_client.py` 等)。
- **auth_type**:`api_key`(默认)、`oauth_device_code`(nous)、`oauth_external`(openai-codex、qwen-oauth)、`copilot`(GitHub token 换 Copilot token)、`aws_sdk`(bedrock)、`vertex`(google-auth)、`external_process`(copilot-acp)。
- `fetch_models()` 默认实现经 `hermes_cli.urllib_security.open_credentialed_url` 拉取 `{base_url}/models`,失败回退静态 `fallback_models`;UA 设为 `hermes-cli/<version>`(规避 WAF 拦截默认 Python-urllib UA)。
- Kimi 用 `OMIT_TEMPERATURE` 哨兵表示"完全不发送 temperature"。

### A.1 凭据池与 auth.json

- `agent/credential_pool.py`:同一 provider 多凭据故障转移。凭据状态机 `STATUS_OK` / `STATUS_EXHAUSTED` / `STATUS_DEAD`;OAuth 终态原因集合 `_TERMINAL_AUTH_REASONS`(token_invalidated、token_revoked 等)进入 DEAD 不再重试。轮换策略经 config `credential_pool_strategies`(按 provider)配置。
- `hermes_cli/auth.py`:持久化于 `~/.hermes/auth.json`;写入采用 O_EXCL 原子创建 + `0o600`(防 TOCTOU),读写整体走跨进程 advisory 文件锁(`_auth_store_lock`,一个读/写事务);profile 模式下先查 profile 自己的 auth.json,缺失回退全局根 auth.json。
- Provider 解析顺序的最后一环是 `auth.json active_provider`(已登录 OAuth 的兜底)。

---

## B. `hermes` CLI 子命令清单

来源:`hermes_cli/_parser.py`(顶层 parser + `chat`)、`hermes_cli/main.py`、`hermes_cli/subcommands/*.py` 及各领域模块的注册函数。入口点:`hermes`(`hermes_cli.main:main`)、`hermes-agent`(`run_agent:main`)、`hermes-acp`(`acp_adapter.entry:main`)。

| 子命令 | 定义位置 | 功能 |
|---|---|---|
| chat | `hermes_cli/_parser.py` | 交互式会话(默认命令)。关键 flag:`-q/--query` 单问、`-m/--model`、`--provider`、`-t/--toolsets`、`-s/--skills`、`-r/--resume <id>`、`-c/--continue [name]`、`-w/--worktree`(git worktree 隔离)、`--yolo`、`--checkpoints`、`--max-turns N`、`--ignore-user-config`、`--ignore-rules`、`--safe-mode`(前两者之和再禁 plugins/MCP)、`--tui` / `--cli`、`--dev`(tsx 直跑 TUI 源码)、`-Q/--quiet`、`--source`(会话来源标签)、`--pass-session-id`、`--accept-hooks`、`--no-restore-cwd` |
| send | `hermes_cli/send_cmd.py` | 向运行中的 gateway/会话发送消息 |
| gateway | `hermes_cli/subcommands/gateway.py` | 消息平台网关(run/start/stop/status 等子命令;`gateway run` 为长驻进程) |
| proxy | `hermes_cli/subcommands/gateway.py` | 网关代理(与 GATEWAY_PROXY_URL/KEY 相关) |
| dashboard | `hermes_cli/subcommands/dashboard.py` | 启动 FastAPI dashboard(默认 127.0.0.1:9119,构建并服务 SPA,打开浏览器) |
| serve | `hermes_cli/subcommands/dashboard.py` | 同一服务器的 headless 形态:`set_defaults(func=cmd_dashboard, no_open=True, headless_backend=True)`,设 `HERMES_SERVE_HEADLESS=1`,不服务 SPA |
| desktop | `hermes_cli/subcommands/gui.py` | Electron 桌面应用 |
| console | `hermes_cli/subcommands/console.py` | 控制台(console_engine) |
| acp | `hermes_cli/subcommands/acp.py` | ACP(IDE 集成)入口,转交 `acp_adapter` |
| setup | `hermes_cli/subcommands/setup.py` | 引导向导(`--portal` 走 Nous Portal 订阅) |
| login / logout | `hermes_cli/subcommands/login.py`, `logout.py` | OAuth 登录/登出(auth.json) |
| auth | `hermes_cli/subcommands/auth.py` | 凭据管理 |
| model | `hermes_cli/subcommands/model.py` | 模型选择器 |
| config | `hermes_cli/subcommands/config.py` | `config get/set` 等配置读写 |
| tools | `hermes_cli/subcommands/tools.py` | 工具/工具集管理 |
| skills | `hermes_cli/subcommands/skills.py` | 技能搜索/安装/管理(agentskills.io) |
| memory | `hermes_cli/subcommands/memory.py` | 记忆管理(含 pending 审批) |
| mcp | `hermes_cli/subcommands/mcp.py` | MCP 服务器管理;`mcp serve` 为 stdio MCP 服务端(`mcp_serve.py`) |
| cron | `hermes_cli/subcommands/cron.py` | 定时任务管理(`cron run`/`cron tick` 为执行路径) |
| kanban | `hermes_cli/kanban.py` | 看板;子命令:init/create/list/show/edit/assign/claim/complete/block/unblock/link/unlink/comment/attach/attach-rm/attachments/watch/schedule/promote/decompose/specify/context/boards/archive/reassign/reclaim/heartbeat/daemon/dispatch/swarm/runs/log/tail/gc/stats/diagnostics/notify-list/notify-subscribe/notify-unsubscribe/assignees |
| projects | `hermes_cli/projects_cmd.py` | 项目(create/list/show/use/rename/archive/restore/add-folder/remove-folder/bind-board/set-primary) |
| sessions | `hermes_cli/main.py` | 会话管理:list/export/delete/prune/archive/optimize/repair/stats/rename/browse |
| profile | `hermes_cli/subcommands/profile.py` | 多 profile 管理(create/use/delete/list;`--clone`/`--clone-all`) |
| doctor | `hermes_cli/subcommands/doctor.py` | 诊断 |
| status | `hermes_cli/subcommands/status.py` | 状态 |
| logs | `hermes_cli/subcommands/logs.py` | 日志查看 |
| insights | `hermes_cli/subcommands/insights.py` | 用量分析 |
| update | `hermes_cli/subcommands/update.py` | 自更新(git pull + 重装;pre_update_backup) |
| uninstall / postinstall | `hermes_cli/subcommands/uninstall.py`, `postinstall.py` | 卸载/装后钩子 |
| version | `hermes_cli/subcommands/version.py` | 版本 |
| debug | `hermes_cli/subcommands/debug.py` | 上传诊断报告 |
| dump | `hermes_cli/subcommands/dump.py` | 导出数据 |
| prompt-size | `hermes_cli/subcommands/prompt_size.py` | system prompt 体积分析 |
| security | `hermes_cli/subcommands/security.py` | 安全公告/advisory 管理 |
| backup | `hermes_cli/subcommands/backup.py` | 备份/恢复 |
| import | `hermes_cli/subcommands/import_cmd.py` | 导入(会话等) |
| claw | `hermes_cli/subcommands/claw.py` | OpenClaw 迁移(`claw migrate`) |
| hooks | `hermes_cli/subcommands/hooks.py` | shell hooks 管理(配合 `hooks_auto_accept` 与 `~/.hermes/shell-hooks-allowlist.json`) |
| pairing | `hermes_cli/subcommands/pairing.py` | 消息平台配对码审批 |
| plugins | `hermes_cli/subcommands/plugins.py` | 插件列表/状态 |
| slack / whatsapp / whatsapp-cloud | `hermes_cli/slack_cli.py` 等 | 平台专用设置流 |
| webhook | `hermes_cli/subcommands/webhook.py` | webhook 订阅管理 |
| moa | `hermes_cli/main.py` | MoA preset 管理:list/configure/delete |
| fallback | `hermes_cli/main.py` | fallback 链管理:list/add/remove/clear |
| secrets | `hermes_cli/main.py` + `secrets_cli.py`/`onepassword_secrets_cli.py` | 密钥后端:`secrets bitwarden`(install/setup/status/sync/disable)、`secrets onepassword`(setup/set/remove/status/sync/disable) |
| migrate | `hermes_cli/main.py` | 迁移(子命令 xai) |
| checkpoints | `hermes_cli/checkpoints.py` | 文件系统检查点:status/list/prune/clear/clear-legacy |
| bundles | `hermes_cli/bundles.py` | 技能包:list/show/create/delete/reload |
| curator | `hermes_cli/curator.py` | 技能维护:status/run/pause/resume/pin/unpin/archive/restore/list-archived/prune/backup/rollback/usage |
| pets | `hermes_cli/pets.py` | 吉祥物:list/select/show/install/remove/scale/off/doctor |
| journey | `hermes_cli/journey.py` | 学习时间线:list/edit/delete |
| computer-use | `hermes_cli/main.py` | CUA 驱动:install/status/doctor/permissions(status/grant) |
| completion | `hermes_cli/main.py` | shell 补全 |
| portal | `hermes_cli/portal_cli.py` | Nous Portal 计费/订阅 |

顶层通用 flag(先于子命令解析,经 `_inherited_flag` 与子命令共享 namespace):`-p/--profile <name>`(经 `_apply_profile_override()` 切 HERMES_HOME)、`-m/--model`、`--provider`、`-r/--resume`、`-c/--continue`、`-w/--worktree`、`--yolo`、`--accept-hooks`、`--ignore-user-config`、`--ignore-rules`、`--safe-mode`、`--tui`/`--cli`/`--dev` 等。

分发特例(`hermes_cli/main.py` ~12963):`acp`、`gateway run`、`cron run|tick` 在常规分发前拦截(需要特殊进程环境)。

---

## C. 斜杠命令清单(COMMAND_REGISTRY)

来源:`hermes_cli/commands.py` 的 `COMMAND_REGISTRY`(`CommandDef` 列表)。字段:name、description、category(Session / Configuration / Tools & Skills / Info / Exit)、aliases、args_hint、`cli_only`、`gateway_only`、`gateway_config_gate`(config 点路径为 truthy 时,允许原本 cli_only 的命令在 gateway 使用)。CLI(REPL/TUI)与 gateway(消息平台)共用一张注册表。未标注界面者两侧均可用(dataclass 默认 cli_only=False、gateway_only=False)。

| 命令 | 类别 | 说明 | 界面限制 / 备注 |
|---|---|---|---|
| /start | Session | 响应平台 start ping,不回复 | gateway_only |
| /new | Session | 新会话(新 session ID + 历史) | |
| /topic | Session | Telegram DM topic 会话开关 | gateway_only,args `[off\|help\|session-id]` |
| /clear | Session | 清屏并新会话 | cli_only |
| /redraw | Session | 强制重绘 UI | cli_only |
| /history | Session | 显示会话历史 | cli_only |
| /save | Session | 保存当前会话 | cli_only |
| /retry | Session | 重发上一条消息 | |
| /prompt | Session | 在 $EDITOR 中撰写下一条 prompt | cli_only,alias `compose` |
| /undo | Session | 回退 N 个用户轮次并重新输入(默认 1) | |
| /title | Session | 设置会话标题 | |
| /handoff | Session | 把会话移交到消息平台 | cli_only,args `<platform>` |
| /branch | Session | 会话分支 | |
| /compress | Session | 压缩上下文(`here [N]` 保留近 N 轮;`--preview`) | |
| /rollback | Session | 列出/恢复文件系统检查点 | |
| /snapshot | Session | 配置/状态快照(create/restore/prune) | cli_only,alias `snap` |
| /stop | Session | 杀掉所有后台进程 | |
| /approve | Session | 批准待审批危险命令 | gateway_only,args `[session\|always]` |
| /deny | Session | 拒绝待审批危险命令 | gateway_only,args `[all] [reason]` |
| /background | Session | 后台运行一个 prompt | |
| /agents | Session | 显示活动 agent 与任务 | |
| /journey | Session | 学习时间线 | cli_only,aliases `learning`,`memory-graph` |
| /queue | Session | 排队 prompt 到下一轮(不打断) | |
| /steer | Session | 下一次工具调用后注入消息(不打断) | |
| /goal | Session | 设定跨轮持续目标 | |
| /moa | Session | 单次经默认 MoA preset 运行后恢复原模型 | |
| /subgoal | Session | 给活动目标加子标准 | |
| /status | Session | 会话/模型/token/上下文信息 | |
| /whoami | Info | 显示斜杠命令权限(admin/user) | |
| /profile | Info | 活动 profile 与 home 目录 | |
| /sethome | Session | 把当前聊天设为 home channel | gateway_only,alias `set-home` |
| /resume | Session | 恢复具名会话 | |
| /sessions | Session | 浏览/恢复历史会话 | |
| /config | Configuration | 显示当前配置 | cli_only |
| /model | Configuration | 切换模型(会话级;`--global` 持久化) | |
| /codex-runtime | Configuration | 切换 codex app-server 运行时 | |
| /personality | Configuration | 预设人格 | |
| /statusbar | Configuration | 状态栏开关 | cli_only,alias `sb` |
| /timestamps | Configuration | [HH:MM] 时间戳开关 | cli_only,args `[on\|off\|status]` |
| /verbose | Configuration | 工具进度显示循环:off→new→all→verbose→log | cli_only,gate `display.tool_progress_command` |
| /footer | Configuration | gateway 运行时元数据 footer 开关 | |
| /yolo | Configuration | YOLO 模式(跳过所有危险命令审批) | |
| /reasoning | Configuration | reasoning effort 与显示管理 | |
| /fast | Configuration | fast 模式(OpenAI Priority / Anthropic Fast) | |
| /skin | Configuration | 显示皮肤 | cli_only,args `[name]` |
| /indicator | Configuration | TUI busy 指示样式 | cli_only,args `[kaomoji\|emoji\|unicode\|ascii]` |
| /voice | Configuration | 语音模式开关 | |
| /busy | Configuration | 忙碌时 Enter 行为(queue/steer/interrupt) | cli_only |
| /tools | Tools & Skills | `/tools [list\|disable\|enable] [name...]` | cli_only |
| /toolsets | Tools & Skills | 列出可用工具集 | cli_only |
| /skills | Tools & Skills | 技能搜索/安装/管理(含 pending/diff/approve/reject) | gate `skills.write_approval`(写审批开启时 gateway 可用) |
| /memory | Tools & Skills | 待审记忆写入 / 审批门开关 | CLI 与 gateway 均可用；命令内部管理 `memory.write_approval`，其可达性不受该开关门控 |
| /bundles | Tools & Skills | 技能包列表 | |
| /pet | Tools & Skills | petdex 吉祥物 | cli_only,args `[toggle\|list\|scale <n>\|<slug>]` |
| /hatch | Tools & Skills | 由描述生成新宠物 | cli_only,alias `generate-pet` |
| /learn | Tools & Skills | 从任意来源学习可复用技能 | |
| /cron | Tools & Skills | 定时任务管理 | cli_only,args `[subcommand]` |
| /suggestions | Tools & Skills | 自动化建议(accept/dismiss) | |
| /blueprint | Tools & Skills | 从模板建立自动化 | |
| /curator | Tools & Skills | 技能后台维护 | |
| /kanban | Tools & Skills | 多 profile 协作看板 | |
| /reload | Tools & Skills | 重载 .env 到运行中会话 | cli_only |
| /reload-mcp | Tools & Skills | 重载 MCP 服务器 | |
| /reload-skills | Tools & Skills | 重扫 ~/.hermes/skills/ | |
| /browser | Tools & Skills | 经 CDP 连接本机 Chromium 浏览器 | cli_only,args `[connect\|disconnect\|status]` |
| /plugins | Tools & Skills | 已装插件列表 | cli_only |
| /commands | Info | 分页浏览命令与技能 | gateway_only,args `[page]` |
| /help | Info | 可用命令 | |
| /restart | Session | 排空活动 run 后优雅重启 gateway | gateway_only |
| /usage | Info | token 用量与限额;`reset` 兑换 Codex 限额重置 | |
| /subscription | Info | Nous 订阅计划 | cli_only,alias `upgrade` |
| /topup | Info | Nous 余额与计费 | |
| /insights | Info | 用量洞察 | |
| /platforms | Info | 网关平台状态 | cli_only,alias `gateway` |
| /platform | Info | 暂停/恢复/列出故障平台 | gateway_only,args `<pause\|resume\|list> [name]` |
| /copy | Info | 复制上一条回复到剪贴板 | cli_only,args `[number]` |
| /paste | Info | 从剪贴板附图 | cli_only |
| /image | Info | 附加本地图片 | cli_only,args `<path>` |
| /update | Info | 更新 Hermes | |
| /version | Info | 版本 | alias `v` |
| /debug | Info | 上传调试报告 | |
| /quit | Exit | 退出 CLI(`--delete` 连历史一起删) | |

Gateway 侧另有 runner 级拦截(先于 agent 看到消息):`/stop` `/new` `/queue` `/status` `/approve` `/deny` 在 `gateway` runner 中直接处理;基类 adapter 维护 `_pending_messages` 缓冲,构成双重消息守卫。

---

## D. 工具目录(toolsets × tools)

工具注册模式:`tools/registry.py` 的 `registry.register(name, toolset, schema, handler, check_fn, requires_env)`,并同步登记到 `toolsets.py`。`check_fn` 决定工具是否可用(环境/配置门槛);核心工具永不被 tool_search 延迟加载(`tools.tool_search.enabled: "auto"`,当工具数超过上下文的 `threshold_pct: 10` 时启用检索式装载)。

### D.1 两层命名:注册键 vs 组合预设

`toolsets.py::TOOLSETS` 有 **57** 个条目,但它们不是同一种东西,混用会导致配置写错:

- **注册键(29 个)** —— `registry.register(name=..., toolset=...)` 里 `toolset` 参数的字面值,是工具的归属分类。
- **组合预设(`hermes-*`)** —— 每个使用面(CLI / 各消息平台 / cron / ACP / API server)的打包集合,是 `config.yaml` 里 `toolsets:` 应当填的值。默认 `toolsets: ["hermes-cli"]`(53 个工具)。

组合预设规模(实测):`hermes-cli` 53、`hermes-cron` 53、`hermes-api-server` 35、`hermes-acp` 29、`hermes-feishu` 58、`hermes-yuanbao` 58、`hermes-discord` 55、`hermes-bluebubbles` 53,其余各消息平台均为 53;`hermes-webhook` 4;`hermes-gateway` 与 `context_engine`、`safe` 为 0(运行时动态填充或刻意留空)。

> **文档勘误(本次核对发现)**:早期版本此处列出的键来自 AGENTS.md,与实际注册结果不符。实际**不存在**注册键 `debugging`、`messaging`、`moa`、`rl`、`safe`、`search`、`spotify`、`yuanbao`(其中部分仅作为 `TOOLSETS` 中的组合/占位条目存在);实际**存在但曾遗漏**的有 `browser-cdp`、`computer_use`、`managed_installations`、`project`、`video_gen`、`x_search`。下表以 AST 扫描 `tools/**` 的 `registry.register(...)` 实参为准。

### D.2 逐工具清单(73 个工具 / 29 个注册键)

| 注册键 | 工具 | 实现文件(`tools/`) | 环境门槛 |
|---|---|---|---|
| browser | browser_navigate, browser_click, browser_type, browser_press, browser_scroll, browser_back, browser_snapshot, browser_console, browser_get_images, browser_vision | browser_tool.py | |
| browser-cdp | browser_cdp | browser_cdp_tool.py | |
| browser-cdp | browser_dialog | browser_dialog_tool.py | |
| clarify | clarify | clarify_tool.py | |
| code_execution | execute_code | code_execution_tool.py | |
| computer_use | computer_use | computer_use_tool.py | cua-driver(macOS/Windows/Linux) |
| cronjob | cronjob | cronjob_tools.py | |
| delegation | delegate_task | delegate_tool.py | |
| discord | discord | discord_tool.py | `DISCORD_BOT_TOKEN` |
| discord_admin | discord_admin | discord_tool.py | `DISCORD_BOT_TOKEN` |
| feishu_doc | feishu_doc_read | feishu_doc_tool.py | |
| feishu_drive | feishu_drive_list_comments, feishu_drive_list_comment_replies, feishu_drive_reply_comment, feishu_drive_add_comment | feishu_drive_tool.py | |
| file | read_file, write_file, patch, search_files | file_tools.py | |
| homeassistant | ha_get_state, ha_call_service, ha_list_entities, ha_list_services | homeassistant_tool.py | |
| image_gen | image_generate | image_generation_tool.py | |
| kanban | kanban_create, kanban_list, kanban_show, kanban_comment, kanban_complete, kanban_block, kanban_unblock, kanban_link, kanban_attach, kanban_attach_url, kanban_attachments, kanban_heartbeat | kanban_tools.py | 仅在 agent 由看板派生时激活 |
| managed_installations | managed_installation | managed_installation_tool.py | |
| memory | memory | memory_tool.py | |
| project | project_create, project_list, project_switch | project_tools.py | 仅 GUI 会话 |
| session_search | session_search | session_search_tool.py | |
| skills | skills_list, skill_view, skill_manage | skills_tool.py / skill_manager_tool.py | |
| terminal | terminal, read_terminal, close_terminal, process | terminal_tool.py / read_terminal_tool.py / close_terminal_tool.py / process_registry.py | |
| todo | todo | todo_tool.py | |
| tts | text_to_speech | tts_tool.py | |
| video | video_analyze | vision_tools.py | |
| video_gen | video_generate | video_generation_tool.py | |
| video_gen | xai_video_edit, xai_video_extend | xai_video_tools.py | |
| vision | vision_analyze | vision_tools.py | |
| web | web_search, web_extract | web_tools.py | 动态 `requires_env`(按已配置的搜索后端解析) |
| x_search | x_search | x_search_tool.py | `XAI_API_KEY` |
| *(无 toolset 实参)* | yb_send_dm, yb_send_sticker, yb_search_sticker, yb_query_group_info, yb_query_group_members | yuanbao_tools.py | 经 `hermes-yuanbao` 预设纳入 |

`terminal` 的工具描述在本轮修正为 "persistent POSIX bash shell"(原文 "a Linux environment" 与 Windows/macOS 本地后端的环境提示自相矛盾,见 `tools/terminal_tool.py:969`)。

---

## E. 网关平台适配器与能力矩阵

平台适配器位于 `gateway/platforms/` 与 `plugins/platforms/`,共 **29 个** adapter 类继承自 `BasePlatformAdapter`(`gateway/platforms/base.py`)。

### E.1 能力如何声明

两种机制,理解它们的区别才能正确加平台:

1. **类属性开关** —— 基类给出默认值,子类覆写以声明渲染/投递语义:`supports_code_blocks`(默认 `False`)、`supports_status_text`(`False`)、`supports_async_delivery`(默认 **`True`**)、`supports_inchannel_continuable`(`False`,调度器用 `getattr` 读取)。
2. **方法覆写** —— 基类为每种媒体提供默认实现(通常降级为"发链接/发文字"),子类覆写 `send_image` / `send_document` / `send_voice` / `send_video` / `send_animation` / `send_typing` / `send_draft` / `send_clarify` / `edit_message` / `add_reaction` 来使用平台原生能力。**未覆写不等于不可用**,而是走基类降级路径。

### E.2 能力矩阵(AST 实测:Y = 该平台自行覆写/开启)

| 平台 | code | status | 续聊 | 图 | 文档 | 语音 | 视频 | 动图 | typing | 草稿流式 | clarify | reaction | 编辑 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Telegram | Y | | | Y | Y | Y | Y | Y | Y | **Y** | Y | | Y |
| Slack | Y | **Y** | **Y** | Y | Y | Y | Y | | Y | | | | Y |
| Discord | Y | | | Y | Y | Y | Y | Y | Y | | Y | | Y |
| Feishu | Y | | | Y | Y | Y | Y | Y | Y | | | | Y |
| Google Chat | | | | Y | Y | Y | Y | Y | Y | | Y | | Y |
| Matrix | Y | | | Y | Y | Y | Y | | Y | | | | Y |
| Mattermost | | | | Y | Y | Y | Y | | Y | | | | Y |
| WhatsApp | | | | Y | Y | Y | Y | | Y | | Y | | Y |
| WhatsApp Cloud | | | | Y | Y | Y | Y | | Y | | Y | | |
| Weixin | Y | | | Y | Y | Y | Y | | Y | | | | |
| BlueBubbles | | | | Y | Y | Y | Y | Y | Y | | | | |
| Photon | | | | Y | Y | Y | Y | Y | Y | | | **Y** | |
| Signal | | | | Y | Y | Y | Y | | Y | | | | |
| SimpleX | | | | Y | Y | Y | Y | | Y | | | | |
| QQ | | | | Y | Y | Y | Y | | Y | | | | |
| Teams | | | | Y | Y | Y | Y | | Y | | | | |
| WeCom | | | | Y | Y | Y | Y | | Y | | | | |
| DingTalk | | | | Y | Y | | | | Y | | | | Y |
| Yuanbao | | | | Y | Y | | | | Y | | | | |
| Email | | | | Y | Y | | | | Y | | | | |
| LINE | | | | | | Y | Y | | Y | | | | |
| Home Assistant | | | | | | | | | Y | | | | |
| IRC | | | | | | | | | Y | | | | |
| ntfy | | | | | | | | | Y | | | | |
| SMS / WeCom-callback | | | | | | | | | | | | | |

非会话型 adapter(不出现在上表):`APIServerAdapter`(OpenAI 兼容 HTTP 面)、`WebhookAdapter`、`MSGraphWebhookAdapter`、`RaftAdapter`。

### E.3 读表要点

- **Telegram 是唯一实现草稿流式**(`send_draft` + `supports_draft_streaming`)的平台;其余平台一次性投递完整回复。
- **Slack 是唯一同时开启 `supports_status_text` 与 `supports_inchannel_continuable`** 的平台。
- **Photon 是唯一自行实现 `add_reaction`** 的平台。
- SMS 与 WeCom-callback 整行为空 —— 纯文本单向面,所有富媒体走基类降级。
- `supports_async_delivery` 未在任何子类中被覆写为 `False`,即全部沿用基类默认 `True`。

### E.4 加一个平台需要动的地方

1. 在 `plugins/platforms/<name>/` 建 `adapter.py`,继承 `BasePlatformAdapter`;
2. 只覆写平台原生支持的 `send_*`,其余交给基类降级;
3. 按需覆写上述四个类属性开关;
4. 在 `toolsets.py` 增加 `hermes-<name>` 组合预设(参照既有 53 工具的平台预设);
5. 若引入外部下载源,必须接入 SSRF 与媒体投递校验(见 §5 安全模型与 `gateway/platforms/base.py` 的 basename 拒绝表);
6. Webhook 型平台须实现重放保护 —— 参照 Feishu 的 `(timestamp, nonce)` 方案,并注意 nonce 只能在 2xx 成功路径提交(见 `plugins/platforms/feishu/adapter.py:3604-3665`)。

---

## F. HTTP 服务面与路由清单

代码库存在 **6 个可并行监听的 HTTP/TCP 服务面**(仅 dashboard 常开,其余 5 个是 gateway 平台按配置启用)+ 2 个 stdio-only RPC 面。三个"信条式"服务面(`hermes_secret_compare.py` docstring 所列)为 dashboard、api_server、tui_gateway;实测 `tui_gateway` 无独立 TCP 监听——其 WebSocket 传输 `tui_gateway/ws.py:handle_ws` 挂载在 dashboard FastAPI 的 `/api/ws` 上(`web_server.py:18418-18420`),该 docstring 在此点已过时(实际 import 方为 `web_server.py`、`api_server.py`、`bluebubbles.py`)。

| # | 服务面 | 文件 | 默认 host | 默认端口 | 认证(实测) |
|---|---|---|---|---|---|
| 1 | Dashboard/backend(FastAPI/uvicorn) | `hermes_cli/web_server.py`(20,194 行) | 127.0.0.1 | 9119 | 见 F.1 |
| 2 | OpenAI 兼容 API(aiohttp) | `gateway/platforms/api_server.py` | 127.0.0.1 | 8642 | `bearer_matches(Authorization, API_SERVER_KEY)`;启动守卫:无 key 拒绝启动(含 loopback),且要求 `has_usable_secret(min_length=16)` |
| 3 | 通用 webhook 接收器(aiohttp) | `gateway/platforms/webhook.py` | `None`(双栈全接口!) | 8644 | 每路由 HMAC secret 启动时强制存在;`INSECURE_NO_AUTH` 仅 loopback 允许;host 未设按非 loopback 处理(fail-closed) |
| 4 | BlueBubbles iMessage webhook | `gateway/platforms/bluebubbles.py` | 127.0.0.1 | 8645 | BlueBubbles 服务器密码经 `guid`/`x-guid`/`x-bluebubbles-guid` 头,`constant_time_equals`;`access_log=None` 防密码进日志 |
| 5 | MS Graph 变更通知 webhook | `gateway/platforms/msgraph_webhook.py` | 0.0.0.0 | 8646 | 无 `extra.client_state` 拒绝启动;POST 校验 body `clientState` 常时比较 |
| 6 | WhatsApp Cloud webhook | `gateway/platforms/whatsapp_cloud.py` | 0.0.0.0 | 8090 | GET:verify_token(无则 503);POST:`X-Hub-Signature-256` HMAC(app_secret,无则 503) |
| — | MCP server(stdio) | `mcp_serve.py`(990 行) | — | — | 无网络认证(信任 = 本地进程派生;`hermes mcp serve`);10 个工具(conversations/messages/events/permissions/channels_list) |
| — | TUI gateway(stdio JSON-RPC) | `tui_gateway/server.py`(16,003 行) | — | — | Ink TUI 经 stdio;网络暴露委托给 dashboard `/api/ws` |

### F.1 Dashboard 认证模型(`hermes_cli/web_server.py` + `hermes_cli/dashboard_auth/`)

两种模式按 bind 决定:**loopback token 模式** vs **gated(provider)模式**。

- `should_require_auth(host)`:`host not in {"localhost","127.0.0.1","::1"}` 即需认证——RFC1918/LAN bind 一律按公网对待。`--insecure`/`allow_public` **被接受但忽略**(仅告警;2026-06 hermes-0day 事件后不再绕过)。
- **fail-closed 拒绝绑定**:需认证但零 auth provider 注册且 owner 注册未开放 → `SystemExit("Refusing to bind dashboard to {host}...")`。
- **临时会话 token**:`_SESSION_TOKEN = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)`(`web_server.py:311`);仅注入到所服务 SPA 的 HTML(`window.__HERMES_SESSION_TOKEN__`,gated 模式不注入);请求头 `X-Hermes-Session-Token`(兼容 `Authorization: Bearer`)。
- **中间件链**(Starlette 后注册先执行):`_token_auth_seam`(bearer token → provider registry;`/api/cron/fire` 放行 NAS JWT)→ `auth_middleware`(legacy `_SESSION_TOKEN`)→ `_dashboard_auth_gate` → `gated_auth_middleware` → `_plugin_api_runtime_gate` → `host_header_middleware`(DNS-rebinding 防御,GHSA-ppp5-vxwm-4cf7;WS 因不走 HTTP 中间件而单独复检 Host/Origin)。
- **gated 模式公开路径**:`_GATE_PUBLIC_EXACT` = {`/auth/login`, `/auth/callback`, `/auth/password-login`, `/auth/logout`, `/login`, `/api/auth/providers`, `/favicon.ico`, `/manifest.webmanifest`, `/apple-touch-icon.png`, `/hermes-official.png`};前缀 = (`/auth/mobile/`, `/api/mcp/oauth/callback/`, `/assets/`, `/ds-assets/`, `/fonts/`, `/fonts-terminal/`)。未认证 `/api/*` → 401 JSON;HTML → 302 `/login?next=`;单 provider 自动 SSO(带一次性防环 cookie)。
- **免认证 API 路径** `PUBLIC_API_PATHS`(`dashboard_auth/public_paths.py`):`/api/status`、`/api/config/defaults`、`/api/config/schema`、`/api/model/info`、`/api/dashboard/themes`、`/api/dashboard/plugins`、`/api/mobile/v1/handshake`、`/api/managed-nodes/recovery-hook`(自带共享密钥)、`/api/cron/fire`(NAS JWT,`purpose=cron_fire`)。
- **密码登录限速**(`dashboard_auth/routes.py`):每 IP 滑窗 10 次/60s **且** 每账号 20 次/300s(键 `provider:username` casefold);超过 4096 个键坍缩进共享 `_overflow_` 桶(更严);open-redirect 防护 `_validate_post_login_target` 拦 `//`、`/login`、`/auth/`、`/api/`。
- **受信代理 / X-Forwarded-For**(`dashboard_auth/client_ip.py`):`HERMES_TRUSTED_PROXIES` env 优先于 config `dashboard.trusted_proxies`;默认**完全忽略 XFF**、以传输层 peer 为准;peer 受信时自右向左走 XFF,第一个不受信条目即客户端;非法条目终止行走。
- **uvicorn `proxy_headers=bool(auth_required)`**:默认关(loopback 门看真实 peer);仅 gated 模式开(需 X-Forwarded-Proto 决定 cookie Secure)。
- **WS 认证**(`_ws_auth_reason`):gated → `?ticket=`(30s 单次,`ws_tickets.py`)或 `?internal=`(仅服务器自派 PTY 子进程);gated 模式下 legacy `?token=` 一律拒绝;loopback → `?token=` 与 `_SESSION_TOKEN` 常时比较。peer 门:loopback bind + 无认证 ⇒ 仅 loopback peer,`ws.client` 为空 fail-closed 拒绝。关闭码:4401 凭据 / 4403 host-origin / 4408 peer / 4404 chat 禁用 / 4400 bad channel / 1011 spawn 错误。
- **WS keepalive**:loopback bind 禁 ping(事件循环停顿不误杀);公网 bind ping 20s/pong 60s。
- **CORS**:`allow_origin_regex=^https?://(localhost|127\.0\.0\.1)(:\d+)?$`。

### F.2 Dashboard 路由分组(~200 REST 路由 + 5 WebSocket)

| 分组(`/api/...`) | 代表路由 | 用途 |
|---|---|---|
| 媒体/聊天 | `/api/media`, `/api/chat/image-upload` | 聊天上传 |
| 文件 | `/api/files*`, `/api/fs/*` | 文件浏览器;`/api/files/download` 是唯一接受 `?token=` 的 HTTP 路由(`_QUERY_TOKEN_API_PATHS`) |
| Git | `/api/git/*` | 仓库状态/diff |
| Mobile | `GET /api/mobile/v1/handshake`(公开) | 契约:`api_version:1`、`hermes_version`、capabilities(`auth.owner, chat, devices, notifications.apns, profiles, sessions, config`);未认证调用 `profiles` 返回 `[]` |
| 状态 | `GET /api/status`(公开), `/api/system/stats` | 存活探针 / 系统统计 |
| Fleet | `/api/managed-nodes/*`, `/api/managed-installations*` | 受管节点/安装 |
| Curator/learning | `/api/curator*`, `/api/learning/*` | 技能维护与学习 |
| Ops | `/api/portal`, `/api/ops/*`, `/api/gateway/restart\|drain\|start\|stop`, `/api/hermes/update*` | 网关生命周期、自更新 |
| 音频 | `/api/audio/*` | TTS/STT |
| 会话/profile | `/api/actions/{name}/status`, `/api/sessions*`, `/api/profiles*` | 会话 CRUD、profile 管理 |
| 记忆/配置/模型/env | `/api/memory*`, `/api/config*`, `/api/model/*`, `/api/env*`, `/api/providers/*` | 配置编辑含密钥;env 明文查看限速 `_REVEAL_MAX_PER_WINDOW=5`/30s |
| 消息/cron/mcp | `/api/messaging/*`, `/api/cron/*`, `/api/mcp/*` | 平台消息、cron、MCP |
| Pairing | `GET /api/pairing`, `POST /api/pairing/approve\|revoke\|clear-pending` | 消息平台配对码审批(非移动端流);approve 有每平台锁定(429) |
| Webhooks/凭据/技能/工具/分析 | `/api/webhooks*`, `/api/credentials/pool*`, `/api/skills*`, `/api/tools/*`, `/api/analytics/*` | 如名 |
| Dashboard 元/插件 | `/api/dashboard/themes\|theme\|font\|plugins\|plugins/hub`, `/api/agent-plugins/*`, `/api/plugin-providers`;静态 `/dashboard-plugins/{plugin}/{path}` | 主题 + 插件面板;`/dashboard-plugins/*` 静态资源**不认证**(浏览器后缀白名单,不出 `.py`) |
| 插件 API | `/api/plugins/<name>/...` | 每插件 `router` 挂载(见 H) |

**WebSocket(6)**:`/api/console`(PTY 控制台)、`/api/pty`(见 F.3)、`/api/ws`(tui_gateway JSON-RPC 桥,"Drives the same `tui_gateway.dispatch` surface Ink uses over stdio")、`/api/pub`(派生的 `tui_gateway.entry` 回连的发布端)、`/api/events`(事件扇出)、`/api/plugins/collaboration/single/conversations/{id}/hosted-events-ws`(iOS 托管会话事件流)。六者共用 `_ws_auth_reason`。

**SPA 服务**:`mount_spa()`(`web_server.py:18607`);`HERMES_SERVE_HEADLESS=1` 时 catch-all 返回 404 JSON("Headless backend (hermes serve): web UI disabled")。SPA dist 目录 `HERMES_WEB_DIST` 或 `hermes_cli/web_dist`;注入 `window.__HERMES_SESSION_TOKEN__`(仅非 gated)、`__HERMES_AUTH_REQUIRED__`、`__HERMES_BASE_PATH__`(取自 `X-Forwarded-Prefix`)、`__HERMES_DASHBOARD_EMBEDDED_CHAT__`;路径穿越以 `resolve().is_relative_to(WEB_DIST)` 阻断。

### F.3 PTY 桥(dashboard 内嵌真实 TUI)

- `hermes_cli/pty_bridge.py`(286 行):**仅 POSIX**——`_PTY_AVAILABLE = not sys.platform.startswith("win")`(fcntl/termios/ptyprocess;pywinpty 明确"future enhancement")。resize 经 `fcntl.ioctl(TIOCSWINSZ)`,上限 `_MAX_COLS=2000`/`_MAX_ROWS=1000`(规避 WSL2 columns=131072 bug)。关闭序列 SIGHUP→SIGTERM→SIGKILL(进程组,各 0.5s)。
- `/api/pty` WS:query 参数 `resume`/`profile`/`channel`/`fresh`/`attach`;客户端 resize 转义序列本地消化;keep-alive 注册表 `PTY_REGISTRY.attach_or_spawn`,断连保活,30 分钟 TTL 回收。Windows 上提示 "requires a POSIX PTY... Install Hermes inside WSL2"。

### F.4 api_server 路由(OpenAI 兼容)

`/v1/chat/completions`、`/v1/responses`(+id)、`/v1/models`、`/v1/capabilities`、`/api/sessions*`(+messages/fork/chat[/stream])、`/v1/runs`(202 异步)+status/events SSE/approval/stop、`/health`、`/health/detailed`;所有路由镜像于 `/p/{profile}/...`(profile 前缀中间件)。`MAX_REQUEST_BYTES` 10MB;中间件:profile 前缀、CORS、body 限长、安全响应头。并发上限 `gateway.api_server.max_concurrent_runs: 10`(超出 429+Retry-After)。

---

## G. 移动端 Owner API 契约(hermes-ios 客户端)

实现:`hermes_cli/dashboard_auth/owner_mobile.py`(710 行)+ `mobile_device_store.py`(~1,241 行)。

### G.1 路由

| 路由 | 认证 | 功能 |
|---|---|---|
| `GET /auth/mobile/status` | 公开(gate 前缀白名单) | `{registration_open, account_configured, email_verification_required: true, owner_email_configured}` |
| `POST /auth/mobile/registration-code` | 公开 | 向 owner 邮箱发 6 位验证码 |
| `POST /auth/mobile/register` | 公开(带验证码) | 创建 owner 账号 |
| `POST /auth/mobile/token` | 公开(用户名/密码) | 登录 → `{access_token, refresh_token, token_type:"Bearer", expires_at, refresh_expires_at, session_id, device_id, account{username, display_name}}` |
| `POST /auth/mobile/refresh` / `logout` | refresh token | 刷新/注销 |
| `GET /api/mobile/v1/devices`;`DELETE .../{device_id}`;`PUT/DELETE .../{device_id}/apns` | Bearer access token | 设备与 APNs 管理 |
| `GET /api/mobile/v1/handshake` | 公开 | 能力协商(见 F.2) |

### G.2 规则(fail-closed 细节)

- 注册开放条件(全部满足):`HERMES_MOBILE_REGISTRATION_ENABLED` truthy **且** `HERMES_OWNER_EMAIL` 匹配 `^[1-9][0-9]{4,11}@qq\.com$` **且** 尚无 owner(或已 tombstone)。
- 验证码:QQ SMTP SSL(`HERMES_QQ_SMTP_USERNAME`/`HERMES_QQ_SMTP_AUTH_CODE`/`HERMES_QQ_SMTP_HOST`=smtp.qq.com/`HERMES_QQ_SMTP_PORT`=465);TTL 600s;重发冷却 60s(按 IP 且按邮箱);最多试 5 次;摘要 `hmac.digest(_VERIFICATION_PEPPER, f"{email}:{code}", "sha256")`(进程内随机 pepper)。
- 用户名 `^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$`;密码 8–256 字符、禁 NUL。注册写入 config `dashboard.basic_auth = {username, password_hash, password:"", secret: base64(32B), session_ttl_seconds, disabled: False}`。删号后用户名永久 tombstone(重用 → 409)。
- 登录同时消耗每 IP + 每账号限速预算(同 F.1 密码限速器)。**无 QR 配对**——`/api/pairing*` 是消息平台配对功能,与移动端无关。

### G.3 Token 存储(`mobile_device_store.py`)

- DB:`get_hermes_home()/dashboard/mobile-auth.db`(目录 0o700、文件 0o600、WAL、SCHEMA_VERSION=4)。表:`mobile_devices`、`mobile_sessions`(access/refresh hash UNIQUE)、`mobile_refresh_history`、`mobile_refresh_idempotency`、`mobile_apns_tokens`(UNIQUE(device_id, environment, bundle_id))、`mobile_account_deletion_outbox`。
- `ACCESS_TTL_SECONDS=15*60`;`REFRESH_TTL_SECONDS=30*24*3600`;token 形如 `hma_`+token_urlsafe(48) / `hmr_`+token_urlsafe(64);**只存 SHA-256 hex 摘要**。
- `create_session` 撤销同设备旧会话(`replaced_by_login`);拒绝设备跨账号重绑(PermissionError)与 tombstone 用户。`rotate_refresh`:旧 refresh hash 重放 → 撤销整个会话(`refresh_token_replay`)并禁用该设备 APNs。设备 id 8–128 字符 `[A-Za-z0-9._:-]`。
- `OwnerMobileTokenProvider`:`name="owner-mobile"`,`supports_token=True`;验证通过 → `TokenPrincipal(scopes=("dashboard:admin",))`——**有效移动 access token 等于完整 dashboard 管理权限**。
- 另有 `MobileApiKeyProvider`(`mobile_api_provider.py`):`HERMES_MOBILE_API_KEY` 配置时启用,`hmac.compare_digest` 校验,principal `"ios-native"`、scopes `("dashboard:admin",)`,并对 `/api` 前缀开可选 token 认证。

---

## H. Collaboration 插件(群聊/工作流面板 + 移动端扩展 API)

位置:`plugins/collaboration/dashboard/`(`manifest.json` + `plugin_api.py` 14,773 行 + `dist/` 前端)。功能:多 Profile **群聊**("Group chat turns execute the existing Hermes CLI against named profiles")、**rooms**、**Kanban 工作流**(渲染委托给内置 Kanban dashboard API)、移动端写审批恢复环(5s 间隔)。观察到的 connector id:`dbb3-primary`、`pc-primary`。

- **面板注册**:manifest `{"name":"collaboration","label":"群聊与工作流",...,"tab":{"path":"/collaboration","position":"after:chat","hidden":true},"slots":["chat:top"],"entry":"dist/index.js?v=2.1.48","api":"plugin_api.py"}`。`web_server.py` `_discover_dashboard_plugins` 扫描:用户 `~/.hermes/plugins`(需列入 config `plugins.enabled` 才 import Python,GHSA-mcfc-hp25-cjv7)、内置(repo `plugins/`、`memory/`)、项目 `cwd/.hermes/plugins`(仅 `HERMES_ENABLE_PROJECT_PLUGINS`,且从不自动 import Python);`_safe_plugin_api_relpath` 拒绝绝对路径/`..`(GHSA-5qr3-c538-wm9j)。
- **路由**(挂载于 `/api/plugins/collaboration/`,约 54 条):`/connector/health`;`/connector/runs` pull/ack/status/fail/cancel-ack;`/connector/runs/{id}/attachments(+file)`;`/connector/artifacts`;`/connector/cancellations/pull`;`/profiles`;`/route`;`/single/conversations` CRUD + adopt/attachments/record/runtime-session/hosted-events/hosted-events-ws/enqueue/hosted-turns/messages/artifacts;`/rooms` CRUD + messages + `hosted-turns/{turn_id}/cancel`;`/files` CRUD + download;`/mobile/*`(conversations session-state/fork/compress、sessions fork/lineage/context、write-approvals + decision、runtime-runs)。
- **认证**:非 connector 路由走 dashboard 标准链。connector 路由用专用 bearer:token 来自 `HERMES_COLLABORATION_CONNECTOR_TOKEN(_FILE)` 或 `HERMES_COLLABORATION_CONNECTOR_TOKENS(_FILE)`(JSON id→token),回退文件 `/etc/hermes-mobile/connector_token`、`/etc/hermes-agent/collaboration-connector-token`;请求需 `Authorization: Bearer` + `x-connector-id`;校验 `hmac.compare_digest(supplied, expected)`,未配置 → 503 fail-closed。注册 `_ConnectorTokenProvider`(scope `collaboration:connector`)并对 `/api/plugins/collaboration/connector` 前缀启用 token 认证。

---

## I. Cron 子系统存储与调度

- **存储**(`cron/jobs.py`):任务在 `~/.hermes/cron/jobs.json`(实为 `get_hermes_home()/cron/` —— cron **按 profile 隔离**,issue #4707:profile `coder` 的任务存于 `~/.hermes/profiles/coder/cron/jobs.json`,由该 profile 的 gateway 用其 `.env`/`config.yaml`/skills 执行);输出存 `~/.hermes/cron/output/{job_id}/{timestamp}.md`(保留 `cron.output_retention: 50` 份)。jobs.json 读写走跨进程 advisory 锁(fcntl,Windows 退化 msvcrt,都缺则进程内锁)。
- **任务记录字段**(`create_job()` 实测):`id`, `name`, `prompt`, `skills`/`skill`, `model`, `provider`, `provider_snapshot`/`model_snapshot`(#44585:未 pin 的任务在创建时快照解析结果), `base_url`, `script`, `no_agent`(纯脚本任务), `context_from`, `schedule`(解析后 dict,含 `kind`)、`schedule_display`, `repeat{times, completed}`(times=None 永久), `enabled`, `state`(scheduled/paused), `paused_at/paused_reason`, `created_at`, `next_run_at`, `last_run_at/last_status/last_error/last_delivery_error`, `deliver`, `origin`, `enabled_toolsets`, `workdir`;`attach_to_session` 仅显式设置时持久化(缺省回退全局 `cron.mirror_delivery`)。
- **schedule 四种格式**:duration("30m")、"every" 短语、5 字段 cron 表达式(croniter)、ISO 一次性(`kind:"once"`;超过 ONESHOT 宽限窗的过去时刻拒绝创建)。
- **调度器**(`cron/scheduler.py`):gateway 后台线程每 60s 调 `tick()`;文件锁 `~/.hermes/cron/.tick.lock` 保证跨进程单 tick;3 分钟硬中断;错过的任务按半周期 catchup(夹在 120s–2h);执行时 `skip_memory=True`;ticker 心跳文件记录存活。desktop 场景由 dashboard lifespan 起 ticker(`HERMES_DESKTOP=1`)。
- **托管模式**:`cron.provider: "chronos"` 时任务由 NAS 侧 Chronos 服务触发,回调 `/api/cron/fire` 携 NAS 签发 JWT(`expected_audience`,`nas_jwks_url` 为空 = 拒绝一切 token)。
- 其他文件:`executions.py`(执行记录)、`lifecycle_guard.py`、`suggestions.py`/`suggestion_catalog.py`(自动化建议)、`blueprint_catalog.py`(/blueprint 模板)、`scheduler_provider.py`(内置/chronos 切换)。

---

## J. Kanban 子系统存储模型

来源:`hermes_cli/kanban_db.py` 模块 docstring(实测)。

- **共享根板**:默认 DB 位于 `<root>/kanban.db`,`<root>` 是**共享 Hermes 根**(所有 profile 的父目录)。profile 故意坍缩到同一板上——它就是跨 profile 协作原语;`hermes -p <profile>` 派生的 worker 与派单 dispatcher 同板。`<root>/kanban/workspaces/`、`<root>/kanban/logs/` 同理共享。
- **多板(项目)**:`<root>/kanban/boards/<slug>/` 各有自己的 `kanban.db`/`workspaces/`/`logs/`,彼此隔离(worker 只见自己板)。首板 `default` 出于兼容仍在 `<root>/kanban.db`(不在 `boards/default/`)。
- **板解析顺序**(高→低):`connect()/init_db()` 的 `board=` 实参(CLI `--board`、dashboard `?board=`)→ `HERMES_KANBAN_BOARD` env(dispatcher 用于把 worker 钉在任务所在板)→ `HERMES_KANBAN_DB` env(直接钉 DB 文件路径,若指定文件路径本身则最高)→ `<root>/kanban/current` 单行文本文件。
- **派单**:gateway 内 dispatcher(config `kanban.dispatch_in_gateway: True`,间隔 `dispatch_interval_seconds: 60`),失败上限 `failure_limit: 2`,worker 日志轮转 2MiB×1,`max_in_progress_per_profile`,自动分解 `auto_decompose: True`(每 tick 3 个),陈旧派单超时 `dispatch_stale_timeout_seconds: 14400`。
- CLI 动词见 B 表 kanban 行;agent 侧另有 `kanban_*` 工具(toolset `kanban`)。

---

## K. Memory 子系统与写审批

内置 memory + 可插拔外部 provider(一次仅一个外部):`honcho`/`mem0`/`supermemory`/`byterover`/`hindsight`/`holographic`/`openviking`/`retaindb`(`plugins/memory/`)。`MemoryProvider` ABC 钩子:`sync_turn`/`prefetch`/`shutdown`/`post_setup`。

### K.1 内置 MemoryStore(来源:`tools/memory_tool.py`)

- **两个存储**(`class MemoryStore`,memory_tool.py:123):`MEMORY.md`(agent 自身笔记)与 `USER.md`(用户画像),位于 `get_hermes_home()/memories/`(`get_memory_dir()` 动态解析,尊重 profile 切换后的 HERMES_HOME)。条目分隔符 `\n§\n`;限额按**字符**计(模型无关):`memory.memory_char_limit: 2200`(≈800 token)、`memory.user_char_limit: 1375`(≈500 token)(默认值同见 `hermes_cli/config.py` DEFAULT_CONFIG `memory` 节)。
- **冻结快照模式**(缓存纪律的体现):`load_from_disk()` 在会话启动时捕获 `_system_prompt_snapshot`,注入 system prompt;**会话中途的写入立即落盘但绝不改动快照**——前缀缓存全程稳定,下次会话启动才刷新。`format_for_system_prompt(target)` 只返回冻结快照;块头部常量 `MEMORY_BLOCK_HEADERS`(供压缩子系统检测残留块,与 `agent/conversation_compression.py` 锁步)。
- **注入/外泄扫描**:写入(add/replace、batch 内每条)与快照构建两处均过 `tools/threat_patterns.py` 的 `strict` 范围。落盘文件中命中威胁模式的条目在快照里被替换为 `[BLOCKED: <file> entry contained threat pattern(s): …]` 占位符——毒化条目进不了 system prompt,但**活状态保留原文**让用户可见可删(静默丢弃会隐藏攻击)。扫描对磁盘字节确定,快照会话内稳定。
- **三个动作 + 批量**:`add`(append-only;精确重复幂等跳过)、`replace`/`remove`(按 `old_text` 短唯一子串匹配;多个不同条目命中 → 报错要求更具体;全同重复命中 → 操作第一条)。`operations=[…]` 批量路径 `apply_batch()` **all-or-nothing**,预算只对最终结果检查——一次调用即可"先删旧腾位再加新",替代多轮 consolidate-then-retry。`replace`/`remove` 缺 `old_text` 时返回带 `current_entries` 清单的可恢复错误而非死胡同(部分结构化输出客户端会省略可选字段,#43412/#49466)。
- **每轮整合失败上限**:`_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3`——超限后返回终态 "save skipped" 结果,阻止脆弱的 replace/add 循环烧光预算、压住用户回复(#42405);成功写入即重置计数。
- **并发与漂移防护**:读-改-写全程持 `.lock` 旁文件排他锁(fcntl,Windows 退化 msvcrt);写入用临时文件 + `atomic_replace`(读者永远看到完整旧文件或完整新文件)。`replace`/`remove`/batch 前做**外部漂移检测**(`_detect_external_drift`):round-trip 不一致或单条目超过全店限额 ⇒ 判定有外部写者(patch 工具、shell append、手改、姊妹会话),先快照到 `.bak.<ts>` 再**拒绝本次变更**(冲掉会静默丢数据,#26045);`add` 因 append-only 跳过漂移检查。
- **无 agent 场景**:`load_on_disk_store()` 供 gateway、桌面 GUI、裸 CLI `/memory` 处理器构建独立磁盘店,读取 config 的字符限额,保证"无 live agent 时应用已批准写入"与有 agent 时同限额。
- 工具注册:`registry.register(name="memory", toolset="memory", schema=MEMORY_SCHEMA, check_fn=永真)`;schema 顶层 `required: ["target"]`,`target: null` 容错为默认 `"memory"`。

### K.2 Provider 插件系统(来源:`plugins/memory/__init__.py`、`agent/memory_provider.py`、`agent/memory_manager.py`)

- **发现与加载**:扫描①内置 `plugins/memory/<name>/` → ②用户 `$HERMES_HOME/plugins/<name>/`(启发式:`__init__.py` 源码含 `register_memory_provider` 或 `MemoryProvider`);同名**内置优先**。激活由 config `memory.provider` 决定,**一次仅一个外部 provider**(MemoryManager 拒绝注册第二个——防工具 schema 膨胀与后端冲突)。加载协议二选一:`register(ctx)` 函数(经 `_ProviderCollector` 捕获 `register_memory_provider` 调用)或顶层 `MemoryProvider` 子类直接实例化。用户插件 import 进合成命名空间 `_hermes_user_memory.<name>`(不与内置撞 sys.modules)。
- **CLI 子命令**:`discover_plugin_cli_commands()` 只为**活跃** provider 注册其 `cli.py::register_cli(subparser)`(轻量按路径加载,不 import 插件主模块)——所以 `hermes honcho …` 仅在 honcho 激活后存在,新装用 `hermes memory setup honcho`。
- **`MemoryProvider` ABC 全契约**(`agent/memory_provider.py`,新 provider 的实现清单):
  - 必须实现:`name`、`is_available()`(只查配置/依赖,不打网络)、`initialize(session_id, **kwargs)`(kwargs 恒有 `hermes_home`/`platform`,可有 `agent_context`(primary/subagent/cron/flush,非 primary 应跳过写入)、`agent_identity`(profile 名)、`agent_workspace`、`parent_session_id`、`user_id`/`user_id_alt`)、`get_tool_schemas()`(OpenAI function 格式;纯上下文型返回 `[]`)。
  - 核心钩子:`system_prompt_block()`(静态文本)、`prefetch(query, session_id=)`(每次 API 调用前;须快,后台线程做真正召回)、`queue_prefetch()`(turn 结束后排下轮预取)、`sync_turn(user_content, assistant_content, session_id=, messages=)`(turn 后持久化;`messages` 为含工具调用的完整消息表,可忽略)、`handle_tool_call()`、`shutdown()`。
  - 可选钩子(override 即启用):`on_turn_start(turn, message, **kw)`、`on_session_end(messages)`(仅真会话边界)、`on_session_switch(new_session_id, parent_session_id=, reset=, rewound=)`(/resume、/branch、/new、压缩等 session_id 轮换)、`on_pre_compress(messages) -> str`(压缩前抽取,返回文本并入摘要 prompt)、`on_delegation(task, result, child_session_id=)`(父侧观察子 agent;子 agent 本身 `skip_memory=True`)、`on_memory_write(action, target, content, metadata)`(镜像内置 memory 写入)、`get_config_schema()`/`save_config(values, hermes_home)`(`hermes memory setup` 向导;secret 字段进 `.env`,非 secret 走 provider 原生配置文件——新插件**必须**二选一)、`backup_paths()`(声明 HERMES_HOME 之外的状态路径,否则 `hermes backup` 丢失;仅限用户 home 内路径,归档进 `_external/` 子树)。
  - 注:任务书口径中的 "post_setup" 钩子在 ABC 上对应 `save_config`/`get_config_schema` 安装向导族;dashboard 侧另有 `POST /api/tools/{name}/post-setup` 路由(见 F.2)。ABC 本身无名为 `post_setup` 的方法(实测)。
- **MemoryManager 编排**(`agent/memory_manager.py`):run_agent 的单一集成点。`sync_all` **不在 turn 完成路径内联执行**——经单 worker 串行后台执行器派发(曾有配置错误的 Hindsight daemon 内联阻塞 ~298s,把所有界面卡成"running"),turn N 先于 N+1 落地;shutdown 排空超时 `_SYNC_DRAIN_TIMEOUT_S = 5.0`,外部 prefetch 超时 `_EXTERNAL_PREFETCH_TIMEOUT_S = 8.0`。`normalize_tool_schema()` 统一裸 function schema 与已包装的 OpenAI tool 形(双重包装会让 DeepSeek 等严格供应商 400 拒掉**整个**请求,#47707)。
- **共享辅助**:`plugins/memory/query_rewrite.py`(provider 无关的检索 query 重写器,配置在 `auxiliary.memory_query_rewrite`,输入截 4,000 字符、输出截 320);`plugins/memory/config_schema.py`(声明式 provider 配置 schema,按路径加载**绝不**包 import——插件 `__init__.py` 会拉起 agent 运行时,不能进 web server;由通用 `GET/PUT /api/memory/providers/{name}/config` 端点对与桌面通用渲染器消费,新 provider 配置面=纯声明)。

### K.3 内置 provider 目录(来源:`plugins/memory/*/plugin.yaml` 与 `*/README.md`)

| name | 形态 | 依赖 | 关键配置/env | 工具面(README) | 钩子(plugin.yaml) |
|---|---|---|---|---|---|
| byterover | 外部 `brv` CLI,层级知识树 | brv 二进制(curl 安装脚本) | `BRV_API_KEY`(可选云同步);工作目录 `$HERMES_HOME/byterover/` | `brv_query`、`brv_curate` | on_pre_compress |
| hindsight | 知识图谱 + 实体解析;cloud / local embedded(自带 PostgreSQL daemon,5 分钟闲置自停)/ local external 三模式 | pip `hindsight-client>=0.6.1` | `HINDSIGHT_API_KEY`(cloud);embedded 需任一 LLM key;daemon 日志 `~/.hermes/logs/hindsight-embed.log` | (retain/recall 族) | on_session_end |
| holographic | 本地 SQLite FTS5 事实库 + 信任分 + HRR 组合检索 | 无(NumPy 可选) | config `plugins.hermes-memory-store`:`db_path`(默认 `$HERMES_HOME/memory_store.db`)、`auto_extract: false`、`default_trust: 0.5`、`hrr_dim: 1024` | `fact_store`(9 action)、`fact_feedback` | on_session_end |
| honcho | 云端跨会话用户建模(dialectic Q&A);上下文注入**用户消息**而非 system prompt(护缓存),`<memory-context>` 围栏 | pip `honcho-ai` | `HONCHO_API_KEY` 或 OAuth 浏览器登录(`plugins/memory/honcho/oauth_flow.py` 本机回环监听);`HERMES_HONCHO_HOST` env 覆盖 peer 名 | 双向 peer 工具族 | on_session_end |
| mem0 | 服务端 LLM 事实抽取;platform(云)/ self-hosted dashboard(`host`)/ oss(进程内)三模式 | pip `mem0ai>=2.0.10,<3` | `MEM0_API_KEY`;行为配置在 `$HERMES_HOME/mem0.json`(mode/host/user_id/agent_id/rerank) | (search/remember 族) | — |
| openviking | Volcengine 上下文数据库,文件系统式知识层级 | pip `httpx` + 自建 openviking-server | `OPENVIKING_ENDPOINT`(默认 `http://127.0.0.1:1933`)、`OPENVIKING_API_KEY/ACCOUNT/USER/AGENT` | (session/检索族) | on_session_end |
| retaindb | 云 API,Vector+BM25+Rerank 混合检索、7 种记忆类型 | pip `requests` | `RETAINDB_API_KEY`(必需)、`RETAINDB_BASE_URL`、`RETAINDB_PROJECT`(默认按 profile) | `retaindb_profile/search/context/remember/forget` | — |
| supermemory | 语义长期记忆 + 整会话 ingest(每会话一次) | pip `supermemory` | `SUPERMEMORY_API_KEY`;自托管在 `$HERMES_HOME/supermemory.json` 设 `base_url` | (profile/search/记忆工具族) | — |

激活方式统一:`hermes memory setup [<name>]` 向导,或 `hermes config set memory.provider <name>` + secret 写 `~/.hermes/.env`。

### K.4 写审批门(memory/skills 共用;来源:`tools/write_approval.py`、`hermes_cli/account_write_approvals.py`、`hermes_cli/write_approval_commands.py`)

- **开关**:`memory.write_approval` / `skills.write_approval`(布尔,默认 **False** = 门关、自由写入;非法值按 False)。门只对变更动作(add/replace/remove、skill_manage 的 create/edit/patch/write_file/delete/remove_file)生效。设计不变量(模块 docstring 原文):"the gate only ever delays a write for approval, never silently refuses it"——没有配置驱动的"全拒"态,要禁用子系统用 `memory_enabled: false`。
- **决策矩阵**(`evaluate_gate`):门关 → allow;门开 + memory + 交互式 CLI(线程注册了 `tools.terminal_tool` 审批回调)→ **内联 approve/deny**(直接调回调而非 `prompt_dangerous_approval`——后者的 `input()` 回退在 prompt_toolkit 下死锁且把回调异常吞成 deny,#15216;prompt 失败 → 降级为 stage 而非丢弃);门开 + memory + gateway/脚本/后台 → **stage**;门开 + skills(任何来源)→ **stage**(SKILL.md 太大不宜内联审)。写入来源 `current_origin()` 复用 skill-provenance ContextVar:`foreground` / `background_review`(后台自我改进 fork——正是用户抱怨"错误假设自动入库"的来源;后台一律 stage,daemon 线程无法阻塞在交互 prompt 上)。
- **持久层**:`AccountWriteApprovalStore`(SQLite `<HERMES_HOME>/write-approvals.db`,WAL + `synchronous=FULL`)。文件强制 `0o600`(含 `-wal`/`-shm` 旁文件——payload_json 明文含记忆条目与 skill 文件内容);父目录仅在专属子目录时收紧 0o700(默认 DB 在共享 home 根,整根翻 0700 超出本店职权)。表:`account_write_approvals`(状态机 `pending → applying → applied`,或 `rejected/expired/failed`;字段含 revision、decision_token、idempotency_key、effect_key、apply_lease_expires_at、apply_attempts)、`account_write_approval_effects`(效果收据:payload_hash + plan_json + result_json + execution lease)、`_migrations`(遗留 JSON `pending/{memory,skills}/<id>.json` 一次性入库)、`_deletions`(owner 删号墓碑,`ApprovalAccountDeleted` 永久围栏)。每行记 `owner_id` + `profile`(scope 经 ContextVar 传递,回退 env `HERMES_OWNER_ID`/`HERMES_IOS_OWNER_ID`/`HERMES_OWNER_EMAIL`/`local-owner` 与 `HERMES_PROFILE`/`default`),移动端/dashboard 调用者不可枚举他人账户。默认过期 30 天(`expires_in=30*24*60*60`,下限 60s),读路径顺带 `expire_due()`。
- **payload_digest 绑定(混淆代理修复)**:`summary` 是 agent 在 stage 时可控的自由文本,与 `payload` 各存一列——被操纵的 agent 可用无害 summary 掩护恶意 payload 骗取真人批准。因此:①每次读出记录都附 `payload_digest`(对 payload 的 canonical-JSON SHA-256)与 `derived_summary`(**从 payload 本身派生**的服务端描述,UI 应展示这个而非 agent 的 summary);②`claim_decision(decision="approve")` **默认要求**调用方回显它渲染过的 `payload_digest`,常量时间比对(`hmac.compare_digest`),不符抛 `ApprovalPayloadMismatch`;缺失且 env `HERMES_WRITE_APPROVAL_REQUIRE_DIGEST` ≠ "0"(默认 "1")同样拒绝。**reject 永不要求 digest**(丢弃 payload 是安全动作,不得阻塞)。`=0` 仅用于兼容旧客户端,注释原文明言这"reinstates the confused-deputy risk"。
- **裁决与执行的崩溃安全**:`claim_decision` 是 CAS(绑 `expected_revision`)+ `Idempotency-Key`(≤256 字符;同 key 同决定 → 幂等重放,同 key 异决定 → 409 冲突);approve 授予 60s apply 租约(`_APPLY_LEASE_SECONDS`)。`execute_effect` 以 `effect_key` 为副作用身份:prepare 阶段先持久化 plan_json,再发布 execution lease + 后台**心跳线程**续租(间隔 ≈ 租约/3,上限 5s),apply 回调执行后写 result_json 收据——收据一旦落库,租约恢复只重放收据**不再执行回调**。进程崩溃后 `claim_recoverable_applies()`/`recoverable_apply_scopes()` 认领过期租约续跑(dashboard 启动 lifespan 补跑,见 ARCHITECTURE §4.4);`finish_apply` 终态化。`delete_owner` 先立墓碑再清行,有活跃 execution lease 时抛 `ApprovalDeletionInProgress` 让调用方等租约排空。
- **审批面**:①斜杠命令(CLI 与 gateway 共用 `hermes_cli/write_approval_commands.py`):`/memory`、`/skills` 裸命令显示门状态+待审清单;子命令 `pending`、`approve|apply <id|all>`、`reject|deny|drop <id|all>`、`diff <id>`(仅 skills,完整 unified diff)、`approval|mode <on|off>`。approve 路径经 `hermes_cli/account_write_approval_apply.py` 的 `prepare_write_approval`/`apply_write_approval` 收敛适配器(before/after 摘要;目标在 stage 与 approve 之间被改动 → 拒绝"changed after approval was claimed";目标已等于 after → 幂等成功;成功后 live MemoryStore `load_from_disk()` 刷新),import 失败才回退无守卫直写。②移动端/dashboard:collaboration 插件 `GET /mobile/write-approvals`、`POST /mobile/write-approvals/{id}/decision`(见 H 与 API-HTTP §3.5)。③CLI:`hermes memory` 子命令(B 表)。
- skills 侧配套:`skill_gist()`(启发式一行摘要,create/edit 取 frontmatter description)与 `skill_pending_diff()`(create 给全文,edit/patch/write_file 给对现盘的 unified diff)。

---

## L. Agent 核心(会话循环)细节

### L.1 模块布局(来源:`agent/conversation_loop.py` 模块 docstring)

`run_conversation()`(5,919 行文件的主体,自述"roughly 3,900-line body")从 `run_agent.AIAgent` 抽出,第一参数是 `agent` 实例,经属性访问其状态;`AIAgent.run_conversation` 是薄转发器。生产代码/测试在 `run_agent` 上打补丁的符号(`handle_function_call`、`_set_interrupt`、`OpenAI`…)经 `_ra()` 间接解析,补丁契约保持。周边协作模块:`agent/turn_context.py`(每轮 prologue,902 行)、`agent/turn_retry_state.py`(单次尝试恢复簿记)、`agent/iteration_budget.py`(线程安全预算)、`agent/turn_finalizer.py`(收尾)、`agent/chat_completion_helpers.py`(非流式调用/请求组装/fallback 激活,3,844 行)、`agent/error_classifier.py`(1,698 行)、`agent/tool_executor.py` + `agent/tool_dispatch_helpers.py`(工具批执行)、`agent/memory_manager.py`(K.2)。

### L.2 turn 生命周期

1. **MoA 解码**:`user_message` 可能携带 MoA 编码负载,`decode_moa_turn` 剥出;解码失败**记警告继续**(旧行为静默吞错,表现为"MoA 神秘失效")。
2. **Prologue**(`build_turn_context`,一次性副作用全在此):stdio 守卫、重试计数复位、用户消息脱敏(surrogate 清理)、todo/nudge 水合、system prompt **restore-or-build**(`_restore_or_build_system_prompt`:恢复会话时优先复用持久化 prompt 保缓存,与运行时不匹配才重建)、预检压缩、`pre_llm_call` 插件 Hook、外部记忆 prefetch、崩溃恢复持久化。返回 `_ctx`(messages、turn_id、current_turn_user_idx、should_review_memory 等 locals)。
3. **外层循环**:`while (api_call_count < agent.max_iterations and iteration_budget.remaining > 0) or agent._budget_grace_call`。`max_iterations` 来自 config `agent.max_turns`(默认 90;gateway 侧经 `HERMES_MAX_ITERATIONS` env 桥接);`IterationBudget` 线程安全 consume/refund——`execute_code` 程序化工具调用与压缩重启等会 `refund()`;每个子 agent 有**独立**预算(`delegation.max_iterations` 默认 50),父+子总量可超父上限。grace call:预算耗尽后再给模型一次收尾调用。每次迭代:checkpoint 管理器 `new_turn()`(每迭代最多一次快照)、中断检查、`step_callback`(gateway agent:step 事件,附上一批工具的 name/arguments/result)、skill nudge 计数、**pre-API /steer 排水**(把排队的 steer 文本注入最近 tool 消息;多模态注入失败时**重新排队**留给 post-tool 排水,而非标记已注入丢掉用户输入)。
4. **codex_app_server 旁路**:`api_mode == "codex_app_server"` 时整轮转交 `agent._run_codex_app_server_turn`(codex CLI 子进程运行时),默认 Hermes 路径完全绕过。

### L.3 API 调用与重试策略(内层循环)

- **结构**:`while retry_count < max_retries`;`max_retries = agent._api_max_retries` ← config `agent.api_max_retries`(默认 3,OpenAI SDK 自身的 max_retries=2 低层重试在其下叠加);env 面板:`HERMES_API_RETRY_DELAY_SECONDS`(0-600s 附加延迟)、`HERMES_API_RETRY_STATUS_LIVE`(重试时发"正在思考"状态)、`HERMES_API_RETRY_CLIENT_ERRORS`(4xx 也重试)(来源:`agent/agent_init.py:1600-1610`)。`max_compression_attempts = 3`。
- **每次尝试的一次性恢复守卫**(`TurnRetryState`,每外层迭代新建;取代散落的 ~16 个布尔 locals):per-provider OAuth/凭据刷新(codex/anthropic/nous/copilot/vertex + nous 付费 entitlement)、格式恢复(thinking-signature 剥离、invalid-encrypted-content、图片缩边重试、多模态 tool content 剥离、OAuth 1M-beta 头、llama.cpp grammar 回退)、传输恢复(primary_recovery、429)、auth 失败 failover(401/403 刷新失败后升级到 fallback 链,单次尝试内不循环)。
- **重启信号**(尝试后由外层读):`restart_with_compressed_messages`(上下文超限 → 压缩重建 messages;**计入 retry 上限**防"压了但仍不够"死循环;退还预算并重锚 `current_turn_user_idx`——陈旧索引会把本轮 prefetch 注进历史消息、分叉重放前缀)、`restart_with_length_continuation`(输出被截 → 输出预算按 2^n 递增,floor 为请求原 cap、上限 32,768)、`restart_with_rebuilt_messages`(内容过滤流停滞如 MiniMax "new_sensitive" → 回滚半截内容、切 fallback 重发,#32421,退预算)。
- **请求组装**:`_reapply_reasoning_echo_for_provider`(fallback 后按当前供应商补 reasoning_content 回显垫片——DeepSeek/Kimi/MiMo 拒收缺失者)→ `_build_api_kwargs` → 强制 ASCII 载荷清洗(部分供应商)→ codex preflight → Copilot `x-initiator: user`(用户轮首个调用计 premium 请求,#3040)→ `llm_request` 中间件(可改写)→ `pre_api_request` Hook(观察;**失败记警告不再吞**)→ `HERMES_DUMP_REQUESTS` 调试倾印。
- **流式 vs 非流式**:`stream_callback` 存在(TTS/显示消费者)且未被 `_disable_streaming` 关闭时 `_use_streaming = True`,走 `agent._interruptible_streaming_api_call(...)`;MoA 轮由 `MoAChatCompletions.create()` 内部处理(references 跑完由 aggregator 流式返回)。非流式走 `chat_completion_helpers` 的可中断调用。流停滞检测:`HERMES_STREAM_STALE_TIMEOUT` / `HERMES_API_CALL_STALE_TIMEOUT`;本地端点(Ollama/oMLX/llama-cpp)有限顶棚 `agent.local_stream_stale_timeout: 900`(env `HERMES_LOCAL_STREAM_STALE_TIMEOUT` 覆盖)取代旧的无限禁用。
- **供应商特判**:Nous Portal 速率限制守卫(其它会话已记录限流 → 本轮直接跳过 API 调用尝试 fallback,不再加深 RPH 坑;无 fallback → 带剩余时间的失败返回);zai coding 过载抬高重试顶棚;Cloudflare 源站错误专用延迟;自适应 429 退避;通用 `jittered_backoff`(错误恢复 base 2s/max 60s;无效响应 base 5s/max 120s)。退避等待期间**每 200ms 轮询中断**、每 ~30s `_touch_activity`(喂 gateway 不活动监视)。
- **错误分类**:`classify_api_error` → `FailoverReason` 驱动结构化恢复(auth/限流/上下文超限/输出截断/内容策略…);HTTP 200 内容策略拒绝、thinking-budget 耗尽、Bedrock SDK 流失败、图片被拒恢复、Anthropic Sonnet 长上下文档位门、无效加密 reasoning 重放等各有专段(行号锚点:1852、1988、2697、2788、3109、3152、3270、3337、3468…)。

### L.4 工具分发(来源:`run_agent.py:6186` `_execute_tool_calls` + `agent/tool_executor.py`)

- **分段计划器**:`_plan_tool_batch_segments` 把一批 tool_calls 切成最大连续**并行安全**段(只读工具、互不重叠的文件目标、显式 opt-in 的 MCP 工具)与**顺序屏障**(交互式/不安全/未识别工具)。单调用走顺序路径;全并行段走 `execute_tool_calls_concurrent`(并发超时 `HERMES_CONCURRENT_TOOL_TIMEOUT_S`);混合批按发射顺序逐段执行(安全子集仍并发,副作用顺序保持)。
- **委派特例**(`_dispatch_delegate_task`):顶层模型发起的 delegate_task **一律后台化**(立即返回 handle,子 agent 结果作为新消息回注;schema 的 `background` 参数被有意忽略);深度 >0 的 orchestrator 子 agent 保持同步(需要在自己轮内合成 worker 结果,且不拥有异步结果可路由回的 gateway 会话)。
- 工具执行期 `agent._executing_tools = True` 放行 `_vprint`;单工具入口 `_invoke_tool` → `agent.agent_runtime_helpers.invoke_tool`(工具请求中间件、pre-tool 阻断检查)。

### L.5 响应处理、空响应恢复与退出

- **归一化**:`transport.normalize_response(response)`(anthropic_messages 时按 OAuth 决定 strip_tool_prefix);llama-server 等把 content 返回成 dict/list → 展平为字符串。post-call:`post_api_request` Hook(失败记警告)、工具循环 guardrails(`tool_loop_guardrails` 配置的软警告/硬停,`_turn_exit_reason="guardrail_halt"`)。
- **无 finish 内容的恢复链**(带专段注释):部分流恢复(`PARTIAL_STREAM_STUB_ID` 桩)、回退上一轮内容、post-tool 空响应 nudge、thinking-only prefill 续写、空响应重试 → 重试耗尽切 fallback → `empty_response_exhausted`。
- **验证门**:verify-on-stop(config `agent.verify_on_stop: "auto"`)扣住已合成答案继续验证时,答案存入 `_pending_verification_response`;若后续预算耗尽,finalizer **原样启用该答案**并只在其确实流式预览过时标记 `_response_was_previewed`(#65919 响应丢失阻断器)。kanban worker 终局守卫:必须以 `kanban_complete`/`kanban_block` 收尾,叙述式 stop 先 nudge(`finish_reason="kanban_terminal_required"` 合成消息对)。
- **外层异常分类**(#66267):遍历 traceback 模块名——命中 `_LOCAL_PROCESSING_MODULES`(agent_runtime_helpers/message_content/message_sanitization/chat_completion_helpers)且未进 `_API_CALL_MODULES` ⇒ **确定性本地 bug,立即停**(重试只会烧预算);否则按 API 错误近上限退出。已 append 的 assistant tool_calls 若缺 role=tool 回执,统一补错误结果(维持 API 对每个 tool_call_id 有回应的要求);traceback 以 `logger.exception` 进 agent.log + errors.log。
- **退出原因目录**(`_turn_exit_reason`,诊断用):`interrupted_by_user`、`budget_exhausted`、`ollama_runtime_context_too_small`、`interrupted_during_api_call`、`all_retries_exhausted_no_response`、`guardrail_halt`、`partial_stream_recovery`、`fallback_prior_turn_content`、`empty_response_exhausted`、`text_response(finish_reason=…)`、`local_processing_error(…)`、`error_near_max_iterations(…)`、`max_iterations_reached(…)`(finalizer 补写)。
- **取消语义**:`agent._interrupt_requested` 在循环顶、退避等待(200ms 粒度)与流式读取中检查;等待模型时被取消发出以 `INTERRUPT_WAITING_FOR_MODEL_PREFIX`("Operation interrupted: waiting for model response (")开头的状态串——ACP/TUI 按前缀识别为取消元数据而非正文;`close_interrupted_tool_sequence` 为半途 tool 序列补桩维持 user/assistant 交替;返回 `{"interrupted": True, "completed": False}`。
- **Finalizer**(`agent/turn_finalizer.py::finalize_turn`):预算耗尽兜底(优先启用被验证门扣住的答案;否则 `_handle_max_iterations` 注入总结请求做**一次**去工具的额外调用);kanban worker 预算耗尽 → `_record_task_failure(outcome="timed_out", release_claim, end_run)` 计入派发器连续失败熔断(#29747);判定 completed;此后触发会话持久化、记忆 sync(K.2 的后台 worker)与后台自我改进 review fork(`agent._spawn_background_review`,run_agent.py:1643;finalizer 内 L595 调用)。

### L.6 文件安全护栏(来源:`agent/file_safety.py`,754 行;工具与 ACP 垫片共用)

- **写拒绝分类器** `_classify_write_denial(path) -> 'credential' | 'session_state' | 'safe_root' | None`,消费方 `is_write_denied` / `get_write_denied_error(verb=)`:
  - `credential`:精确路径集 `build_write_denied_paths`(`~/.ssh/{authorized_keys,id_rsa,id_ed25519,config}`、活动 profile 与根两级 `.env`(#15981)与 `.anthropic_oauth.json`、`.netrc`/`.pgpass`/`.npmrc`/`.pypirc`/`.git-credentials`、`/etc/{sudoers,passwd,shadow}`)+ 前缀集 `build_write_denied_prefixes`(`.ssh/`、`.aws/`、`.gnupg/`、`.kube/`、`.docker/`、`.azure/`、`.config/gh`、`.config/gcloud`、`/etc/sudoers.d`、`/etc/systemd`)+ HERMES_HOME/根下 `mcp-tokens/`、`pairing/`。
  - `session_state`:`state.db` 与 `sessions/`(应用自有状态;改写会伪造对话历史、破坏 resume/压缩)。此前这两支错误地返回布尔 True,落到凭证文案——拦截一直正确、**理由**才是错的(本次修正)。
  - `safe_root`:设置 `HERMES_WRITE_SAFE_ROOT`(os.pathsep 分隔多根)后,根外一律拒绝。
  - **大小写折叠**(`_fold` = `os.path.normcase` + `casefold`,双向应用于路径与清单):`realpath` 不做大小写归一,macOS APFS/大小写不敏感卷上 `~/.SSH/authorized_keys` 曾能绕过精确匹配、实际写入真实 `~/.ssh/authorized_keys`(提示注入 → 植入 SSH 公钥)。无条件折叠(不做平台猜测):只可能**多拒**,误伤面是无人合法书写的 `~/.SSH/...`。
- **读拒绝** `get_read_block_error` / `raise_if_read_blocked`(#57698 统一 chokepoint):①`skills/.hub` 缓存(提示注入载体);②凭据库精确名 `auth.json`、`auth.lock`、`.anthropic_oauth.json`、`.env`、`webhook_subscriptions.json`、`auth/google_oauth.json`、`cache/bws_cache.json`(Bitwarden 明文缓存,#31968 引入时漏加)——同样折叠比较;③`mcp-tokens/` 前缀;④全盘任意位置的项目 `.env` 家族(`.env`、`.env.local/.development/.production/.test/.staging`、`.envrc`;`.env.example` 是替代品)。**明示非安全边界**:terminal 工具同 OS 用户可 `cat` 绕过;价值在让守规模型停手 + 留可见审计痕迹。
- **跨 profile 软护栏**:`PROFILE_SCOPED_AREAS = ("skills", "plugins", "cron", "memories")`;`classify_cross_profile_target` 判定写目标属于哪个 profile(`<root>/<area>` = default,`<root>/profiles/<name>/<area>` = 具名),与活动 profile 不符 → `get_cross_profile_warning` 返回模型可见告警,要求用户确认或带 `cross_profile=True` 重试(2026-05 hermes-security profile 误改双份 skills 事故)。
- **沙箱镜像护栏**(#32049):`classify_sandbox_mirror_target` 按路径形状识别 `…/sandboxes/<backend>/<task>/home/.hermes/…`(host 侧视角);`classify_container_mirror_target(path, mirror_prefix)` 覆盖容器内 bind-mount 剥掉前缀的情形(file_tools 在 docker+persistent 后端传入活动镜像前缀)。两者都警示"写的是宿主进程永不读取的镜像副本",bypass 复用 `cross_profile=True`。同为 defense-in-depth,非安全边界。

---

## 附:文档状态与未尽事项

- D 节(73 个工具 / 29 个注册键)与 E 节(29 个 adapter 能力矩阵)已补全,数据由 AST 扫描代码得出而非抄录既有文档 —— 过程中发现并订正了一处 toolset 键清单与实际注册不符的文档错误(见 D.1 勘误框)。两节的表若与代码不一致,以 `registry.register(...)` 实参与 adapter 类定义为准。
- 运维视角(部署形态、端口、配置总表、HERMES_* 环境变量全量清单、备份与故障排查)见姊妹文档 [`OPERATIONS.md`](OPERATIONS.md);本月安全/稳定性变更见 [`CHANGES-2026-07.md`](CHANGES-2026-07.md)。
- D/E 工具与平台能力专项已按注册表和 adapter AST 补齐；其余 `待确认` 标注均为原样保留的未证实项,修订时以代码为准。

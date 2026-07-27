# Hermes Agent 后端架构设计文档（ARCHITECTURE）

**状态**：描述性文档，基于 2026-07-26 的源码核对撰写。度量数字（依赖边、延迟导入数等）引自 [`../architecture/layering.md`](../architecture/layering.md)，该文档与 `tests/architecture/baselines/` 不一致时以 baselines 为准；本文不重复其表格，只引用结论。
**配套文档**：[`API-HTTP.md`](API-HTTP.md)（HTTP/WS 接口全量清单）、[`API-EXTENSION.md`](API-EXTENSION.md)（扩展点契约）、仓库根 `AGENTS.md`（贡献者守则，本文多处引用其硬性纪律）。

---

## 1. 系统概览

Hermes 是一个个人 AI Agent 后端（Python，约 70 万行，测试约 1.7 万个），同一套 agent 核心通过多个「表面」（surface）暴露：

- **CLI/REPL**（`hermes chat`）、**TUI**（`hermes --tui`，Node Ink 前端）、**Web Dashboard**（`hermes dashboard`）、**桌面端**（Electron，复用 headless dashboard 后端）、**iOS 原生端**（经 dashboard 的 mobile 门面）;
- **聊天平台**（Telegram / Slack / Discord / WhatsApp / 飞书 / 企业微信 / Google Chat / Teams / LINE / iMessage 等，由 gateway 常驻进程承载）;
- **程序化接口**（OpenAI 兼容 REST、ACP stdio、MCP stdio、本地凭据代理）;
- **定时任务**（cron，本地 ticker 或 Chronos 托管唤醒）。

核心设计约束（AGENTS.md 明文纪律）：**提示词缓存是神圣的**——system prompt 必须字节级稳定，消息严格 user/assistant 交替；一切会破坏缓存前缀的改动默认拒绝。

---

## 2. 包地图（Package Map)

### 2.1 六大包 + 根模块

| 目录/模块 | 目标层级（layering.md） | 职责 |
|---|---|---|
| `agent/` | L2 | 模型循环（`run_agent.py`、`conversation_loop.py`）、上下文引擎/压缩、脱敏、`file_safety.py`（读写护栏）、`memory_provider.py`/`memory_manager.py` |
| `tools/` | L3 | 全部工具实现 + `registry.py`（注册/发现/分发）+ `toolsets.py` + `approval.py`（危险命令审批的单一事实源）+ `write_approval.py`（写审批门）+ MCP 客户端 |
| `gateway/` | L4 | 常驻编排器 `run.py`（23,097 行）：平台适配器契约（`platforms/base.py`）、消息路由、会话路由、看板派发、cron ticker、`platforms/api_server.py`（OpenAI 兼容 REST，aiohttp） |
| `hermes_cli/` | **L5 + L1 双层** | L5：`main.py`（约 60 个子命令）、`web_server.py`（FastAPI dashboard，20,194 行）、`dashboard_auth/`（认证栈）、业务数据层（`account_write_approvals.py`、`kanban_db.py`、`projects_db.py`、`active_sessions.py` 等）;L1：`config.py`（9,509 行，**配置权威**）与 `managed_scope.py` |
| `plugins/` | L4' | 平台适配器、记忆后端、model-providers、dashboard 插件（collaboration 等）、观测、workflows……详见 API-EXTENSION.md |
| `tui_gateway/` | L5 | TUI 的 Python 侧 JSON-RPC 服务器（`server.py`，16,003 行，134 个 RPC 方法），经 stdio 或 dashboard 的 `/api/ws` 提供服务 |
| `cron/` | — | `jobs.py`（`~/.hermes/cron/jobs.json` 存储 + 跨进程文件锁）、`scheduler.py`、blueprint 目录 |
| `providers/` | — | `ProviderProfile` 抽象与注册表（`base.py`、`__init__.py`）;新供应商实体已迁往 `plugins/model-providers/` |
| `acp_adapter/` | — | ACP（Agent Client Protocol）stdio 适配器，「stdio-only, local-trust」 |
| 根 `cli.py` | L0/入口 | 轻量入口与 `load_cli_config()`（历史遗留的第二配置装载器） |
| 根 `hermes_state.py` | L0 | `SessionDB`：`~/.hermes/state.db`（SQLite WAL + FTS5），会话/消息/用量/路由/压缩锁/异步委派的**权威存储** |
| 根 `utils.py` | L0 | `atomic_replace` / `atomic_json_write` / `atomic_yaml_write` 等原子写原语 |
| 根 `hermes_constants.py` | L0 | `get_hermes_home()` / `display_hermes_home()` / profile 根解析 |
| 根 `hermes_secret_compare.py` | L0 | 全仓库唯一的常量时间秘密比较模块（78 行，故意零第三方依赖），三个 HTTP 面共用 |

### 2.2 `hermes_cli` 的双重身份（理解全仓依赖图的钥匙）

layering.md 的原话：**"`hermes_cli` is two layers wearing one package name."** 它同时是最顶层入口（L5）与所有下层都要的配置权威（L1）。`agent → hermes_cli` 的 188 条、`plugins → hermes_cli` 的 217 条 import 大多在够 L1 那一半。layering.md 判定「把配置权威从入口包中抽出」是**单点杠杆最大**的重构。

### 2.3 分层的真实状态

目标分层与实测差距、每条边的宽度、延迟导入棘轮、裸配置读取允许清单、私有符号导入清单，**全部以 [`../architecture/layering.md`](../architecture/layering.md) 为准**，此处只给结论：30 条可能的有向依赖边中 25 条实际存在（2,313 条跨包 import）；仅有的 5 个零格（如「除 `hermes_cli` 外无人 import `tui_gateway`」「`tui_gateway` 从不触碰 `plugins`」）被棘轮当作硬不变量守住。

---

## 3. 进程拓扑

### 3.1 进程清单

```
                                  ┌────────────────────────────────────────┐
   浏览器 SPA / iOS App ──HTTP/WS──▶ dashboard 进程 (hermes dashboard/serve) │
                                  │  FastAPI :9119  hermes_cli/web_server  │
                                  │  ├── /api/* REST + 5 个 WS 端点          │
                                  │  ├── /api/pty ──spawn──▶ hermes --tui 子进程
                                  │  ├── /api/ws  ──in-proc─▶ tui_gateway.server
                                  │  └── (HERMES_DESKTOP=1 时) cron ticker 线程
                                  └────────────────────────────────────────┘
   Telegram/Slack/... ──长连接/webhook──▶ gateway 常驻进程 (hermes gateway start)
                                  │  gateway/run.py::GatewayRunner
                                  │  ├── 平台适配器们(各自 aiohttp 监听或长连接)
                                  │  ├── api_server 平台 (aiohttp :8642, OpenAI 兼容)
                                  │  ├── cron ticker 线程 + kanban 派发
                                  │  └── PID: ~/.hermes/gateway.pid, 状态: gateway_state.json
   终端用户 ──stdio──▶ hermes chat (单次进程内跑 agent 循环)
   Node Ink TUI ──stdio JSON-RPC──▶ tui_gateway (hermes --tui 进程内)
   Electron 桌面 ──spawn──▶ hermes serve (headless dashboard) ──WS /api/ws──▶ tui_gateway
   IDE (Zed 等) ──stdio──▶ acp_adapter (hermes acp)
   MCP 客户端 ──stdio──▶ mcp_serve.py (hermes mcp serve)
   OpenAI 客户端 ──HTTP──▶ hermes proxy (aiohttp 127.0.0.1:8645, 换 Authorization 转发)
   Chronos NAS ──HTTPS + JWT──▶ /api/cron/fire (dashboard 或 gateway 任一面)
```

要点：

- **CLI/REPL**：`hermes chat`（`hermes_cli/main.py`，`_AGENT_COMMANDS = {None, "chat", "acp", "rl"}` 才加载 agent 重依赖）。
- **gateway 守护进程**：唯一长期持有平台连接的进程。PID 文件 `~/.hermes/gateway.pid`，运行态 `gateway_state.json`，锁目录 `gateway-locks/`（Windows 用 1MB 偏移的字节范围锁，保证读者可读）。
- **dashboard / serve**：同一 `start_server()`（默认端口 9119）。桌面端 Electron 以 `hermes serve --host 127.0.0.1 --port 0`（`HERMES_SERVE_HEADLESS=1`，不挂 SPA）拉起后端，经 `/api/ws` 用 JSON-RPC 直连 `tui_gateway`。
- **TUI 双通道**：`hermes --tui` 时 Node Ink 前端与 `tui_gateway/server.py` 走 **stdio 换行分隔 JSON-RPC**（首帧 `gateway.ready`）；dashboard 的 Chat 页则通过 `/api/pty` WebSocket + ptyprocess 内嵌**真实 TUI 子进程**（子进程凭 `?token=`/`?internal=` 回连 `/api/ws`、`/api/pub` 上报事件）。
- **cron 的两个 ticker**：gateway 内 `_start_cron_ticker` 线程；桌面模式下 dashboard 进程再起 `_start_desktop_cron_ticker` 线程——两者靠 `~/.hermes/cron/.tick.lock` 文件锁避免双触发。cron 硬性不变量（AGENTS.md）：单次运行 3 分钟硬中断、`skip_memory`。
- **平台 webhook 监听器**：webhook / whatsapp_cloud / msgraph / bluebubbles / teams / feishu / LINE / SMS / WeCom / raft 等适配器各自起 aiohttp 监听（全部运行在 gateway 进程内）；另有 Node 侧 sidecar（photon、whatsapp-bridge）。全仓网络监听器共 21 个，完整清单见 API-HTTP.md §7。

### 3.2 进程间共享状态（文件即总线）

| 共享物 | 路径 | 写者 | 并发协议 |
|---|---|---|---|
| 配置 | `~/.hermes/config.yaml` | dashboard、gateway（/reasoning、/fast 持久化）、平台适配器回写、CLI | `config_write_lock()` 跨进程建议锁（锁 `<config>.lock` 旁文件，见 §5.1） |
| 会话权威库 | `~/.hermes/state.db` | 所有跑 agent 的进程 | SQLite WAL |
| 写审批 | `~/.hermes/write-approvals.db` | agent 进程 stage、dashboard/移动端裁决、applier | SQLite + CAS + 租约 |
| 移动认证 | `~/.hermes/dashboard/mobile-auth.db` | dashboard | SQLite |
| cron 任务 | `~/.hermes/cron/jobs.json`（每 profile 一份） | 两个 HTTP 面 + CLI + ticker | 跨进程建议文件锁 + `.tick.lock` |
| 活跃会话租约 | `hermes_cli/active_sessions.py` 管的 JSON | 各 surface | 租约文件（含 `max_concurrent_sessions` 上限） |
| gateway 存在性 | `gateway.pid` / `gateway_state.json` / `gateway-locks/` | gateway | PID + 字节范围锁 |
| 模型凭据 | `~/.hermes/auth.json` | `hermes_cli/auth.py` | 跨进程文件锁 |
| 协作房间/单聊 | `~/.hermes/collaboration/rooms.json`、`single.json` | collaboration 插件 API | 进程内 `_STATE_LOCK`（跨进程为**待确认**——该存储假定只有 dashboard 一个写进程） |
| gateway 会话路由镜像 | sessions 目录下 legacy `sessions.json` | gateway `SessionStore` | 从 state.db 导入/修剪/重指 |

---

## 4. 运行时主流程

### 4.1 聊天平台消息 → gateway → 模型响应

```
平台 SDK/webhook → 适配器构造 MessageEvent → await self._message_handler(event)
  └─ GatewayRunner._handle_message (run.py:9947)
       ├─ 【卫兵①】适配器基类 _pending_messages 队列（会话忙时排队）
       ├─ 【卫兵②】runner 内联拦截 /stop /new /queue /status /approve /deny
       │   （AGENTS.md 陷阱原文："The gateway has TWO message guards —
       │     both must bypass approval/control commands"：两个卫兵都必须
       │     放行审批/控制命令，否则用户会被自己待审批的命令锁死）
       ├─ pre_gateway_dispatch Hook（可 skip/rewrite/allow）
       ├─ SessionStore 定位/新建会话（state.db；auto-continue 新鲜度窗口
       │   由 agent.gateway_auto_continue_freshness 经环境变量桥接）
       └─ _handle_message_with_agent (11956)
            └─ _run_agent (18733) → 线程池 _run_in_executor_with_context (16393)
                 └─ agent/run_agent.py 会话循环：
                      llm_request 中间件（可改写请求）→ pre_api_request 观察者
                      → Provider HTTP 调用 → post_api_request → 工具调用循环
                      → memory sync（后台单 worker 执行器）
            → 适配器.send(chat_id, content) → SendResult（失败带 retryable/retry_after）
```

### 4.2 工具调用与危险命令审批

`tools/approval.py` 是「危险命令系统的单一事实源」：模式检测（DANGEROUS_PATTERNS）→ 按 `session_key` 的会话内审批状态 → 智能审批（辅助 LLM）→ `config.yaml` 永久允许清单。

- **CLI 交互态**：`prompt_dangerous_approval`（L2273）终端内联提问。
- **gateway 异步态**：审批挂入 pending 队列 `submit_pending(session_key, approval)`（L2115），向平台发送审批提示消息，`_await_gateway_decision`（L3058）阻塞等待用户在聊天里回 `/approve` 或 `/deny`（这正是 4.1 中两个卫兵必须放行控制命令的原因）。
- **YOLO 冻结**：`_YOLO_MODE_FROZEN` 在 import 时定格——树内注释：每次调用都读 `os.environ` 会让任何 skill 经改环境变量绕过全部审批（提示注入提权路径）。会话/turn/tool_call 身份用 contextvars 传递（GHSA-96vc-wcxf-jjff：修复 ACP 线程池下 `HERMES_INTERACTIVE` 环境变量竞态）。

### 4.3 Dashboard API 请求过认证中间件栈

Starlette 语义：**后注册者最外层、最先执行**。`web_server.py` 的注册顺序（L331→L645）与实际执行顺序相反：

```
请求 → _token_auth_seam (L645)      # Bearer token seam：精确 token 路由 + 可选 /api 前缀
                                    #   （豁免 /api/cron/fire —— "Chronos authenticates this
                                    #     callback with its own short-lived NAS JWT"）
     → auth_middleware (L621)       # 遗留回环门：/api/* 要求 X-Hermes-Session-Token，
                                    #   PUBLIC_API_PATHS 与 MCP OAuth 回调豁免；
                                    #   token_authenticated / auth_required 时跳过
     → _dashboard_auth_gate (L615)  # 非回环绑定时的 OAuth/密码 Cookie 会话门
     → _plugin_api_runtime_gate (L539) # 已禁用插件路由 404（故意排在认证之后执行，
                                    #   注释原文：未认证请求必须先拿到 401，
                                    #   否则可指纹探测已装插件）
     → host_header_middleware (L509) # Host 头校验，DNS-rebinding 防御 (GHSA-ppp5-vxwm-4cf7)
     → CORSMiddleware (L331)        # 仅 localhost 来源
     → 路由处理器
```

注意 L606-611 的注释块写的是「host check → cookie auth → token auth」——那是**洋葱由内向外**的描述；与 L549、L649 两处注释和 Starlette 语义合读，按请求方向即上表顺序。三份注释并存，勿只读其一。

认证三模式与公开路径清单详见 API-HTTP.md §2。

### 4.4 写审批：从 stage 到 apply

记忆/skills 写入的门（`tools/write_approval.py`，553 行；持久层 `hermes_cli/account_write_approvals.py`，`~/.hermes/write-approvals.db`）：

```
工具想写 MEMORY.md / SKILL.md
  └─ evaluate_gate 决策矩阵（write_approval.py 文档字符串逐字翻译）：
       门关(默认) → 直接放行
       门开 + memory + 交互式 CLI → 终端内联 approve/deny
       门开 + memory + gateway/脚本/后台 → stage（入库待审）
       门开 + skills + 任何来源 → stage
     （"the gate only ever delays a write for approval, never silently
       refuses it" —— 门只延迟写入，从不静默拒绝）
  └─ stage_write() → store.stage()：状态机 pending → applying → applied
       │             （或 rejected / expired / failed），带 revision、
       │              decision_token、effect_key、apply_lease_expires_at
  └─ 审批面：iOS/dashboard 经 collaboration 插件 mobile 门面
       GET /mobile/write-approvals, POST .../decision（Idempotency-Key +
       payload_digest 绑定）→ store.claim_decision()【CAS】
  └─ 执行：store.execute_effect()【租约 + 心跳】→
       account_write_approval_apply.py「收敛式文件系统适配器」：
       memory 三种操作 add/replace/remove 带歧义检查；
       _written_text_hash 对 CRLF 归一后比对（幂等重放安全）
  └─ 崩溃恢复：claim_recoverable_applies() 在 dashboard 启动 lifespan 里补跑
```

内联提问复用 `tools.terminal_tool.set_approval_callback` 的回调（**不是** `prompt_dangerous_approval`——避免 `input()` 回退在无 TTY 时死锁，#15216）。

---

## 5. 关键机制

### 5.1 配置权威（`hermes_cli/config.py`）

**读**（严禁绕过——裸 `yaml.safe_load(config.yaml)` 有 23 处冻结允许清单，新增即被棘轮测试打回）：

| 函数 | 用途 |
|---|---|
| `load_config()` | 全量装载：defaults 深拷贝 → 用户合并（含 max_turns 迁移）→ normalize → `_expand_env_vars` → **managed 覆盖层在用户展开之后应用**（注释原文：用户的 `${VAR}` 不能遮蔽 managed 字面量）→ 缓存 |
| `load_config_readonly()` | 同上但跳过深拷贝（约省一半的 265µs），调用方承诺不改返回值 |
| `read_raw_config()` | 原始 YAML（不合 defaults、不展开），带按路径缓存；**有意**无视 `HERMES_IGNORE_USER_CONFIG` |
| `cfg_get(cfg, "a.b.c", default)` | 点路径取值 |

缓存键 =（用户文件 mtime_ns,size）+（managed 文件 mtime_ns,size）+ 环境变量引用快照（#58514）。解析失败回退 **LKG**（last-known-good，`_LAST_EXPANDED_CONFIG_BY_PATH`）——保护如 yolo 下 `approvals.deny` 之类的安全键不因一次损坏写入而失效（移植 openai/codex#31188 的不变量）。

**写**（唯一被认可的模式）：

```python
from hermes_runtime.config import mutate_config
def _bump(raw: dict):          # 收到的是磁盘上的 RAW 文档（无 defaults）
    raw["gateway"]["x"] = 1
    return raw                  # 返回 None 表示放弃写入
mutate_config(_bump)
```

`mutate_config`（L7317）在锁内**重读**磁盘 raw YAML → 调 mutate_fn → `atomic_config_write`。`atomic_config_write`（L7132）经 `require_readable_config_before_write` fail-closed——因为 `read_raw_config()` 对「文件不存在」和「文件存在但读不出」都返回 `{}`，不设防会把整份配置写成只剩你那一个键。

**跨进程锁** `config_write_lock()`（L7164-7190 注释块）：进程内 `_CONFIG_LOCK` 只能串行线程，而部署形态**确实**有多进程同时对 config.yaml 做读-改-写（gateway 持久化 `/reasoning`、`/fast`；dashboard 走 save_config；平台适配器回写状态）。于是加 OS 级建议锁——Windows `msvcrt.locking`、POSIX `fcntl.flock`——**锁的是 config.yaml 旁边的 `<config>.lock`，永不锁 config.yaml 本身**（`atomic_replace` 每次写入换 inode，锁在旧 inode 上会失效）。可重入（`_CONFIG_FILE_LOCK_DEPTH`），10 秒超时后**带警告继续无锁执行**——树内原话："Degrades, never deadlocks"。行为由 `tests/architecture/test_config_write_lock.py` 守护。

**managed scope**（`hermes_cli/managed_scope.py`）：「IT 推送、用户不可变的配置与环境层」，默认 `/etc/hermes`（`HERMES_MANAGED_DIR` 覆盖），按叶键胜出;与 `is_managed()`/`HERMES_MANAGED`（包管理器写锁，粗粒度）是**两回事**。pytest 下忽略。

**历史遗留**：根 `cli.py` 里还有第二个装载器 `load_cli_config()`，gateway 个别处直读 YAML（在允许清单内）。新代码一律走上表。

### 5.2 认证与凭据模型

**Dashboard 三模式**（详表见 API-HTTP.md §2.3）：

1. **回环临时 token**：`_SESSION_TOKEN = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)`（web_server.py L311，模块级全局）。回环绑定时 SPA HTML 注入该 token，请求带 `X-Hermes-Session-Token`。`should_require_auth`（L443）：绑定 127.0.0.1/localhost/::1 → 不要求账号登录；其它一律要求——**`allow_public`（旧 `--insecure`）不再能关掉这个门**（注释点名 2026 年 6 月 `hermes-0day` MCP 持久化攻击活动后封死的洞）。
2. **门控 Cookie 会话**：非回环绑定时 `DashboardAuthProvider` 栈（OAuth PKCE / 密码 / 自建 provider），`verify_session` 每请求校验，透明续期。
3. **Bearer token seam**：`TokenPrincipal(principal, provider, scopes)`；`register_token_route`（精确路径全权归 seam）与 `register_optional_token_prefix(prefix, required_scope="dashboard:admin")`（最长前缀的 scope 生效）。iOS 原生端即由 `MobileApiKeyProvider` / `OwnerMobileTokenProvider` 走此通道。

**WS 凭据**（gated 模式禁用 `?token=`）：`?ticket=` 单次使用、30 秒 TTL（`ws_tickets.py`，经认证的 `POST /api/auth/ws-ticket` 铸造）；`?internal=` 进程生命周期凭据，仅经子进程 env 传递给服务端自拉的 PTY 子进程。

**秘密比较**：三个 HTTP 面（FastAPI dashboard、aiohttp api_server、TUI sidecar 相关）统一走根模块 `hermes_secret_compare.py`：常量时间（hmac.compare_digest）、双侧 UTF-8 编码、空值 fail-closed。模块注释记载了动机（「kanban dashboard 停机事故」）并声明「故意住在仓库根、除标准库外零依赖」以规避分层纠纷。

**模型供应商凭据**与 dashboard 认证是两个体系：`hermes_cli/auth.py`（8,446 行）管 OAuth 设备码流/API key，状态存 `~/.hermes/auth.json`（跨进程文件锁），`resolve_provider()` 决定优先链。`hermes proxy` 则在本地把客户端 Authorization 丢弃、换上真实上游凭据转发。

### 5.3 会话与数据存储全景（该用哪个）

| # | 存储 | 位置/介质 | 用途 | 何时用它 |
|---|---|---|---|---|
| 1 | `hermes_state.py::SessionDB` | `~/.hermes/state.db`（SQLite WAL + FTS5） | 会话/消息/模型用量/gateway 路由/压缩锁/异步委派的**权威库** | 任何要落盘的对话数据。注意头注：Batch runner 与 RL 轨迹**不**存这里 |
| 2 | `gateway/session.py::SessionStore` | 包装 SessionDB + sessions 目录 legacy `sessions.json` 路由索引镜像 | gateway 的「平台会话 → 存储会话」路由、重置策略、auto-continue 新鲜度 | 只在 gateway 内用;别的进程别碰镜像文件 |
| 3 | `hermes_cli/active_sessions.py` | JSON 租约文件 | 「跨进程活跃聊天会话租约」：记录当前打开的 surface（含空闲的），实施 `max_concurrent_sessions` | 需要知道/限制「现在谁开着会话」时 |
| 4 | `hermes_cli/account_session_facade.py` | state.db 增表（mobile_session_bindings/forks/account_deletions） | 移动端账号作用域门面：分叉在同事务按精确 message id 拷贝，CAS + 幂等 | iOS 会话分叉/世系/账号删除;「Messages and sessions remain in the existing authoritative tables」 |
| 5 | `tui_gateway/server.py::_sessions` | 进程内 dict + RLock | 活跃 TUI worker 表;gateway 拥有的会话对 TUI 只读 | 仅 tui_gateway 进程内部 |
| 6 | `hermes_cli/account_write_approvals.py` | `~/.hermes/write-approvals.db` | 写审批状态机（§4.4） | 记忆/skills 写审批 |
| 7 | `dashboard_auth/mobile_device_store.py` | `~/.hermes/dashboard/mobile-auth.db` | 移动设备/访问会话/refresh 历史+幂等/APNs token/删号 outbox | iOS 设备与令牌生命周期 |
| 8 | collaboration 插件 | `~/.hermes/collaboration/rooms.json`、`single.json` | 群聊房间、单聊会话、hosted_turns 字典（含恢复/备份） | 仅经 collaboration 插件 API 读写 |
| 9 | 其他 | `cron/jobs.json`、`kanban_db.py`（kanban.db）、`projects_db.py` | 各自领域 | — |

经验法则：**对话正文永远进 state.db（#1）**;其余七个都是围绕它的路由/租约/审批/账号侧写。新功能若想再发明一个会话存储，先在此表里找位置。

### 5.4 文件安全护栏（`agent/file_safety.py`）

「工具与 ACP 垫片共用的文件安全规则」：

- 写拒绝**精确路径**（`build_write_denied_paths`）：ssh 密钥与配置、profile 与根 `.env`（#15981）、`.anthropic_oauth.json`、`.netrc`/`.pgpass`/`.npmrc`/`.pypirc`/`.git-credentials`、`/etc/sudoers`,`passwd`,`shadow`;
- 写拒绝**前缀**（`build_write_denied_prefixes`）：`.ssh/`、`.aws/`、`.gnupg/`、`.kube/`、`.docker/`、`.azure/`、`.config/gh`、`gcloud`、`/etc/sudoers.d`、`/etc/systemd`;
- `HERMES_WRITE_SAFE_ROOT`（os.pathsep 分隔）圈定安全根;判定入口 `is_write_denied` / `get_write_denied_error(verb=)`;
- 读侧：`get_read_block_error` / `raise_if_read_blocked`（#57698 统一读拒绝清单）;
- 跨 profile 访问分类与告警：`classify_cross_profile_target` / `get_cross_profile_warning`;沙箱/容器有镜像分类器。

消费方：`tools/file_operations.py`、`file_tools.py`、`credential_files.py`、`image_source.py`。新的写文件路径**必须**过这套判定，不得自行实现。

### 5.5 延迟导入惯例（为什么到处是函数体 import）

全仓 2,012 条跨包 import 被推迟进函数体——这是本仓「让循环依赖可导入」的标准手法，并有第二动机：入口冷启动速度（`main.py` 的 `_plugin_cli_discovery_needed()` 快路径省约 500-650ms）。layering.md 的警句值得整段记住：**"a deferred import is a cycle that only fails when the function first runs"**——延迟导入是一枚要到函数首次运行才爆的雷，所以每包的延迟导入计数被棘轮冻结，只许降不许升。写新代码时：顶层 import 能工作就放顶层;放不了顶层说明你在制造新环——重构而不是下沉 import。

### 5.6 Profile 与 HERMES_HOME

多 profile（如 `coder`）各有独立 `~/.hermes/profiles/<name>/` 树（config/state.db/cron/...）。`HERMES_HOME` 必须在任何业务 import 之前设定（AGENTS.md 规则）;取路径一律 `get_hermes_home()`，展示用 `display_hermes_home()`。gateway 的 api_server 用 `/p/<profile>/` URL 前缀 + ContextVar `_profile_scope` 在请求期间切换 profile 作用域。

---

## 6. 架构债（直说）

以下是已知、被度量、被棘轮圈住但尚未偿还的债。改动时不要装作它们不存在。

### 6.1 六包依赖网

25/30 条有向边存在、2,313 条跨包 import、438 条跨包私有符号 import。目标分层（§2.1 表的 L 列）目前**每一条被禁止的上行边都实际存在**。四个入口巨石文件（`gateway/run.py` 23k 行、`web_server.py` 20k 行、`tui_gateway/server.py` 16k 行、`hermes_cli/main.py`）集中了最深的延迟导入债。数字与允许清单见 layering.md;`tests/architecture/` 的六个测试（bare reads / dependency direction / private symbols / wired security controls / config write lock / ignore-user-config 收敛）是唯一的执法机制——**baselines 只许收紧**（改善后 `archlint.py --write-baselines` 锁住战果）。

### 6.2 三个并行 HTTP 面 + cron 三重暴露

三套互不复用的 HTTP 技术栈同时在线：FastAPI dashboard（240 个路由装饰器 + 4 个挂载 router）、aiohttp `api_server`（OpenAI 兼容 + 自己的一套 sessions/cron 路由）、tui_gateway JSON-RPC（134 个方法）。后果实例：

- cron CRUD 在 dashboard（`/api/cron/jobs*`）与 gateway（`/api/jobs*`，动词还不同：`trigger` vs `run`）各实现一遍，`/p/<profile>/api/jobs*` 是第三种路径语法;`POST /api/cron/fire` 两面各挂一个，共用同一 Chronos JWT 校验器——两处必须同步维护（对照表见 API-HTTP.md §6）。
- sessions 的 REST 也在两面各有一套形状不同的实现。
- collaboration「dashboard 插件」实际是寄生在插件位上的**第二个应用**：53 个路由、14.7k 行，装着群聊、hosted turns、远程 connector 作业协议和整个 iOS 移动门面。

### 6.3 模块级可变全局 → 单进程部署假设

`web_server.py` 的 `_SESSION_TOKEN`（import 时生成）、`app.state` 上的 event channels/PTY 注册表、内存里的 OAuth flow/WS ticket 表、`tui_gateway` 的 `_sessions` dict、`tools/approval.py` 的进程内 pending 队列——这些都把「一个部署 = 一个进程」焊死：uvicorn 开多 worker 会得到 N 个互不相认的 token 和会话表。跨进程协调目前全靠文件锁（§3.2），任何「横向扩容」设想都得先动这一层。

### 6.4 双配置装载器与裸读残留

`load_cli_config()`（cli.py）与 `load_config()`（config.py）并存;23 处裸 YAML 读取被冻结在允许清单里（gateway/run.py 独占 7 处）。语义差异（defaults 合并、managed 覆盖、LKG、环境展开）意味着同一份 config.yaml 在不同代码路径下可能被读出**不同结果**——新代码只准走 §5.1 的权威函数。

---

## 7. 「新功能放哪」决策指南

第一原则（AGENTS.md，Footprint Ladder）：**从最小足迹开始，逐级上升，能停就停**——
扩展现有工具 → CLI 命令 + skill → `check_fn` 门控工具 → 插件 → MCP catalog → 核心工具。

| 你要做的事 | 落点 | 备注 |
|---|---|---|
| 给模型加能力 | 插件内 `ctx.register_tool`；确需核心工具才碰 `tools/` + `toolsets.py` 双文件 | API-EXTENSION.md §1 |
| 新聊天平台 | `plugins/platforms/<name>/`（`kind: platform`） | 勿走内置 16 步路径;HTTP 回调型实现 `verify_http_event_request`/`dispatch_http_event` |
| 新模型供应商 | `plugins/model-providers/<name>/` | `register_provider(ProviderProfile(...))` |
| 新记忆后端 | `plugins/memory/<name>/`（`kind: exclusive`） | 工具名不得撞核心工具 |
| Dashboard 新页面/新 API | `dashboard/manifest.json` 约定;API 挂 `/api/plugins/<name>/` | 别再往 `web_server.py` 直加路由（它已 20k 行）;服务间调用叠 token seam |
| 观察/改写模型请求 | Hook `pre_api_request`（观察）/ middleware `api_request`（改写） | API-EXTENSION.md §2.7 |
| 读写配置 | `load_config[_readonly]()` / `mutate_config()` | 触发裸读棘轮=返工 |
| 比较任何秘密 | `hermes_secret_compare` | 三面统一，禁止手写 `==` |
| 写用户文件 | 先过 `agent/file_safety.py` 判定 | §5.4 |
| 新会话类数据 | 进 state.db 或 §5.3 表内既有存储 | 不新发明存储 |
| 定时任务 | `cron/jobs.py` + blueprint | 尊重 3 分钟硬中断、`.tick.lock`、`skip_memory` |
| 斜杠命令 | `hermes_cli/commands.py::COMMAND_REGISTRY` 或插件 `ctx.register_command` | — |

提交前自检清单：

1. 新增跨包 import 是否让 `tests/architecture` 棘轮变红？变红即重新选落点（把共享代码下沉到低层或 L0 叶模块，而不是放宽 baseline）。
2. 是否动了 system prompt 的字节稳定性或 user/assistant 交替？（缓存纪律，默认答案是「不许」。）
3. gateway 路径改动是否同时照顾了**两个消息卫兵**对控制命令的放行？
4. 涉及 cron/sessions 的 HTTP 改动是否需要同步 dashboard 与 api_server 两面？
5. 秘密比较、文件写入、配置读写是否走了对应权威模块？

---

## 附：本文引用的树内注释原文索引

| 位置 | 原文（节选） |
|---|---|
| `hermes_cli/web_server.py` L549 | "Registered BEFORE the auth middlewares (so it executes AFTER them): a request that hasn't cleared auth must get auth's 401 first, never this gate's 404" |
| `hermes_cli/web_server.py` L649 | "Registered LAST so it runs FIRST (Starlette middleware is outermost-last)" |
| `hermes_cli/config.py` L7164-7190 | "The lock target is `<config>.lock` NEXT TO config.yaml, never config.yaml itself: atomic_replace swaps in a new inode on every write" / "Degrades, never deadlocks" |
| `tools/write_approval.py` | "the gate only ever delays a write for approval, never silently refuses it" |
| `tools/approval.py` | "Reading os.environ on every call would allow any skill ... to bypass all approval checks — a prompt-injection escalation path" |
| `docs/architecture/layering.md` | "a deferred import is a cycle that only fails when the function first runs" |
| `AGENTS.md` | "The gateway has TWO message guards — both must bypass approval/control commands" |
| `hermes_secret_compare.py` | 模块自述：三条规则（常量时间 / UTF-8 双侧编码 / 空值 fail-closed）与「deliberately lives at the repository root」 |

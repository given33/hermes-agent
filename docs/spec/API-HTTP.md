# Hermes Agent HTTP / WebSocket 接口文档（API-HTTP）

**状态**：基于 2026-07-26 源码中**实际路由注册**逐条枚举（非设计稿）。行号为对应源文件锚点，与代码漂移时以代码为准。
**配套文档**：[`ARCHITECTURE.md`](ARCHITECTURE.md)（进程拓扑与认证机制原理）、[`API-EXTENSION.md`](API-EXTENSION.md)（如何新增路由/平台/插件）。

**表格约定**：認证列缩写——`会话` = dashboard 会话认证（回环 token 或 gated Cookie，见 §2.1）;`Token` = 额外要求 `_require_token`（X-Hermes-Session-Token/Bearer）;`公开` = 无认证;`APIKey` = gateway 的 `Authorization: Bearer <API_SERVER_KEY>`;`JWT` = Chronos NAS 短时 JWT。错误列只列处理器显式抛出的状态码。

---

## 1. 总览：三个主 HTTP 面

| 面 | 技术栈 | 默认绑定 | 定义文件 | 规模 |
|---|---|---|---|---|
| Dashboard | FastAPI + uvicorn | `127.0.0.1:9119` | `hermes_cli/web_server.py`（app 于 L283） | 240 个 `@app.*` 装饰器 + 4 个挂载 router + 5 个 WS 端点 |
| Gateway REST（api_server 平台） | aiohttp | `127.0.0.1:8642` | `gateway/platforms/api_server.py` | 35 条路由 ×2（每条同时注册 `/p/{profile}` 前缀变体;`/api/cron/fire` 仅 cron 可用时注册） |
| TUI Gateway | JSON-RPC 2.0（stdio / WS） | stdio;WS 复用 dashboard `/api/ws` | `tui_gateway/server.py` | 133 个 RPC 方法 |

此外仓库内共 **21 个网络监听器**（含平台 webhook、OAuth 回环、Node sidecar 等），完整清单见 §7。`mcp_serve.py` 与 `acp_adapter/server.py` 为 stdio-only，无套接字。

**明确不存在的接口**（常见误解，已核实）：dashboard 上没有 `/api/kanban`、`/api/projects`、也没有独立的写审批 HTTP 端点——kanban/projects 走 TUI JSON-RPC（§5.3），写审批走 collaboration 插件的 mobile 门面（§3.6）。

---

## 2. Dashboard（FastAPI，`hermes_cli/web_server.py`）

### 2.1 认证模型

| 模式 | 触发条件 | 凭据 |
|---|---|---|
| 回环临时 token | 绑定 127.0.0.1/localhost/::1（`should_require_auth` L443 返回 False） | `_SESSION_TOKEN`（L311，进程级随机;SPA HTML 注入 `__HERMES_SESSION_TOKEN__`），头 `X-Hermes-Session-Token` 或 Bearer |
| 门控 Cookie 会话 | 非回环绑定（`auth_required`;旧 `--insecure`/`allow_public` **不再**豁免） | OAuth PKCE / 密码登录 → Cookie 会话，`DashboardAuthProvider.verify_session` 每请求校验 + 透明续期 |
| Bearer token seam | 任意（最外层中间件） | `register_token_route` 精确路由（现仅 `/api/gateway/drain`，drain 插件设 `HERMES_DASHBOARD_DRAIN_SECRET` 时注册）;`register_optional_token_prefix("/api")`（mobile provider 注册，scope `dashboard:admin`）→ iOS 原生端全程 Bearer |

查询参数 token 仅允许 `_QUERY_TOKEN_API_PATHS = {"/api/files/download"}`（L389）。

### 2.2 中间件栈（按请求执行顺序）

```
_token_auth_seam (注册 L645) → auth_middleware (L621) → _dashboard_auth_gate (L615)
→ _plugin_api_runtime_gate (L539) → host_header_middleware (L509) → CORSMiddleware (L331) → 路由
```

原理、三处树内注释原文及 L606-611 注释的方向性辨析见 ARCHITECTURE.md §4.3。要点：token seam 豁免 `/api/cron/fire`（Chronos 用自己的 JWT）;插件运行时门故意在认证之后执行（防插件名指纹探测）;host 头校验防 DNS-rebinding（GHSA-ppp5-vxwm-4cf7）;CORS 仅放行 localhost 来源。

### 2.3 公开（无认证）路径清单

`hermes_cli/dashboard_auth/public_paths.py:33-68` 逐字：

```python
PUBLIC_API_PATHS: frozenset[str] = frozenset({
    "/api/status",
    "/api/config/defaults",
    "/api/config/schema",
    "/api/model/info",
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
    "/api/mobile/v1/handshake",
    "/api/managed-nodes/recovery-hook",   # 自带 peer-token 认证
    "/api/cron/fire",                     # 自带 Chronos JWT 认证
})
```

Cookie 门（`dashboard_auth/middleware.py`）额外放行：精确路径 `_GATE_PUBLIC_EXACT`（L65-78）：`/auth/login`、`/auth/callback`、`/auth/password-login`、`/auth/logout`、`/login`、`/api/auth/providers`、`/favicon.ico`、`/manifest.webmanifest`、`/apple-touch-icon.png`、`/hermes-official.png`;前缀 `_GATE_PUBLIC_PREFIXES`（L82-89）：`/auth/mobile/`、`/api/mcp/oauth/callback/`、`/assets/`、`/ds-assets/`、`/fonts/`、`/fonts-terminal/`。遗留回环门另豁免 `/api/mcp/oauth/callback/*`（L635）。`/dashboard-plugins/*` 静态资源有意不认证（L19564，后缀允许清单 + 防穿越）。

### 2.4 WebSocket 端点（5 个）

| WS 路径 | 行号 | 用途 | 关闭码 |
|---|---|---|---|
| `/api/console` | 17867 | 内嵌聊天控制台通道 | 4404 禁用;认证/host 各有专属码 |
| `/api/pty` | 18217 | PTY 桥：spawn `hermes --tui` 子进程供 Chat 页 xterm.js | 4404 禁用;认证/来源/host/peer 分码 |
| `/api/ws` | 18404 | tui_gateway JSON-RPC 通道（桌面端与 SPA 主通道） | 4403 禁用、4401 认证、4403 来源/peer |
| `/api/pub` | 18435 | PTY 子进程侧事件发布 sidecar | 4403 / 4401 / 4403 |
| `/api/events` | 18463 | 浏览器事件订阅（侧栏） | 4403 / 4401 / 4403 |

WS 认证（`_ws_auth_reason` L17242-17328）：回环模式 `?token=<_SESSION_TOKEN>`（常量时间比较）;gated 模式**无条件拒绝** `?token=`，改用 `?ticket=`（单次、30s TTL，`ws_tickets.py`，经 `POST /api/auth/ws-ticket` 铸造，拒绝记审计 `WS_TICKET_REJECTED`）或 `?internal=`（进程生命周期凭据，仅经子进程 env 下发，绝不进 HTML）。所有端点另过 Host/Origin 校验（L17210）与 peer IP 校验（L17133）。

#### 2.4.1 `/api/console` 帧协议

该端点运行进程内受限 `HermesConsoleEngine`，不创建 PTY、shell 或完整 CLI 子进程。命令在线程数固定为 4 的独立 executor 中执行，单命令超时 60 秒，输出上限 50,000 字节；同一连接同时只允许一个活动命令。可选查询参数 `profile=<name>` 进入对应 Profile scope，未知 Profile 在 accept 后发送 `error` 并以 4400 关闭。

客户端帧均为 UTF-8 JSON object：

| `type` | 字段 | 行为 |
|---|---|---|
| `input` / `command` | `line` 或 `command` | 执行一条受限控制台命令；空行直接返回 `complete(status=ok)` |
| `confirm` | `command` | 仅当命令与当前 `confirm_required.command` 完全一致时继续执行 |
| `cancel` | — | 取消当前 awaitable 或待确认命令；无任务时返回 `complete(status=idle)` |
| `ping` | — | 返回 `pong` |

服务端帧：

| `type` | 主要字段 | 语义 |
|---|---|---|
| `ready` | `profile,prompt` | 握手完成，prompt 固定为 `hermes> ` |
| `output` | `id,stream=stdout,data,command` | 成功命令的有界输出 |
| `confirm_required` | `id,command,message,prompt` | 危险/写操作等待同连接显式确认 |
| `clear` | `id` | 客户端清屏 |
| `error` | `id?,message,command?,prompt?` | 非法帧、并发命令、执行异常或超时详情 |
| `complete` | `id?,status,command?,prompt` | 终态；status 为 `ok/error/confirm_required/clear/exit/timeout/cancelled/idle` |
| `pong` | `prompt` | 心跳响应 |

二进制帧只在能按 UTF-8 解码为 JSON object 时接受；格式错误返回 `error` 且连接保持。`exit` 结果发送终态后以 1000 正常关闭；断连时取消该连接的活动 awaitable。线程内 Python 调用不可抢占，因此超时 worker 可能继续到自然结束，但不会再回写旧 generation，且固定 executor 将资源占用限制在 4 个线程内。

### 2.5 认证域（`hermes_cli/dashboard_auth/routes.py`，挂载于根，L19774）

| 方法 | 路径 | 行 | 认证 | 用途 | 响应/错误 |
|---|---|---|---|---|---|
| GET | `/login` | 137 | 公开 | 服务端渲染登录页（校验 `next=`） | HTML |
| GET | `/api/auth/providers` | 157 | 公开 | 列出可交互登录的 provider | `{providers:[{name,display_name,supports_password}]}`;零 provider 时 503（fail-closed） |
| GET | `/auth/login` | 187 | 公开 | 发起 OAuth：302 → IDP，PKCE Cookie 打包 provider + 校验过的 `next` | 404×2、503 |
| GET | `/auth/callback` | 253 | 公开 | 完成 OAuth：state 校验、铸 Cookie 会话、302 → `next` 或 `/` | 400×4（PKCE 缺失/未知 provider/IDP 错误/state 不符）、503 |
| POST | `/auth/password-login` | 554 | 公开 | 用户名密码登录;双滑动窗限流（每 IP 10/60s + 每账号 20/300s，键表上限 4096 带溢出桶） | `{ok,next}` + Cookie;429、404、401、500、503 |
| POST | `/auth/logout` | 647 | 会话 | 尽力在全部 provider 上吊销，清 Cookie，302 `/login` | — |
| GET | `/api/auth/me` | 683 | 会话 | 当前会话信息 | `{user_id,email,display_name,org_id,provider,expires_at}`;401 |
| POST | `/api/auth/ws-ticket` | 704 | 会话或 `dashboard:admin` token | 铸 30s 单次 WS ticket（iOS 走 token 路径） | `{ticket,ttl_seconds}`;401 |

错误语义（`dashboard_auth/base.py`）：`ProviderError→503`、`InvalidCodeError→400`、`InvalidCredentialsError→401`（统一文案，不区分「用户不存在」与「密码错误」）。

### 2.6 移动 / Owner 域（`hermes_cli/dashboard_auth/owner_mobile.py`，挂载于根，L19780）

注册流程受 `HERMES_MOBILE_REGISTRATION_ENABLED` + owner 邮箱（`HERMES_OWNER_EMAIL`，QQ SMTP 发验证码）门控。`ensure_owner_provider()`（L399）注册 `BasicAuthProvider`（读 `dashboard.basic_auth` 配置）与 `OwnerMobileTokenProvider`（调用 `register_optional_token_prefix("/api")`）。`/auth/mobile/` 是 Cookie 门公开前缀（注册/登录/续期须在未认证时可用）;`/api/mobile/v1/*` 由 Bearer 认证（token seam + 处理器内 `MobileDeviceStore.verify_access` 复核）。

| 方法 | 路径 | 行 | 认证 | 用途 | 错误 |
|---|---|---|---|---|---|
| GET | `/api/mobile/v1/handshake` | web_server.py 2878 | 公开 | 原生端契约探针：`api_version, hermes_version, profiles, profile_count, capabilities, server_time` | — |
| GET | `/auth/mobile/status` | 437 | 公开 | `{registration_open, account_configured, email_verification_required, owner_email_configured}` | — |
| POST | `/auth/mobile/registration-code` | 447 | 公开 | 发 6 位邮箱验证码（10 分钟 TTL、60s 重发、按 IP/邮箱限流） | 403×2、429×2、422、503、502、500 |
| POST | `/auth/mobile/register` | 494 | 公开 | 首任 owner 注册：验码、写 `basic_auth` 配置、铸 token 对（删号墓碑边界强制） | 429×2、403×3、422×3、409×3、503、500 |
| POST | `/auth/mobile/token` | 576 | 公开 | Owner 密码登录 → `{access_token,refresh_token,token_type,expires_at,refresh_expires_at,session_id,device_id,account}` | 429、409、401 |
| POST | `/auth/mobile/refresh` | 616 | 公开（凭 refresh token） | 轮换 refresh token（幂等表防重放） | 429、409、401 |
| POST | `/auth/mobile/logout` | 629 | Bearer | 吊销会话 → `{ok,revoked}` | — |
| GET | `/api/mobile/v1/devices` | 644 | Bearer | 列 owner 设备 | 401 |
| DELETE | `/api/mobile/v1/devices/{device_id}` | 657 | Bearer | 吊销设备 | 401、404 |
| PUT | `/api/mobile/v1/devices/{device_id}/apns` | 671 | Bearer | 登记 APNs token（限当前设备） | 403、422、404 |
| DELETE | `/api/mobile/v1/devices/{device_id}/apns` | 694 | Bearer | 注销 APNs → `{ok,removed}` | 403 |

持久层 `mobile_device_store.py`（`~/.hermes/dashboard/mobile-auth.db`）：表 mobile_devices / mobile_sessions / mobile_refresh_history / mobile_refresh_idempotency / mobile_apns_tokens / mobile_account_deletion_outbox。`mobile_notifications.py` 为纯 APNs 推送库（HTTP/2 provider-token），**不注册路由**。iOS 的会话/审批业务面在 collaboration 插件（§3.6）。

### 2.7 会话域（web_server.py）

| 方法 | 路径 | 行 | 用途 | 关键参数 | 错误 |
|---|---|---|---|---|---|
| GET | `/api/sessions` | 4461 | 分页会话列表 | `limit,offset,min_messages,archived,order,source,exclude_sources,cwd_prefix,full,profile` | 500 |
| GET | `/api/profiles/sessions` | 4560 | 跨 profile 合并列表 | 同上 + `profile="all"` | 400×2 |
| GET | `/api/profiles/sessions/sidebar` | 4688 | 侧栏批量切片（recents/cron/messaging） | `recents_*,cron_limit,messaging_*` | 500 |
| GET | `/api/sessions/search` | 4821 | FTS5 全文 + id 检索，按压缩世系去重 | `q,limit,profile` | 404 |
| POST | `/api/sessions/bulk-delete` | 11475 | 单事务批删 | `{ids}` | 413 |
| POST | `/api/sessions/import` | 11528 | 导入导出的会话 JSON | raw body | 400×3、413 |
| GET | `/api/sessions/empty/count` | 11554 | 空会话计数 | `?profile` | — |
| DELETE | `/api/sessions/empty` | 11572 | 清空空的已结束会话 | `?profile` | — |
| GET | `/api/sessions/stats` | 11603 | 存储统计 | `?profile` | — |
| GET | `/api/sessions/{session_id}` | 11649 | 详情（支持前缀解析） | `?profile` | 404 |
| GET | `/api/sessions/{session_id}/latest-descendant` | 11665 | 压缩世系最新后代 | `?profile` | 404 |
| GET | `/api/sessions/{session_id}/messages` | 11687 | 消息分页 | `profile,limit,offset` | 404 |
| DELETE | `/api/sessions/{session_id}` | 11722 | 删除（幂等） | `?profile` | 404 |
| PATCH | `/api/sessions/{session_id}` | 11758 | 重命名/归档 | `{title?,archived?,profile?}` | 404、400 |
| GET | `/api/sessions/{session_id}/export` | 11792 | 导出 JSON | `?profile` | 404、400 |
| POST | `/api/sessions/prune` | 11919 | 条件批量修剪 | `SessionPrune` | 400 |

（gateway 面还有一套形状不同的 sessions REST，见 §4.2——两套并存是已声明的架构债。）

### 2.8 配置域

| 方法 | 路径 | 行 | 用途 | 错误 |
|---|---|---|---|---|
| GET | `/api/config` | 6193 | 规范化配置（剥内部键，`?profile`） | — |
| GET | `/api/config/defaults` | 6201 | **公开** `DEFAULT_CONFIG` | — |
| GET | `/api/config/schema` | 6206 | **公开** `{fields, category_order}` | — |
| PUT | `/api/config` | 7424 | 深合并保存（`{config,profile}`;走配置权威） | 500 |
| GET | `/api/config/raw` | 16545 | 原始 YAML 文本 + 路径 | — |
| PUT | `/api/config/raw` | 16561 | 整文档 YAML 替换 | 400×2 |

### 2.9 Cron 域（dashboard 侧;与 gateway 侧对照见 §6）

| 方法 | 路径 | 行 | 用途 | 错误 |
|---|---|---|---|---|
| GET | `/api/cron/jobs` | 12244 | 列任务（`?profile=all` 跨 profile） | — |
| GET | `/api/cron/jobs/{job_id}` | 12259 | 单任务 | 404×2 |
| GET | `/api/cron/jobs/{job_id}/runs` | 12309 | 运行历史（会话名 `cron_{id}_*`） | 400 |
| POST | `/api/cron/jobs` | 12352 | 创建（`CronJobCreate`） | 400/404 |
| GET | `/api/cron/delivery-targets` | 12357 | 投递目标下拉（按平台动态） | — |
| PUT | `/api/cron/jobs/{job_id}` | 12419 | 更新；body=`{updates:{...}}` | 404×2、400 |
| POST | `/api/cron/jobs/{job_id}/pause` | 12434 | 暂停 | 404×2 |
| POST | `/api/cron/jobs/{job_id}/resume` | 12449 | 恢复 | 404×2 |
| POST | `/api/cron/jobs/{job_id}/trigger` | 12464 | 立即执行 | 404×2 |
| DELETE | `/api/cron/jobs/{job_id}` | 12482 | 删除 | 404×2、400 |
| POST | `/api/cron/fire` | 12513 | **公开路径 + Chronos JWT**（purpose=cron_fire）：scale-to-zero 唤醒，CAS 认领后跑一个到期任务 | 401、400;200 gone / 202 accepted |
| GET | `/api/cron/blueprints` | 12582 | 蓝图目录（表单 schema 形式） | 500 |
| POST | `/api/cron/blueprints/instantiate` | 12616 | 蓝图实例化 → 建任务 | 404、422、400 |

### 2.10 其余域（压缩枚举）

以下为完整枚举但压缩描述;每行含处理器行号锚点。除标注外认证均为「会话」。

**媒体/上传**：GET `/api/media`(1869, 网关本地图→dataUrl, 400/415/403/404/413/500);POST `/api/chat/image-upload`(2146, 剪贴板图存 `HERMES_HOME/images/`, 400/413/403/500)。

**托管文件 `/api/files`**（容器场景根 `/opt/data`;共享助手 `_resolve_managed_path` 抛 400/403/404）：GET `/api/files`(2189 列目录);GET `/read`(2221, 敏感路径拦截, +413);GET `/download`(2256, 唯一允许 `?token=` 的路径);POST `/upload`(2292, data-URL 写入, 409×2/403/500);POST `/upload-stream`(2327, multipart, +413);POST `/mkdir`(2391, 409/403/500);DELETE `/api/files`(2412, 根保护, 400×2/404)。

**桌面远程 FS `/api/fs`**（`_fs_path` 校验）：GET `/list`(2437, 隐藏名过滤);GET `/read-text`(2463, 限长预览, 413/403/400);POST `/write-text`(2492, 原子写 UTF-8, 413/403/400×4/500);GET `/read-data-url`(2539);GET `/git-root`(2553);GET `/default-cwd`(2564)。

**Git `/api/git/*`**（全部委托 `hermes_cli/web_git`，包装器 400 于 2588）：status(2628)/worktrees(2633)/branches(2638)/base-branches(2643)/review/list(2648)/review/diff(2653)/file-diff(2660)/review/commit-context(2665)/review/rev-parse(2670)/review/ship-info(2675)/review/stage(2680)/unstage(2685)/revert(2690)/commit(2695, 可选 push)/push(2700)/create-pr(2705, gh)/worktree/add(2710)/worktree/remove(2725)/branch/switch(2732)。

**状态/系统**：GET `/api/status`(2899, **公开**存活探针：版本/gateway 态/会话数/认证门形状);GET `/api/system/stats`(3165, psutil 优雅降级);GET `/api/portal`(3498, Nous portal 状态)。

**托管节点/舰队安装**：GET `/api/managed-nodes/status`(3246, 脱敏健康);POST `/recover`(3261, 幂等恢复, 400);POST `/recovery-hook`(3280, **公开路径 + peer token 自认证**, 401/400/503);POST `/api/managed-installations`(3314, 202);GET 同路径(3340) 与 `/{operation_id}`(3359, 404)。

**Curator/学习图谱**：GET `/api/curator`(3378);PUT `/paused`(3403);POST `/run`(3411);GET `/api/learning/graph`(3421);GET/DELETE/PUT `/api/learning/node`(3449/3461/3473, 404/400)。

**运维 `/api/ops/*`**（多数为 spawn 后台动作 → `{ok,pid,name}`，日志经 `/api/actions/{name}/status`(4406) 轮询）：prompt-size(3546)/dump(3555)/config-migrate(3564)/debug-share(3582, 同步脱敏上传, 502/500)/doctor(13904)/security-audit(13914)/backup(13938)/backup/download(13966, 目录限定, 404×2/400/403)/import(14001)/import-upload(14029, 413)/hooks GET·POST·DELETE(14107/14166/14228, shell hook + 同意机制)/checkpoints(14265)/checkpoints/prune(14297)。

**Gateway 控制与自更新**：POST `/api/gateway/restart`(3894)/start(13614)/stop(13626)/drain(3911, 兼 token 路由);POST `/api/hermes/update`(3984, 容器内禁用);GET `/api/hermes/update/check`(4076)。

**音频**：POST `/api/audio/transcribe`(4166, 400×3/413/500);GET `/api/audio/elevenlabs/voices`(4273, 502×2);POST `/api/audio/speak`(4341, TTS→dataUrl, 400/500×4)。

**记忆**：GET `/api/memory`(活跃 provider + 内置文件大小);PUT `/api/memory/provider`;POST `/api/memory/reset`(删 MEMORY.md/USER.md);GET/PUT `/api/hermes/memory?profile=`（iOS/Hermes Studio 按 Profile 读取或原子保存 MEMORY.md、SOUL.md、USER.md，返回三段内容及 mtime；section 仅 `memory|soul|user`，未知 Profile 404）；GET/POST/PUT `/api/memory/providers/{name}/config|setup|config`;挂载 router `memory_oauth.py`：POST `/api/memory/providers/{provider}/oauth/start`、GET `/oauth/status`。

**模型**：GET `/api/model/info`(6226, **公开**);GET `/custom`(6308, 掩码);GET `/credentials`(6336);DELETE `/credentials/{id}`(6353);PUT `/custom`(6370);POST `/custom/discover`(6433, SSRF 校验 base_url);POST `/custom/test`(6609);GET `/options`(6757，支持 `include_unconfigured=1` 返回未配置 provider 骨架供 iOS 模型检测/配置);GET `/recommended-default`(6806);GET `/auxiliary`(6883);GET/PUT `/moa`(6934/6950);POST `/set`(7018)。

**环境变量与凭据**：GET `/api/env`(7555, 值脱敏);PUT(7622)/DELETE(8022);POST `/api/env/reveal`(8050, **Token + 限流 5/30s→429 + 审计**);`/api/providers/custom-endpoints` CRUD+activate+validate(7851-7960;`/api/providers/validate` 要 Token);凭据池 `/api/credentials/pool` GET/POST/DELETE(13675/13702/13754)。

**LLM 供应商 OAuth**（变更类全部 Token）：GET `/api/providers/oauth`(10266);DELETE `/{provider_id}`(10309);POST `/{provider_id}/start`(11245);POST `/submit`(11288);GET `/poll/{session_id}`(11304);DELETE `/sessions/{session_id}`(11330)。

**消息平台接入**：WhatsApp 配对 start/poll/apply/cancel(9144/9201/9216/9272, 404/410/409);Telegram 托管配对 start/poll/apply/cancel(9432/9470/9572/9641, 502 经安装服务);GET `/api/messaging/platforms`(9648);PUT `/{platform_id}`(9720, 409);POST `/{platform_id}/test`(9787)。

**日志**：GET `/api/logs`(11930, 按级别/组件/搜索过滤, 400)。

**MCP**：GET/POST/PUT `/api/mcp/servers`(12779/12792/12828);DELETE `/{name}`(12847);POST `/{name}/test`(12858);POST `/{name}/auth`(13047, Token);GET `/api/mcp/oauth/flows/{flow_id}`(13114, Token);GET `/api/mcp/oauth/callback/{server_name:path}`(13126, **无认证 OAuth 回调**);PUT `/{name}/enabled`(13173);GET `/api/mcp/catalog`(13195);POST `/catalog/install`(13280)。

**配对与 Webhook 管理**：GET `/api/pairing`(13382);POST `/approve`(13391, 含锁定文案)/revoke(13413)/clear-pending(13427);GET `/api/webhooks`(13476);POST `/enable`(13492);POST `/api/webhooks`(13513, 铸 HMAC secret);DELETE `/{name}`(13567);PUT `/{name}/enabled`(13584)。

**Skills 与 Hub**：GET `/api/skills`(15488);PUT `/toggle`(15520);GET `/content`(15560);POST `/api/skills`(15579, 校验后创建，**绕过 agent 写审批门**——dashboard 操作者视为已亲自批准);PUT `/content`(15598);hub install/uninstall/update(14354/14379/14402);GET hub sources/search/preview/scan(14478/14551/14602/14667, 502 上游)。

**Profiles**：GET `/api/profiles`(15022);POST(15034, 建/克隆);GET/POST `/active`(15147/15168);GET `/{name}/setup-command`(15188);POST `/{name}/open-terminal`(15193);PATCH `/{name}`(15247, 改名);DELETE(15262);GET/PUT `/{name}/soul`(PUT 使用 owner-only 原子写);PUT `/{name}/description`(看板路由信号);PUT `/{name}/model`;POST `/{name}/describe-auto`。

**工具/工具集**：GET `/api/tools/toolsets`(15613);PUT `/{name}`(15668);GET `/{name}/config`(15712);GET `/{name}/models`(15887);PUT `/{name}/model|provider|env`(15953/16000/16138);POST `/{name}/post-setup`(16198);GET `/api/tools/terminal/backends`(16411);PUT `/backend`(16449);GET `/api/tools/computer-use/status`(16490);POST `/permissions/grant`(16504, 仅 macOS)。

**分析**：GET `/api/analytics/usage`(16771);GET `/api/analytics/models`(16952)。

**Dashboard UI**：GET `/api/dashboard/themes`(18997, **公开**);PUT `/theme`(19032);GET/PUT `/font`(19057/19071);GET `/api/dashboard/plugins`(19252, **公开**，仅已启用清单);GET `/plugins/rescan`(19292);以下全 Token——GET `/plugins/hub`(19424)、POST `/agent-plugins/install`(19435)、POST `/{name:path}/enable|disable|update`(19464/19476/19488)、DELETE `/{name:path}`(19501)、PUT `/plugin-providers`(19519)、POST `/plugins/{name:path}/visibility`(19541);GET `/dashboard-plugins/{plugin}/{path}`(19564, **无认证**静态资源, 后缀允许清单+防穿越)。

**SPA**（`mount_spa` L18590，调用于 19783）：GET `/assets/{filename}.css`(18695, X-Forwarded-Prefix 重写);StaticFiles `/assets`;GET `/{full_path:path}`(18713, 兜底 index.html，回环模式注入 `__HERMES_SESSION_TOKEN__`;未匹配 `/api/*` 回 404 JSON;`HERMES_SERVE_HEADLESS=1` 变体一律 404)。

### 2.11 挂载的 Router 汇总

| Router | 挂载点 | 前缀 | 来源 |
|---|---|---|---|
| `_memory_oauth_router` | L301 | `/api/memory/providers` | `hermes_cli/memory_oauth.py` |
| 各 dashboard 插件 `router` | L19760（`_mount_plugin_api_routes`） | `/api/plugins/{name}` | 插件 `dashboard/plugin_api.py`;安全门见 API-EXTENSION.md §6.2 |
| `_dashboard_auth_router` | L19774 | 根 | `dashboard_auth/routes.py`（§2.5） |
| `_owner_mobile_router` | L19780 | 根 | `dashboard_auth/owner_mobile.py`（§2.6） |

---

## 3. Collaboration 插件 API（前缀 `/api/plugins/collaboration`）

`plugins/collaboration/dashboard/plugin_api.py`（14,773 行，53 条路由）。状态存 `~/.hermes/collaboration/rooms.json` 与 `single.json`（`_STATE_LOCK` 保护）;router 带 lifespan（启动时 `reconcile_stale_hosted_turns`、恢复托管工作流、恢复移动端写审批）。它承载 dashboard「群聊与工作流」页与 **iOS 移动门面**。

**认证**：默认继承 dashboard 全栈（§2.2）;`/connector/*` 子树额外要求 `_require_connector`(L457)——`Authorization: Bearer <token>` + `X-Connector-ID` 头（hmac 匹配配置的 connector 凭据;未配置 503，不符 401），并经 `register_optional_token_prefix("/api/plugins/collaboration/connector", required_scope="collaboration:connector")` 允许服务型调用者绕过 Cookie 门（provider 名 `collaboration-connector`，token 文件 `/etc/hermes-agent/collaboration-connector-token`）。

### 3.1 Connector 协议（远程 worker 拉取执行;契约版本 2，租约 60s，单拉上限 20，附件上限 64MB）

| 方法 | 路径 | 行 | 用途 | 错误 |
|---|---|---|---|---|
| GET | `/connector/health` | 10405 | 存活 + 能力握手（contract_version 等） | 401/503 |
| GET | `/connector/runs/{remote_run_id}/attachments` | 10454 | 列已认领 run 的输入附件 | 401/403/409 |
| GET | `/connector/runs/{remote_run_id}/attachments/{file_id}` | 10469 | 下载附件 | +404 |
| POST | `/connector/runs/pull` | 10503 | 认领待办 hosted turns（租约 + 每认领 `claim_token`） | 401/503 |
| POST | `/connector/runs/ack` | 10567 | 交付终态结果并封印认领 | 401/403/409/422 |
| POST | `/connector/runs/status` | 10620 | 非终态检查点（进度/游标） | +404 |
| POST | `/connector/runs/fail` | 10629 | 终态失败上报 | 401/403/409/422 |
| POST | `/connector/cancellations/pull` | 10640 | 拉取取消请求 | 401 |
| POST | `/connector/cancellations/cancel-ack` | 10714 | 确认取消 | 401/403/409 |
| POST | `/connector/artifacts` | 10725 | 上传产物（CAS 意图 + 回滚;头 X-Claim-Token/X-Remote-Run-ID/X-Relative-Path/X-Filename/X-Content-SHA256） | 401/403/409/413/422 |

认领校验：`_validate_connector_claim`(10152, 403 身份不符)、`_require_remote_run_claim`(10158, 409 认领丢失/封印/租约过期)。

### 3.2 路由/单聊/hosted turns（会话认证;owner 作用域）

| 方法 | 路径 | 行 | 用途 | 错误 |
|---|---|---|---|---|
| GET | `/profiles` | 11039 | 可路由 profile 列表 | — |
| POST | `/route` | 11044 | 消息→profile 分类（路由模型） | 422 |
| GET | `/single/conversations` | 11107 | 会话列表（owner 作用域） | — |
| POST | `/single/conversations` | 11149 | 创建（client_id 幂等；跨平台文件名安全正则 `chat_[A-Za-z0-9._-]{8,245}`） | 400、422 |
| POST | `/single/conversations/adopt` | 11186 | 收编既有 CLI 会话 | 400/422 |
| GET | `/single/conversations/{id}` | 11230 | 详情 + 消息 | 404 |
| PATCH | `/single/conversations/{id}` | 11254 | 重命名 | 400/404 |
| GET/POST | `/single/conversations/{id}/attachments` | 11276/11290 | 列/传附件（raw body + X-Filename/X-Upload-ID；可选 X-Message-ID/X-Profile/X-Turn-ID 会写入文件记录） | 400/409/410/413/422 |
| GET | `/single/conversations/{id}/attachments/{bucket}/{path:path}` | 11350 | 下载/预览（bucket ∈ uploads/outputs） | 403 逃逸、404 |
| POST | `/single/conversations/{id}/record` | 11377 | 只记消息不跑 agent | 404/422 |
| POST | `/single/conversations/{id}/runtime-session` | 11469 | 绑定/更新运行时会话映射 | 404/422 |
| GET | `/single/conversations/{id}/hosted-events` | 11507 | **SSE** 托管回合事件流（游标经 query 或 `Last-Event-ID`） | 404/422 |
| POST | `/single/conversations/{id}/enqueue` | 11711 | 入队一个 hosted turn（request_id+指纹幂等） | 400/404/409/422 |
| POST | `/single/conversations/{id}/hosted-turns` | 12013 | 直接开一个 hosted turn | 404/409/422 |
| POST | `/single/conversations/{id}/hosted-turns/{turn_id}/cancel` | 12116 | 请求取消 | 404 |
| POST | `/single/conversations/{id}/hosted-turns/{turn_id}/interventions` | 12137 | 运行中注入操作者指令（须 @成员） | 400/404/409/422 |
| POST | `/single/conversations/{id}/hosted-turns/{turn_id}/retry` | 14672 | 重试失败回合 | 404/409 |
| DELETE | `/single/conversations/{id}` | 12500 | 删除会话与状态 | 404 |
| POST | `/single/conversations/{id}/messages` | 12554 | 同步（非托管）回合 | 404/422 |

### 3.3 群聊房间

| 方法 | 路径 | 行 | 用途 | 错误 |
|---|---|---|---|---|
| GET / POST | `/rooms` | 12608/12629 | 列 / 建房间 | 400 |
| GET / DELETE | `/rooms/{room_id}` | 12647/12671 | 详情 / 删除 | 404 |
| POST | `/rooms/{room_id}/messages` | 12700 | 发消息并向成员 profile 扇出（request_id 幂等） | 400/404/409/422 |
| POST | `/rooms/{room_id}/hosted-turns/{turn_id}/cancel` | 12889 | 取消房间托管回合 | 404 |

### 3.4 文件/产物库

POST `/single/conversations/{id}/artifacts`(13677, 登记产物, 400/403/404/422);POST `/files`(13753, 上传, 400/413/422);GET `/files`(13810, 搜索：q/date_from/date_to/source/file_type/status/limit/offset；兼容 iOS 的 `type` 作为 `file_type` 别名，显式 `file_type` 优先);GET `/files/{file_id}`(13849, 404);GET `/files/{file_id}/download`(13857);DELETE `/files/{file_id}`(13882)。

### 3.5 移动门面（iOS;会话分叉/世系/压缩 + **写审批** + 运行时运行）

| 方法 | 路径 | 行 | 用途 | 错误 |
|---|---|---|---|---|
| GET | `/mobile/conversations/{id}/session-state` | 14099 | 上下文 + 可分叉点（lineage、branchable_messages） | 404、409 无运行时会话 |
| POST | `/mobile/conversations/{id}/messages/{message_id}/fork` | 14143 | 按消息分叉（子 id `chat_branch_{digest}`;幂等） | 404/409/422 |
| POST | `/mobile/conversations/{id}/compress` | 14242 | 压缩会话上下文（幂等） | 404/409/422 |
| POST | `/mobile/sessions/{session_id}/fork` | 14363 | 裸会话分叉 | 404/409/422/500 |
| GET | `/mobile/sessions/{session_id}/lineage` | 14385 | 会话世系树 | 404/422 |
| GET | `/mobile/sessions/{session_id}/context` | 14400 | token/上下文用量 | 404/422 |
| GET | `/mobile/write-approvals` | 14424 | 按 profile 列待审写入 | 422 |
| GET | `/mobile/write-approvals/{approval_id}` | 14464 | 审批详情（含 diff） | 404/422 |
| POST | `/mobile/write-approvals/{approval_id}/decision` | 14620 | 批准/拒绝;`Idempotency-Key` 头 + `payload_digest` 绑定（`HERMES_WRITE_APPROVAL_REQUIRE_DIGEST=1` 强制） | 404、409（负载不符/冲突）、422 |
| GET | `/mobile/runtime-runs` | 14734 | 活跃/近期运行列表 | 422 |
| GET | `/mobile/runtime-runs/{run_id}` | 14756 | 单运行详情 | 404 |

错误映射（`_mobile_session_error` L13999）：SessionForkConflict→409、SessionNotFound/ScopeDenied→404、ValueError→422、其余 500。写审批的完整状态机见 ARCHITECTURE.md §4.4。

---

## 4. Gateway REST（aiohttp，`gateway/platforms/api_server.py`）

### 4.1 启用、绑定与认证

- 平台名 `api_server`;启用途径：平台列表配置，或环境变量 `API_SERVER_ENABLED` / `API_SERVER_KEY` 存在即自动启用（优先级 env > config.yaml > gateway.json）。
- 默认 `127.0.0.1:8642`;`extra` 可配 `host/port/key/cors_origins/model_name/model_routes`，env 回退 `API_SERVER_HOST/PORT/KEY/CORS_ORIGINS/MODEL_NAME`。
- **启动护栏**：API key 不足 16 字符拒绝启动;端口冲突为不可重试致命错误;非回环绑定且 `terminal.backend=local` 时告警。
- 认证：`_check_auth`(L1234) 以 `hermes_secret_compare.bearer_matches` 常量时间比较 Bearer;失败回 OpenAI 错误信封 401 `invalid_api_key`。例外：`/health`、`/v1/health` 无认证;`/api/cron/fire` 用 Chronos JWT;`/api/platforms/{platform}/events` 委托适配器 `verify_http_event_request`（fail-closed）。
- 并发闸：`gateway.api_server.max_concurrent_runs`（默认 10，0 关闭）超限 429;drain 中 503 `gateway_draining`。
- 会话头：`X-Hermes-Session-Id`（chat/completions 的转写连续性）;`X-Hermes-Session-Key`（长期记忆作用域;无 key 403，非法 400）。
- 中间件：`[profile_prefix, cors, body_limit, security_headers]`。

### 4.2 路由表（35 条，源自 `_http_route_table()` L1476-1525;每条同时注册 `/p/{profile}` 变体，未知 profile → 404）

| 方法 | 路径 | 用途 | 关键错误 |
|---|---|---|---|
| GET | `/health`、`/v1/health` | 存活（**无认证**） | — |
| GET | `/health/detailed` | 组件级健康（跨容器探测用） | 401 |
| GET | `/v1/models` | 模型列表（含 `model_routes` 别名） | 401 |
| GET | `/v1/capabilities` | 能力发现（endpoints/features 图;`jobs_admin: False`） | 401 |
| GET | `/v1/skills`、`/v1/toolsets` | skills / toolsets 列表 | 401 |
| GET/POST | `/api/sessions` | 列 / 建会话 | 409 `session_exists`、400、503 `session_db_unavailable` |
| GET/PATCH/DELETE | `/api/sessions/{sid}` | 读 / 改 / 删 | 404 `session_not_found`、400 |
| GET | `/api/sessions/{sid}/messages` | 转写 | 404 |
| POST | `/api/sessions/{sid}/fork` | 分叉（SessionDB 世系） | 404、409 |
| POST | `/api/sessions/{sid}/chat` | 阻塞式对话回合 | 404、503 |
| POST | `/api/sessions/{sid}/chat/stream` | SSE 对话回合（事件见 §4.3） | 404 |
| POST | `/v1/chat/completions` | OpenAI 兼容（`model` 经 `model_routes` 路由;流式含 `hermes.tool.progress` 哨兵块） | 400 缺 messages |
| POST | `/v1/responses` | OpenAI Responses（`previous_response_id` 与 `conversation` 互斥→400;`truncation auto→100`） | 404 未知前序 |
| GET/DELETE | `/v1/responses/{response_id}` | 取 / 删已存响应 | 404 |
| POST | `/api/platforms/{platform}/events` | 平台 HTTP 事件入口（`verify_http_event_request` → `dispatch_http_event`;唯一实现者为 google_chat 适配器） | 400、401 验证失败、503 适配器不可用、500 |
| GET/POST | `/api/jobs` | cron 列 / 建（`_scan_cron_prompt` 注入扫描;name≤200、prompt≤5000） | 400、501 cron 不可用 |
| GET/PATCH/DELETE | `/api/jobs/{job_id}` | cron 读 / 改（白名单字段）/ 删（id 正则 `[a-f0-9]{12}`） | 404、400 |
| POST | `/api/jobs/{job_id}/pause·resume·run` | 暂停 / 恢复 / 立即执行 | 404 |
| POST | `/v1/runs` | 启动异步 agent run → 202 `{run_id,status:"started"}` | 400 缺 input、429、503 |
| GET | `/v1/runs/{run_id}` | 运行状态 | 404 |
| GET | `/v1/runs/{run_id}/events` | SSE 生命周期事件（§4.3） | 404 |
| POST | `/v1/runs/{run_id}/approval` | 回应审批（choice: once/session/always/deny） | 409 `approval_not_active`/`approval_not_pending`、404 |
| POST | `/v1/runs/{run_id}/stop` | 协作式停止 | 404 |
| POST | `/api/cron/fire` | Chronos 唤醒 webhook（仅 `_CRON_AVAILABLE` 时注册;**JWT 而非 API key**） | 401 `invalid fire token`;202 |

### 4.3 SSE 事件词汇

`POST /api/sessions/{sid}/chat/stream`（字段 `session_id/run_id/seq/ts`）：`run.started`、`message.started`、`assistant.delta`、`tool.progress`、`tool.started`、`tool.completed`、`tool.failed`、`assistant.completed`、`run.completed`（messages+usage）、`error`、`done`。

`GET /v1/runs/{run_id}/events`：`message.delta`、`tool.started`、`tool.completed`（duration/error）、`reasoning.available`、`approval.request`（choices;命令已脱敏）、`approval.responded`、`run.completed`（output/usage）、`run.failed`、`run.cancelled`。

### 4.4 `/p/{profile}` 多路复用

`gateway.multiplex_profiles` 开启时默认 profile 拥有监听端口，次级 profile 经 URL 前缀访问：`_make_profile_prefix_middleware` 剥前缀存 request;`_resolve_request_profile` 对未配置 profile 回 404 `{"error":"Unknown or unconfigured profile"}`;`_profile_scope` 用 ContextVar 在请求期间切换 per-profile 的 `HERMES_HOME`/state.db/secrets。开关关闭时前缀被忽略。

---

## 5. TUI Gateway（JSON-RPC 2.0，`tui_gateway/server.py`）

### 5.1 传输

| 传输 | 说明 |
|---|---|
| stdio（主通道） | `hermes --tui` 时 Node Ink 前端 ↔ `tui_gateway/entry.py`，换行分隔 JSON-RPC;首帧事件 `gateway.ready`（entry.py:357）;Python stdout 被重定向到 stderr 以保护协议通道;`HERMES_TUI_SIDECAR_URL` 时经 TeeTransport 镜像到 dashboard `/api/pub` |
| WebSocket | dashboard `/api/ws`（web_server.py 18404）→ `tui_gateway/ws.py::handle_ws`;线上格式与 stdio 完全一致;`WSTransport` 以约 33ms 定时器合并 `message.delta`/`reasoning.delta`/`thinking.delta`;断开时 `_close_sessions_for_transport(end_reason="ws_disconnect")` |
| 桌面链路 | Electron 以 `hermes serve --host 127.0.0.1 --port 0` 拉起后端（`apps/desktop/electron/backend-command.ts:21`），客户端连 `ws(s)://host/api/ws?token=...` 或 `?ticket=...` |

stdio 无认证（父进程信任）;WS 认证同 §2.4。**不存在 `/api/rpc` HTTP 桥**——WS 是唯一 JSON-RPC 网络通道。

### 5.2 协议与错误码

- 标准错误：`-32700` 解析、`-32600` 非法请求、`-32602` params 必须为对象、`-32601` 方法不存在、`-32603`/`-32000` 内部/处理器异常。
- 应用错误：`4xxx` 校验/状态（4002 未知键、4004 参数、4005 命令被拦、4009 忙/无待办、4016/4017 未知 action、4018 陈旧 ordinal、4022 标题非法、4090 会话槽上限）;`5xxx` 领域内部错误（5001-5061，如 5023 cron、5030-5034 config/tools/pet/model、5061 projects）。
- 长任务方法列在 `_LONG_HANDLERS`（L178-259，40 个）→ 线程池执行（`HERMES_TUI_RPC_POOL_WORKERS`，默认 8）。
- 事件帧契约：`{"jsonrpc":"2.0","method":"event","params":{"type":<kind>,"session_id":<sid>,"payload":{...}}}`。

### 5.3 方法目录（注册表总数 133 = 122 个字面量 `@method("…")` + 11 个 `@_projects_method`;按域分组，逐方法参数/结果详情见 `server.py` 对应行号。下表分组计数合计 133。）

| 域 | 方法（server.py 行号） |
|---|---|
| session.*（20） | create(5825)、list(5977)、most_recent(6023)、resume(6214)、cwd.set(6590)、active_list(6757)、activate(6795)、delete(6819)、title(6861)、usage(7162)、context_breakdown(7186)、status(8740)、history(8801)、undo(8824)、compress(8852)、save(8972)、close(9027)、branch(9039)、interrupt(9123)、steer(9413) |
| 输入/附件（10） | prompt.submit(9450;4009 子代理运行中、4018 陈旧 ordinal)、prompt.background(11173)、clipboard.paste(10559)、image.attach(10599)、image.attach_bytes(10731)、pdf.attach(10792)、file.attach(11059)、image.detach(11106)、input.detect_drop(11126)、paste.collapse(13624) |
| 阻塞应答桥（5） | clarify.respond(11349)、terminal.read.respond(11354)、sudo.respond(11360)、secret.respond(11365)、approval.respond(11370)——与 §5.4 的 `*.request` 事件按 `request_id` 配对 |
| config.*（3） | set(11395)、get(12420)、show(15487)——键族：model/busy/verbose/approvals.mode/yolo/reasoning/details_mode.*/thinking_mode/compact/statusbar/mouse/indicator/cwd;未知键 4002 |
| projects.*（15） | list/get/create/update/add_folder/remove_folder/set_primary/archive/delete/set_active(12028-12113)、for_cwd(12119)、discover_repos(12234)、record_repos(12246)、tree(12367)、project_sessions(12394) |
| pet.*（15） | info(7398)、info.meta(7424)、cells(7447)、gallery(7552)、select(7635)、remove(7662)、export(7692)、rename(7719)、thumb(7757)、disable(7792)、scale(7806)、cancel(7941)、generate.status(7955)、generate(7986)、hatch(8099) |
| billing/usage/subscription（11） | billing.state(8343)、usage.bars(8408)、subscription.state/preview/change/resume/upgrade(8483-8618)、billing.charge(8622)、charge_status(8648)、auto_reload(8677)、step_up(8699) |
| 委派/子代理（6） | delegation.status(9194)、delegation.pause(9214)、subagent.interrupt(9222)、spawn_tree.save/list/load(9295/9338/9389) |
| 模型/补全/命令（9） | model.options(14130)、model.save_key(14173)、model.disconnect(14259)、complete.path(13828)、complete.slash(14051)、commands.catalog(12895)、cli.exec(13010, 内建拦截清单)、command.resolve(13042)、command.dispatch(13072) |
| slash/shell/进程/reload/setup/预览（10） | slash.exec(14677)、shell.exec(15967;4005 危险命令经 `tools.approval` 拦截、30s 超时 5002)、process.stop/list/kill(12668/12695/12707)、reload.mcp(12730)、reload.env(12829)、setup.status(12585)、setup.runtime_check(12595)、preview.restart(11219) |
| voice/learning/skills/tools/插件/cron/浏览器/回滚/洞察等（24） | voice.toggle/record/tts(14865/14962/15039)、learning.frames/detail/delete/edit(15747-15799)、skills.manage(15804)/reload(15863)、tools.list/show/configure(15526/15557/15597)、toolsets.list(15666)、agents.list(15696)、plugins.list(15465)/manage(15888)、cron.manage(15720;转发 `cronjob()` JSON)、browser.manage(15288)、rollback.list/restore/diff(15086/15116/15163)、insights.get(15058)、terminal.resize(9438)、llm.oneshot(6964) |
| handoff/事实/验证（5） | handoff.request(7023)、handoff.state(7111)、handoff.fail(7138)、project.facts(6067)、verification.status(6085) |
| projects 装饰器族 | 上表 projects 中 11 个方法经 `@_projects_method`(def 11992) 注入项目库连接 |

### 5.4 服务端 → 客户端事件目录

流式：`message.start/delta/complete`、`message.interim`（受 `display.interim_assistant_messages` 门控）、`reasoning.delta/available`、`thinking.delta`（WS 下 delta 类合并发送）。
工具：`tool.start/complete/generating/output_risk`;MoA：`moa.reference/aggregating`。
**阻塞请求**（与 §5.3 应答桥配对）：`approval.request`（命令经 `gateway.run._redact_approval_command` 脱敏）、`clarify.request`、`terminal.read.request`（30s 超时）、`sudo.request`（120s）、`secret.request`。
其他：`gateway.ready`、`notification.show/clear`、`status.update`（含 compacting 重标）、`session.info`、`error`、`reaction`、`preview.restart.progress/complete`、`pet.generate.progress`、`pet.hatch.progress`、`terminal.close`(9954)、`skin.changed`(11955)、`voice.transcript/status`、`browser.progress`、`review.summary`(1674)。

---

## 6. Cron 的重复暴露（两面对照表）

同一 `cron/jobs.py` 存储被两个 HTTP 面各自包了一层 CRUD，动词命名不一致，`/p/{profile}` 又构成第三种路径语法;`POST /api/cron/fire` 在两面各挂一个、共用同一 JWT 校验器（`plugins/cron_providers/chronos/verify.get_fire_verifier`）。**改 cron 接口必须同步两处。**

| 操作 | Dashboard（web_server.py） | Gateway（api_server.py） | 底层调用 |
|---|---|---|---|
| 列任务 | GET `/api/cron/jobs` | GET `/api/jobs` | `list_jobs` |
| 单任务 | GET `/api/cron/jobs/{id}` | GET `/api/jobs/{id}` | `get_job` |
| 建任务 | POST `/api/cron/jobs` | POST `/api/jobs` | `create_job` |
| 改任务 | **PUT** `/api/cron/jobs/{id}` | **PATCH** `/api/jobs/{id}` | `update_job` |
| 删任务 | DELETE `/api/cron/jobs/{id}` | DELETE `/api/jobs/{id}` | `remove_job` |
| 暂停/恢复 | POST `.../pause`、`.../resume` | 同名 | `pause_job`/`resume_job` |
| 立即执行 | POST `.../trigger` | POST `.../run`（**动词分歧**） | `trigger_job` |
| 唤醒 webhook | POST `/api/cron/fire`(12513, 公开路径+JWT) | POST `/api/cron/fire`(4346, 绕过 API key) | `fire_due`（CAS 认领去重） |
| 仅 dashboard 有 | `/runs` 历史、`/delivery-targets`、`/blueprints`、`/blueprints/instantiate` | — | — |
| 仅 gateway 有 | — | `/p/{profile}` 前缀变体 | — |

差异细节：dashboard 侧经 `_call_cron_for_profile`(12178) 显式选 profile 存储、`_find_cron_job_profile`(12206) 反查归属;gateway 侧模块顶层直连 `cron.jobs`（import 失败时 501）、变更后 `_notify_cron_provider_jobs_changed`、创建时 `_scan_cron_prompt` 做提示注入扫描。

---

## 7. 仓库内全部网络监听器（21 个）

| # | 监听器 | 位置 | 框架 |
|---|---|---|---|
| 1 | Dashboard | `hermes_cli/web_server.py:283` | FastAPI/uvicorn |
| 2 | Gateway REST | `gateway/platforms/api_server.py:5402` | aiohttp |
| 3 | 凭据代理（`hermes proxy`，默认 127.0.0.1:8645;丢弃客户端 Authorization 换真实上游凭据;路径允许清单） | `hermes_cli/proxy/server.py:96` | aiohttp |
| 4 | 托管节点恢复服务（独立 token 认证控制面） | `hermes_cli/managed_node_recovery_service.py:26` | http.server |
| 5-8 | 平台 webhook：webhook / whatsapp_cloud / msgraph_webhook / bluebubbles | `gateway/platforms/*.py` | aiohttp |
| 9-14 | 插件平台 webhook：wecom / teams / sms / raft / feishu / line | `plugins/platforms/*/adapter.py` | aiohttp |
| 15 | Google Meet node 桥 | `plugins/google_meet/node/server.py:197` | websockets |
| 16 | Photon sidecar | `plugins/platforms/photon/sidecar/index.mjs` | Node http（127.0.0.1） |
| 17 | WhatsApp bridge | `scripts/whatsapp-bridge/bridge.js:1098` | Express（127.0.0.1） |
| 18-20 | OAuth 回环单次监听：MCP（`tools/mcp_oauth.py`）、Spotify（`hermes_cli/auth.py`）、Honcho（`plugins/memory/honcho/oauth_flow.py`） | 各文件 | http.server |
| 动态清单 | iOS MCP server（仅显式 `--transport streamable-http` 时监听） | `hermes_cli/ios_mcp_server.py` | MCP Python SDK v2 |

stdio-only（非监听器）：`mcp_serve.py`（`hermes mcp serve`，暴露 conversations_list/messages_read/events_poll·wait/messages_send/permissions_list_open·respond/channels_list 等 MCP 工具）、`acp_adapter/server.py`（「ACP is stdio-only, local-trust」）。

---

## 8. 覆盖范围声明

**已完整枚举**：dashboard 全部 240 个路由装饰器（优先域详表、其余域压缩枚举于 §2.10，无遗漏）、4 个挂载 router、5 个 WS 端点、公开路径清单、collaboration 插件全部 53 条路由、gateway api_server 全部 33 条路由、TUI 全部 133 个 RPC 方法名与事件目录。

**未逐项展开（明确声明）**：

1. §2.10 与 §5.3 的压缩条目未列每个请求/响应体字段——字段名以源码 pydantic 模型/处理器为准（行号已给锚点）;
2. 各平台 webhook 监听器（§7 #5-14）的内部路由与验签细节未枚举（属各平台适配器实现，非公共 API 面）;
3. collaboration 之外若有第三方 dashboard 插件在用户目录提供 `plugin_api.py`，其路由不在本文范围（挂载机制见 API-EXTENSION.md §6）;
4. TUI 每个 RPC 方法的完整参数/返回 schema 未逐一展开（`server.py` 行号锚点已给;`config.set/get` 的键族已列）;

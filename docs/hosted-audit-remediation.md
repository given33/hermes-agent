# 云托管审计修复记录

本文记录针对服务器托管服务审计表（P0~P3）的修复与落地方式。代码修改集中在：

- `plugins/collaboration/dashboard/plugin_api.py` — 托管 API、连接器端点、SSE、审计、指标
- `deploy/dbb3/dbb3_cloud_connector.py` — DBB3/PC/HK 共享连接器实现
- `deploy/hk/` — HK worker 的独立 profile、skills、凭据、状态和自动部署
- `hermes_cli/dashboard_auth/registry.py` — 移动端静态 key 降级
- `hermes_cli/dashboard_auth/audit.py` — 连接器操作审计事件
- `scripts/backup_hosted_state.py` — 托管状态备份脚本
- `docs/hosted-audit-remediation.md` — 本文档
- `hermes_cli/dashboard_auth/client_ip.py` / `middleware.py` — 可信代理 IP
  与公共路径边界校验
- `gateway/platforms/bluebubbles.py` — webhook 空凭据拒绝与常量时间校验

## P0 安全

| # | 差距 | 修复 |
|---|------|------|
| 1 | `/profiles`、`/route` 无认证 | 新增 `_require_owner()`，两个端点强制校验会话/Token；`/route` 增加 `_enforce_route_body_size()`（256 KiB 上限）和独立 `_ROUTE_RATE_LIMITER`（30 次/分钟/调用方）。 |
| 2 | 应用层无限流、SSE 无上限 | 新增进程内滑动窗口限流 `_SlidingWindowRateLimiter`；所有 `/connector/*` 按 connector+action 限流（默认 120 次/分钟）；`/connector/stream` 每 connector 最多 4 条连接，空闲超过 6 小时自动关闭。 |
| 3 | 移动端静态 key | `HERMES_MOBILE_API_KEY` 在检测到 owner-mobile 短生命周期 token 认证时自动忽略并告警，避免静态 key 后门；官方 `owner_mobile` / `native_flow` 提供 access/refresh token 与单设备撤销。 |
| 4 | 连接器 token 静态、无轮换 | 服务端支持双 secret 轮换（`HERMES_COLLABORATION_CONNECTOR_TOKENS` 可写 `["current","previous"]` 或 `{"token":..., "previous_token":...}`）；连接器在 401 时自动重新读取 token 文件并重试一次，无需重启。 |

## P1 可靠性与效率

| # | 差距 | 修复 |
|---|------|------|
| 5 | 上行传输延迟 | worker 使用认证 WebSocket `/api/plugins/collaboration/worker/ws` 传输进度和结果；连接器/队列保留 SSE 与轮询作为断线重连和持久化重放兜底。 |
| 6 | SSE 无事件重放 | `/connector/stream` 为每个事件分配单调 `id` 并保留最近 200 条历史；连接器重连时发送 `Last-Event-ID`，服务端重放断线期间事件。 |
| 7 | 租约无独立续期 | 连接器新增 `start_heartbeat()`，每 30s 对活跃远程任务发送 lightweight status，避免 >90s 无上报导致租约过期。可用 `HERMES_CONNECTOR_HEARTBEAT_SECONDS` 调整。 |
| 8 | artifact 上传无断点续传 | 连接器上传前先拉取该 run 已有附件，若 `sha256` 已存在则标记去重并跳过整包上传；服务端仍校验 SHA-256 与大小上限。 |

## P2 可观测性与运维

| # | 差距 | 修复 |
|---|------|------|
| 9 | 无统一指标 | 新增 `/connector/metrics`（进程内按 connector 统计各操作计数）；连接器 status payload 增加 `latency_ms`、`lease_conflicts`。 |
| 10 | 无操作审计 | `audit.py` 新增 `CONNECTOR_*` 事件；pull/ack/status/fail/cancel/cancel-ack/artifact/download 均写入 append-only `dashboard-auth.log`。 |
| 11 | 无端到端拨测 | 连接器新增 `--probe-full`：验证 health、pull、cancel-pull、metrics 全链路。 |
| 12 | 核心状态备份 | 新增 `scripts/backup_hosted_state.py`，使用 SQLite online backup 一致备份 `library.sqlite3`、`mobile-auth.db`，并原子快照 `single.json`/`rooms.json`。 |

## P3 架构演进

### 13. 托管运行时组件

- `managed_scope` 已存在于 `hermes_runtime/managed_scope.py` 与 `hermes_cli/managed_scope.py`，云托管节点升级/配置下发时可直接用 `apply_managed_overlay()` 覆盖 `config.yaml` 与受管 env。
- `gateway/drain_control.py` 的 drain marker 已接入连接器：检测到 `HERMES_HOME/.drain_request.json` 后，连接器停止领取新 run，但仍完成/取消已有 run，实现“排空再换版本”。
- `gateway/scale_to_zero.py` 与 `plugins/cron_providers/chronos/` 已随官方代码存在；如需托管节点空闲下电/托管定时任务，按现有配置启用 `scale_to_zero` 和 `cron.provider: chronos` 即可，无需重复实现。

### 15. 消息平台接入

官方 relay 契约已开源在 `docs/relay-connector-contract.md`，`gateway/relay/` 全套服务端可用。若托管 agent 需要接入 Telegram/Discord，可将 `GATEWAY_RELAY_URL` 指向按契约实现的 connector 服务端，或直接使用官方 gateway relay 的 enroll + 自有 IdP 流程，避免自研消息桥。

### 16. Hosted workflow role boundary

托管协作现在只有一个服务器本地调度员和三个独立 worker lane：DBB3、PC/WSL、HK。
监督者、审阅者和汇报者不再创建模型回合；调度员拆分任务，worker 直接提交结果和证据，服务器只执行确定性状态校验与结果聚合。旧状态中的相关字段会在加载时结构化迁移并删除，普通用户文本不会被关键词扫描。
托管子进程创建 `AIAgent` 时还显式启用 `skip_background_review=True`，因此 worker
完成后不会偷偷启动官方普通会话使用的后台 memory/skill review fork；该 fork
仍保留给非托管的本地 CLI 会话。

### 17. Mobile Bot Mode relay bridge

移动端没有桌面插件里的 `message_agent` 工具运行时，因此新增的
`/api/bot-mode/relay/roster` 与 `/api/bot-mode/relay/send` 只做薄 REST
适配：读取桌面维护的 `bot_relay/roster.json`，并直接调用官方
`tools.bot_relay.enqueue_envelope()`。目标歧义、连接器离线、信封 TTL、原子
outbox 写入和回复等待仍由官方 relay helper 负责；服务端不会向 iOS 暴露其他
连接的 token。

Bot avatar 生成同样保持官方链路：`/api/bots/{name}/assets/avatar/generate`
调用注册的 `image.generate` handler，再把返回的 data URL 交给
`profiles.set_asset`；生成器不可用时返回明确失败，不写入半成品。

Petdex 头像选择也不重复实现官方逻辑：`/api/bot-mode/pets/gallery` 直接
代理注册的 `pet.gallery`，`/api/bots/{name}/assets/avatar/pet` 使用官方
 `pet.thumb` 裁剪首帧后再调用 `profiles.set_asset`。移动端只传 slug 和可选
 manifest URL，不接触 petdex 凭据或本地存储细节。

### 18. 安全回归（2026-08-29）

- 登录限流和审计日志统一使用可信代理感知的 `client_ip`：默认忽略
  `X-Forwarded-For`，只有连接对端命中 `HERMES_TRUSTED_PROXIES` 时才从
  右向左解析代理链；伪造请求头不能创建新的限流桶。
- Dashboard 公共路径只允许完整路由或目录边界，避免类似
  `/auth/logout-all`、`/api/auth/providers-evil` 的前缀碰撞。
- BlueBubbles webhook 缺少配置密码、提交空 token 或 token 不匹配时均返回
  401，并使用 `hmac.compare_digest`；既有消息解析和 mention gating 行为不变。

### 19. HK 受管节点状态与恢复闭环（2026-08-29）

- `hermes_cli/managed_nodes.py` 的状态归一化现在识别 `hk`、`hk-worker`、
  `hk-primary` 和香港别名。状态中出现 HK heartbeat/gateway 时，
  `/api/managed-nodes/status` 会在保持 DBB3/WSL 行顺序与字段兼容的前提下追加
  独立的 HK 行，并按 runtime/metrics 各自的时间戳判定 freshness；不会把缺少
  明确 worker-ready/gateway-alive 的 HK 设备误报为在线。
- `/api/managed-nodes/recover`、`recovery-hook` 和 recovery receiver 的
  allow-list 已接受 `hk`，并且配置了 HK 专用 recovery URL 时，自动恢复会把
  HK 纳入目标集合；没有该 URL 的旧配置仍只发送 DBB3/WSL，避免改变既有请求
  契约。
- HK 暂未伪装成 managed-installation receiver：`installation_urls` 仍严格限于
  已部署并有认证 receiver 的 DBB3/WSL；HK 代码、profile、凭据、状态和版本
  同步由 `deploy/hk/` 与 fabric auto-update 独立维护。这避免“配置接受 HK、
  但实际安装端点不存在”的半支持状态。

## 验证

### 2026-08-29 上游同步核验

- 已从 `upstream/main` 同步到官方最新提交 `299c652a66`（在上一轮六个
  提交基础上又包含 `3b362acf48`、`57746cbb84`、`067e58238e`、
  `299c652a66` 四个官方提交：预览文件下载、远程 inline preview 和
  user-stories 维护），并在本地以合并提交 `089108e956` 整合。
  此前的合并提交 `f7891d6aaf`、
  `08574b9995`、`2d6f08be4a` 以及最终修复提交 `9d6aa62ef8` 均保留；
  推送前后均以 `git rev-list --left-right --count HEAD...upstream/main`
  核验右侧差异为 `0`，表示没有漏掉上游新增提交。
- 本次合并冲突仅出现在 `agent/agent_runtime_helpers.py` 的桌面预览工具清单：保留官方 `desktop_preview`、`drive_preview`、`annotate_preview`，并保留旧实现代码但不再把已下线的 `read_preview` 注册为独立后钩子工具；未覆盖 HK worker、三端部署、WebSocket worker 通道或调度员/worker 角色边界。
- 合并后重新执行协作、云文件、受管资源、云部署资产和安装拓扑回归：`346 passed, 7 skipped, 44 subtests passed`。
- 合并后的 Bot Mode 官方接口回归仍通过：`tests/hermes_cli/test_web_server.py -k "bot_"` 为 `8 passed`（含 relay、头像生成与 Petdex 选择）；部署资产回归为 `60 passed`（含 worker connector 隔离断言）。
- 上游同步采用直接验证后的 fast-forward/merge 推送到本项目 `main`，不创建审阅者或 Codex 审查回合；推送完成后应再次确认 `HEAD...origin/main` 为 `0 0`。

### 2026-08-29 二次基础回归

- iOS Hermes：`pnpm typecheck`、`pnpm contract:check` 与全量 `pnpm test` 均通过，`805 passed / 0 failed`。
- 后端合并受影响回归：`495 passed / 0 failed`；Windows 高分辨率进度围栏回归与可选
  `flush_token_counts` 数据库适配器均已修复，随后针对性测试通过且不再产生线程未处理异常。
- `uv run ruff check` 覆盖本次运行时、认证安全和测试改动，全部通过。Windows 环境未执行
  Xcode 原生编译；iOS 原生编译仍需 macOS CI 或签名构建机验证。
- 推送后的部署/角色快速回归：`87 passed`（云部署资产 + DBB3/worker 会话缓存），Bot Mode
  官方接口 `8 passed`，BlueBubbles 与公共路径边界安全回归 `27 passed`。后端 `origin/main`
  与本地 `HEAD` 已一致，且 `HEAD...upstream/main` 右侧为 `0`。
- 上游随后新增的 Vertex 凭据快照和 list-shaped streaming delta 修复也已再次合并；对应
  `tests/agent/test_vertex_adapter.py tests/run_agent/test_streaming.py` 回归为 `49 passed`。
- 随后新增的 gateway turn-hold 上限、i18n deferred notice 与 `hygiene_max_turn_hold_seconds`
  配置也已合并；`tests/gateway/test_session_hygiene.py` 回归为 `17 passed`。
- HK 受管节点状态/恢复闭环回归：
  `tests/hermes_cli/test_managed_nodes.py tests/hermes_cli/test_managed_node_recovery_watchdog.py tests/hermes_cli/test_web_server.py`
  为 `199 passed, 5 skipped`；新增测试覆盖 HK heartbeat 可见性、旧 DBB3/WSL
  行兼容、HK recovery URL/receiver 校验，以及明确的 worker-ready/alive 在线判定。
- 角色迁移 product-chain 回归为 `19 passed`；旧测试中的 `review_verdict`
  断言已改为 `validation_summary`，并显式确认没有 reviewer/supervisor 模型调用。
- 最终推送核验（提交 `a871fac6f2`）：`HEAD...origin/main = 0 0`；
  `HEAD...upstream/main` 的右侧差异为 `0`（本地包含全部官方提交），
  且本地没有未提交改动。

本次同步与角色重构已运行并通过：

```bash
.venv/Scripts/python.exe -m pytest tests/deploy/test_dbb3_connector_session_cache.py -q
.venv/Scripts/python.exe -m pytest tests/plugins/test_collaboration_cloud_files.py -q
uv run pytest -q tests/plugins/test_collaboration_dashboard.py::CollaborationDashboardTests::test_workflow_has_only_dispatcher_and_workers
uv run pytest -q tests/plugins/test_collaboration_dashboard.py::CollaborationDashboardTests::test_hk_worker_is_a_distinct_target_and_connector
uv run pytest -q tests/hermes_services/test_worker_channel.py tests/hermes_runtime/test_harness_runtime_primitives.py
uv run pytest -q tests/deploy/test_cloud_deployment_assets.py
uv run ruff check hermes_runtime hermes_services plugins/collaboration/dashboard hermes_cli tools
```

部署后建议：

1. 更新连接器 token 文件支持双 secret 轮换。
2. 设置 `HERMES_CONNECTOR_HEARTBEAT_SECONDS=30`（默认已为 30）。
3. 配置 cron 定期执行 `scripts/backup_hosted_state.py --output-dir ...`。
4. 在升级节点前写入 `.drain_request.json`，待连接器排空后再替换版本。

### 2026-08-29 续审：上游同步与 iOS 官方 Git 对齐

- 本次再次执行 `git fetch upstream main`，官方 `upstream/main` 为
  `b1ff8722a53ee223485ac9804945acf07ef5c601`（短 SHA：`b1ff8722a5`）。
  通过无冲突 merge 提交 `94a4bd93fd288a74f0e09682a2f5366b366442f8`
  纳入本地 `main`；上游 optional skills、模型/提示词和桌面行为改动均保留，
  本地 HK、worker channel、Bot Mode 与移动端接口未被覆盖。
- `.github/workflows/deploy-three-endpoints.yml` 的 HK job 在生产变量
  `HERMES_HK_ENABLED=1` 时使用固定 SSH host key 调用 `deploy/hk/install-hk-worker.sh`，
  并验证 HK `hermes-fabric-update.timer` 和 `release.json`；三 worker 共用提交的源代码，
  connector token、profile、skills、state 和 systemd unit 按 DBB3/PC-WSL/HK 分离。
- hosted workflow 的实际路径仍是一个服务器本地 dispatcher + 三个 worker lane；
  `_start_hosted_companion()` 是迁移兼容 no-op，不会创建 supervisor/reviewer 模型回合。
  文件中保留的旧 reviewer/supervisor helper 只服务历史状态迁移/跳过的兼容测试，
  不能由当前 dispatcher 路径调用。
- iOS 已新增 `/git` 原生和 fallback 页面，直接消费官方 Git API 的 status、branches、
  worktrees、review、ship-info、GitHub auth、commit context、rev-parse、PR list、
  review/file diff，并提供暂存、取消暂存、还原、提交、推送、切分支、PR、worktree
  操作。具体对齐矩阵和性能记录见审计目录中的
  `HERMES-FEATURE-ALIGNMENT-MATRIX-2026-08-29.md` 与
  `IOS-HERMES-PERFORMANCE-OPTIMIZATION-2026-08-29.md`。
- 本轮证据：iOS `pnpm test` 为 `809 passed`，`pnpm typecheck` 与
  `pnpm contract:check` 通过；后端上游/部署/worker/WebSocket 聚焦回归 `160 passed`，
  上游 optional-skills/model 聚焦回归 `71 passed`，ruff 通过。Windows 没有 Xcode、
  真机或生产四节点凭据，Swift archive、APNs、真实网络 RTT 和 HK 主机实测仍由 macOS CI
  与生产演练验收。

### 2026-08-29 续审：最新 upstream/main 与托管工作区文件闭环

- 重新 fetch 后发现官方 `upstream/main` 已推进到 `5831d8365aea36fea48f9c3e839827d19ccb9a6c`
  （短 SHA：`5831d8365a`），新增 `ccc367dce0`（platform-hint truth pass、
  gateway universal voice-bubble transcode）、`447217b4cc`（桌面 pane-tab
  关闭按钮预留空间）和 `5831d8365a` 格式化提交。本地以 merge 提交
  `c8acb0520f` 纳入全部官方变更；`git rev-list --left-right --count
  HEAD...upstream/main` 右侧为 `0`。
- 上游通用语音气泡转码在 gateway/platform adapters 中保留官方实现，新增
  `tests/gateway/test_voice_transcode.py` 与既有 TTS 路由回归共 `16 passed`；
  `uv run ruff check` 覆盖运行时、适配器和测试文件全部通过。桌面 pane-tab
  合并时保留本地 `showCloseButton` 兼容开关，同时采纳官方水平 tab runway，
  相关 Vitest 因 Windows 工作区缺少 `@rolldown/plugin-babel` 依赖未能启动，
  需在完整 Node 安装或 CI 环境补跑。
- iOS 原生 Files 现在同时显示 collaboration account file library 和官方
  `/api/files` managed workspace：目录父子导航、分块暂存上传、受限下载/分享、
  文件/目录删除以及 folder.create 均通过官方 API；跨语言 route contract 新增
  `managedFilesJSON` 与四个 `files.managed.*` action，并有 route/native source
  回归覆盖。iOS 全量 `pnpm test` 为 `811 passed / 0 failed`，`pnpm typecheck`
  与 `pnpm contract:check` 通过。

### 2026-08-29 续审：桌面端上游合并冲突修复

- 发现此前同步提交把上游完整的 session-owner route、连接注册表、transcript
  tail、消息渲染和 Electron IPC 实现截成旧版，导致桌面 `typecheck` 出现 193
  个缺失导出/变量错误。已按 `upstream/main` 恢复官方实现，并保留本地 HK/worker
  部署、WebSocket 通道、Bot Mode 和 iOS 改动；当前 `npm run --workspace apps/desktop
  typecheck`（renderer、Electron、e2e 三套 tsconfig）通过。
- 会话路由、连接生命周期、预览浏览器栏、clarify 控件五组聚焦 Vitest 共
  `114 passed / 0 failed`。完整 UI suite 未作为发布门禁：Windows 下仍有历史
  shell/Canvas 环境测试需要 Linux/macOS CI；不会把这些平台限制误报为功能已验证。

### 2026-08-30 续审：官方 hosted command bridge 与 reviewer 退役

- 删除 CLI、Gateway、TUI 的 `/review` 执行入口和 `agent/review_engine.py` 专用模块；
  Kanban review 列历史记录仍可读取/迁移，但 `review_dispatch_enabled()` 固定 fail-closed，
  生产工作流只有 `hermes-manager` 调度员与 DBB3、PC/WSL、HK 三个独立 worker。
- 新增 `run_hosted_gateway_command()` 与 account-scoped
  `/mobile/conversations/{conversation_id}/commands`：`bg`/`btw` 调官方
  `prompt.background`/`prompt.btw`，`busy` 调官方 `config.set`；异步结果经持久 reader
  callback 写入 canonical event 和 assistant message，iOS WebSocket/SSE 游标可重放。
- `btw.complete` 已注册到 hosted event protocol；`scripts/upstream_sync_gate.py` 在任何
  自动 upstream merge 推送前检查官方命令、HK 部署资产、三 worker 角色和 reviewer
  fail-closed 边界。后端 dashboard/TUI/command 回归 `877 passed, 7 skipped`，hosted
  runtime/event protocol `22 passed`，ruff/compileall/sync gate 通过。

发布锚点：官方 `upstream/main` `1e21fe8624` 已合并，后端提交
`1a13129011` 已推送到 `origin/main`；该提交同时包含部署器对 merge 暂存删除文件
的防护，避免退役 reviewer 模块再次被误判为运行时缺失。

上游后续提交 `835a913ffd`、`0ffad55e09`、`4209d371aa`（压缩失败冷却、Portal
推荐模型校验）已在 `273111e56f` 追加合并并通过上游新增回归 `61 passed`；同步门禁
再次通过后已推送到 `origin/main`。

### 2026-08-30 续审：统一会话状态接口

- iOS 的 Sessions 页面是账户会话与官方运行时会话的统一索引。此前账户行的归档、
  置顶、未读、批量删除和导出会误发到 `/api/sessions`，而官方 `official:*` 占位符
  也会被原样当作 SQLite session id，真实网关上分别表现为 404 或“会话不存在”。
- Collaboration 后端的单聊 PATCH 现在支持 `archived`、`pinned`、`unread`，并把它们
  存在 `session_*` 命名空间，避免与内部归档占位行的 `archived` 标记冲突；公开快照
  映射为 iOS 统一 Sessions 所需的布尔字段。
- iOS action bridge 会解析官方占位符、按 profile 分组批量删除，并将账户会话删除/状态
  修改路由到 collaboration API；账户导出从 `/single/conversations/:id` 读取，官方
  导出仍走 `/api/sessions/:id/export`。相关 API surface、路由和后端回归已补齐。
- 本轮验证：后端 collaboration dashboard `200 passed, 7 skipped, 44 subtests`，
  cloud-files `74 passed`；iOS `pnpm test` `819 passed / 0 failed`，TypeScript 与
  SwiftUI contract check 通过，ruff 通过。当前后端发布提交为 `9e05ecd9a0`。

### 2026-08-30 续审：适配器竞态、同步恢复与 WhatsApp bridge 鉴权

- Discord backfill/live ingress 在策略过滤后执行最终原子 dedup claim；WeCom callback 入队
  或 handler 失败会释放 claim，供应商重试可以恢复，不会因提前标记而静默丢消息。
- Matrix 将按 homeserver/user/device 绑定的 `next_batch` checkpoint 原子持久化，重启使用
  `since` 增量恢复；恢复中的离线房间事件跳过 startup-grace 丢弃，损坏或跨账号 checkpoint
  fail-closed。
- WhatsApp Node bridge 的 `/send`、`/messages`、媒体、poll、location、typing、chat、read
  控制面要求 profile 独立 Bearer token，token 位于 `session/bridge.token`（0600）；`/health`
  暴露 `authEnabled`，旧的无鉴权 bridge 不再复用。Python 适配器和 standalone sender 统一
  发送 token。
- Connector SSE 对时钟回拨/进程重启造成的 stale cursor 重放保留权威历史；mobile
  managed-resource SSE 每 owner 限制 8 路、单页 200 条，并把空闲轮询从 1 秒自适应退避到
  5 秒。以上均不改变 iOS WebSocket-first hosted chat 契约。
- 后端 `e8c73a4408` 已推送；本批整合回归 `394 passed, 8 skipped, 44 subtests`，ruff、
  py_compile、Node syntax 检查通过。Windows 仍不能替代 macOS Xcode、真机、APNs 及
  HK/DBB3/PC 生产网络验收。

### 2026-08-30 续审：协作成员 hosted-turn quota

- 协作房间非 owner 成员最多同时运行 4 个 hosted turn；`sender_id` 随 room request 持久化，
  新 request id 不能绕过资源边界，owner 仍走正常 gateway quota。
- `f3dd45f11c` 已推送；随后 `40b83dfe50` 修复达到上限时同一 request 仍需幂等 replay 的
  边界。完整 dashboard 回归 `201 passed, 7 skipped, 44 subtests`，新增跨账号成员 quota
  测试通过。
- WhatsApp stale-bridge 日志再由 `d6a5450e15` 明确区分鉴权关闭与配置漂移，便于升级后诊断。

### 2026-08-30 续审：缓存并发、ClawHub 下载与密钥脱敏

Bedrock runtime/control client cache 现在由进程内可重入锁保护；同一区域并发冷启动只会
创建一个 boto3 client，reset/invalidate 也在同一锁下执行。辅助客户端 credential rotation
只移除共享 cache entry，不从驱逐路径关闭可能仍在使用的 transport。ClawHub 下载复用
SSRF/网站策略并在 ZIP 解析前限制 16 MiB 传输与展开总量。Agent/runtime 脱敏器先处理带
空格的单/双引号环境变量值，避免只遮首 token；Provider caret range 对 `^0.0.z` 正确限制
在同一补丁版本线。受影响回归 `486 passed, 3 skipped`。

### 2026-08-30 续审：Worker WebSocket 低延迟通道

官方 collaboration 后端早已提供 `/api/plugins/collaboration/worker/ws`，但 DBB3、PC/WSL
和 HK 连接器此前仍只订阅 legacy `/connector/stream` SSE；新任务要等下一轮 REST pull
才会被唤醒。连接器现在以 pinned `websockets.sync.client` 实现官方
`hermes.low-latency.v1` hello/heartbeat/replay 协议，按 `dbb3-worker`、`pc-worker`、
`hk-worker` 独立节点握手，并把 `worker.queued`/steer 事件即时转成同步唤醒；序列游标在
重连时恢复，token 仍只放在握手 header，不进入 URL。

WebSocket 只作为低延迟加速器，REST durable queue、checkpoint 和 legacy SSE fallback
仍保留，因此依赖缺失、代理不支持或显式 `HERMES_CONNECTOR_WORKER_WS=0` 时不会丢任务。
DBB3/HK systemd 模板和共享安装器默认写入 `HERMES_CONNECTOR_WORKER_WS=1`，PC 安装复用
同一模板。新增 handshake、游标、节点映射和缺依赖 fallback 回归；部署资产套件
`61 passed`，连接器专项 `29 passed`，ruff/compileall 通过。

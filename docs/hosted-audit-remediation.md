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

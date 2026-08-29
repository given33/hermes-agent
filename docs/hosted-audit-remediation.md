# 云托管审计修复记录

本文记录针对服务器托管服务审计表（P0~P3）的修复与落地方式。代码修改集中在：

- `plugins/collaboration/dashboard/plugin_api.py` — 托管 API、连接器端点、SSE、审计、指标
- `deploy/dbb3/dbb3_cloud_connector.py` — DBB3/PC/HK 共享连接器实现
- `deploy/hk/` — HK worker 的独立 profile、skills、凭据、状态和自动部署
- `hermes_cli/dashboard_auth/registry.py` — 移动端静态 key 降级
- `hermes_cli/dashboard_auth/audit.py` — 连接器操作审计事件
- `scripts/backup_hosted_state.py` — 托管状态备份脚本
- `docs/hosted-audit-remediation.md` — 本文档

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

## 验证

### 2026-08-29 上游同步核验

- 已从 `upstream/main` 同步到官方提交 `9f90cd438c`，本地以合并提交 `bb468947da` 完成整合；`HEAD...upstream/main` 的右侧差异为 `0`，表示没有漏掉上游新增提交。
- 合并冲突仅出现在 `hermes_cli/profiles.py`：保留本项目的安全 profile 归档辅助函数，同时保留官方 profile 实现；未覆盖 HK worker、三端部署、WebSocket worker 通道或调度员/worker 角色边界。
- 合并后重新执行协作、云文件、受管资源、云部署资产和安装拓扑回归：`346 passed, 7 skipped, 44 subtests passed`。
- 合并后的 Bot Mode 官方接口回归仍通过：`tests/hermes_cli/test_web_server.py -k "bot_"` 为 `5 passed`；部署资产回归为 `58 passed, 1 skipped`。
- 上游同步采用直接验证后的 fast-forward/merge 推送到本项目 `main`，不创建审阅者或 Codex 审查回合；推送完成后应再次确认 `HEAD...origin/main` 为 `0 0`。

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

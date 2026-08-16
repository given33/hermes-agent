# 云托管审计修复记录

本文记录针对服务器托管服务审计表（P0~P3）的修复与落地方式。代码修改集中在：

- `plugins/collaboration/dashboard/plugin_api.py` — 托管 API、连接器端点、SSE、审计、指标
- `deploy/dbb3/dbb3_cloud_connector.py` — DBB3/PC 连接器
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
| 5 | 上行轮询延迟 | 连接器默认轮询间隔降到 `0.5s`，并继续使用 SSE `run.created/run.terminal/run.steer` 即时唤醒；任务多时延迟从 2s 降到亚秒级。 |
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

## 验证

已运行并通过：

```bash
.venv/Scripts/python.exe -m pytest tests/deploy/test_dbb3_connector_session_cache.py -q
.venv/Scripts/python.exe -m pytest tests/plugins/test_collaboration_cloud_files.py -q
.venv/Scripts/python.exe -m pytest tests/plugins/test_collaboration_dashboard.py -q
.venv/Scripts/python.exe -m pytest tests/plugins/test_collaboration_hosted_event_stream.py -q
.venv/Scripts/python.exe -m pytest tests/hermes_cli/dashboard_auth/test_mobile_api_auth.py -q
```

部署后建议：

1. 更新连接器 token 文件支持双 secret 轮换。
2. 设置 `HERMES_CONNECTOR_HEARTBEAT_SECONDS=30`（默认已为 30）。
3. 配置 cron 定期执行 `scripts/backup_hosted_state.py --output-dir ...`。
4. 在升级节点前写入 `.drain_request.json`，待连接器排空后再替换版本。

# 托管节点管理组件评估与接入

本文评估并说明如何把官方 Hermes 的四个“托管运行时”组件接入我们的云托管链路：

1. `managed_scope` — 云端配置覆盖 overlay
2. `gateway/drain_control.py` — NAS/运维驱动的优雅排空
3. `gateway/scale_to_zero.py` — Labs 开关的空闲下电
4. `plugins/cron_providers/chronos/` — NAS 托管定时任务（JWKS JWT 触发）

以及 P3 #15 的 relay/消息平台接入。

## 1. managed_scope

**位置**：`hermes_runtime/managed_scope.py`、`hermes_cli/managed_scope.py`

**作用**：云端/管理端可以把受管配置写入 `$HERMES_HOME/managed/`，在 CLI、gateway、cron、TUI 等所有配置加载入口统一覆盖用户 `config.yaml` 和受管 env。这样节点升级、远程改配置时不需要登录设备手改文件。

**接入现状**：Hermes 核心配置加载路径已经调用 `apply_managed_overlay()`，云托管链路不需要新增代码；只要在节点上放置 managed 配置即可。

**推荐用法**：

```bash
# 在服务器 HERMES_HOME 下
mkdir -p /var/lib/hermes-agent/managed
cat > /var/lib/hermes-agent/managed/config.yaml <<'EOF'
agent:
  max_iterations: 200
cron:
  provider: chronos
EOF

# 受管 env 示例（可选）
cat > /var/lib/hermes-agent/managed/env <<'EOF'
HERMES_REMOTE_SERVER_CAP_SECONDS=3600
EOF
```

配置会被核心自动 overlay，无需重启即可在下次配置加载时生效（部分进程内缓存项仍需重启）。

## 2. drain_control

**位置**：`gateway/drain_control.py`

**作用**：通过 `.drain_request.json` 标记让运行中的服务进入“排空”状态：停止接受新任务，等已有任务收尾后再重启/升级。

**接入现状**：DBB3/PC 连接器已读取该 marker：

- 检测到 `$HERMES_HOME/.drain_request.json`（或 `HERMES_CONNECTOR_DRAIN_FILE` 指定文件）后，`sync_once()` 不再 `pull_runs()` 领取新远程任务；
- 仍会继续处理本地已有 run、取消请求和状态上报；
- 排空完成后可安全替换版本/重启。

**运维命令**：

```bash
# 开始排空
python - <<'PY'
from gateway.drain_control import write_drain_request
write_drain_request(principal="ops", suppress_notification=True)
PY

# 取消排空
rm -f /var/lib/hermes-agent/.drain_request.json
```

> 如果使用 `drain_requested()`，它还会校验 marker 的 instantiation epoch，避免重启后误判为仍在排空。

## 3. scale_to_zero

**位置**：`gateway/scale_to_zero.py`

**作用**：当托管节点长时间空闲时自动挂起自身（Labs 开关，默认关闭），用于节省 NAS/云资源。

**接入评估**：当前云托管链路主要依赖常驻连接器与 SSE，若直接开启 scale_to_zero 会导致连接器/SSE 断开，需配合 Chronos 或外部唤醒机制使用。因此建议仅在“按需唤醒”场景开启。

**配置示例**（`config.yaml`）：

```yaml
gateway:
  scale_to_zero:
    enabled: true
    idle_timeout_minutes: 30
```

开启前必须确认：
- 有外部唤醒通道（如 NAS 调用 `hermes gateway` 启动）；
- 连接器有重连退避，不会因服务短暂下线而误报故障；
- 移动端有“节点离线”提示而不是静默失败。

## 4. Chronos（NAS 托管 cron）

**位置**：`plugins/cron_providers/chronos/`、`hermes_services/cron_fire.py`

**作用**：把定时任务注册到 NAS，由 NAS 在到期时通过 `POST /api/cron/fire` 携带短期 JWT 触发 agent，替代节点本地 60s ticker。适合常驻节点与按需唤醒节点。

**接入评估**：托管节点若需要定时任务（例如每日巡检、定时生成报告），建议直接使用 Chronos，而不是连接器轮询 cron。现有 `/api/cron/fire` 已实现 JWT 校验并加入公开路径白名单，不依赖 dashboard cookie。

**配置示例**（`config.yaml`）：

```yaml
cron:
  provider: chronos
  chronos:
    portal_url: https://your-nas.example
    callback_url: https://agent.example/api/cron/fire
    expected_audience: hermes-agent
    nas_jwks_url: https://your-nas.example/.well-known/jwks.json
```

## 5. relay / 消息平台接入（P3 #15）

**位置**：`docs/relay-connector-contract.md`、`gateway/relay/`

**评估结论**：官方 relay 契约与 gateway 侧实现已开源，我们的托管链路不需要重复造消息桥。

- 若要让托管 agent 接入 Telegram/Discord/Slack 等，优先把 `GATEWAY_RELAY_URL` 指向按契约实现的 connector 服务端；
- 使用官方 `hermes gateway enroll` + 自有 IdP 完成设备注册；
- 连接器侧保持现有 pull/SSE 契约，消息平台事件由 gateway relay 翻译为 run/steer 事件。

**建议落地顺序**：
1. 先完成本仓库的 P0/P1/P2 修复（已提交）；
2. 在测试环境配置 `managed_scope` 与 drain marker；
3. 按需启用 Chronos 或 scale_to_zero；
4. 消息平台接入作为独立项目，基于 `docs/relay-connector-contract.md` 实现/对接。

# Hermes Agent 运维手册(OPERATIONS)

**状态**:基于 2026-07-26 源码实况撰写(版本 0.19.0)。行号为源文件锚点,与代码漂移时以代码为准;无法确证之处标注 `待确认`。
**配套文档**:[`ARCHITECTURE.md`](ARCHITECTURE.md)(进程拓扑/认证机制原理)、[`API-HTTP.md`](API-HTTP.md)(全部 HTTP/WS 面与 21 个监听器)、[`SRS-features.md`](SRS-features.md)(功能清单)、[`CHANGES-2026-07.md`](CHANGES-2026-07.md)(本月变更)。
**读者定位**:没读过源码的运维者能据此部署/备份/排障;要加功能的开发者能在 §3(配置)与 §4(环境变量)一次查到任何键的含义与读取位置。

---

## 1. 部署形态

### 1.1 裸机安装(pip + `hermes` CLI)

- **入口点**(来源:SRS-features §B / `pyproject.toml`):`hermes`(`hermes_cli.main:main`)、`hermes-agent`(`run_agent:main`,直达 agent 循环)、`hermes-acp`(`acp_adapter.entry:main`)。
- **常驻进程只有两个可选项**:`hermes gateway start`(消息平台网关,PID 文件 `~/.hermes/gateway.pid`)与 `hermes dashboard` / `hermes serve`(FastAPI 后端,serve = 无 SPA 的 headless 形态,`HERMES_SERVE_HEADLESS=1`)。`hermes chat` 是单次进程。
- 状态与配置全部在 **HERMES_HOME**(默认 `~/.hermes`;profile 模式为 `~/.hermes/profiles/<name>/`)。`HERMES_HOME` 必须在任何业务 import 之前设定(AGENTS.md 纪律,ARCHITECTURE §5.6)。
- 安装脚本:`setup-hermes.sh`(POSIX)、`scripts/install.ps1`(Windows,安装 PortableGit/ripgrep 并写 User PATH;`hermes_cli/stdio.py:218` 的 `_augment_path_with_known_tools` 会在首启动时把 `%LOCALAPPDATA%\hermes\git\{cmd,bin,usr\bin}`、venv `Scripts`、WinGet Links 预挂到进程 PATH,弥合"刚装完的 shell 看不到新 PATH"间隙)。
- 升级:`hermes update`(git pull + 重装;预更新备份策略见 §6.3 `updates.pre_update_backup`)。非交互更新对本地改动的策略 `updates.non_interactive_local_changes: stash|discard`(来源:`hermes_cli/config.py:3253-3267`)。

### 1.2 docker-compose.yml(Linux/macOS,来源:`docker-compose.yml`)

```
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d
```

- 两个服务:`gateway`(`command: ["gateway","run"]`)与 `dashboard`(`command: ["dashboard","--host","127.0.0.1","--no-open"]`),均 `network_mode: host`、卷 `~/.hermes:/opt/data`。
- **HERMES_UID/HERMES_GID**(默认 10000):s6-overlay stage2 钩子把容器内 `hermes` 用户 usermod/groupmod 成宿主属主,每个受监督服务经 `s6-setuidgid` 降权——宿主 `~/.hermes` 的文件属主保持可读写。Python 侧读取点:`hermes_cli/config.py:780-781`(非 Windows 才生效)。
- **ENTRYPOINT 契约**:镜像默认 `["/init", "/opt/hermes/docker/main-wrapper.sh"]`;覆盖 entrypoint 时必须保留 `/init`(s6-overlay PID 1,跑 cont-init.d 的 chown/profile 调和/dashboard 开关并建立监督树),绕过它 gateway 无法正常工作(来源:docker-compose.yml 头注释)。
- 安全注释(原文要点):dashboard 默认只绑 127.0.0.1,存有 API key,**不要** `--insecure --host 0.0.0.0` 暴露;远程访问用 SSH 隧道或带认证的反代。API server 平台默认关闭,需同时设 `API_SERVER_HOST` + `API_SERVER_KEY` 才启用。
- root 逃生阀:官方镜像的降权被绕过(euid=0)时 gateway 拒绝启动,`HERMES_ALLOW_ROOT_GATEWAY=1` 显式放行(来源:`hermes_cli/gateway.py:4760`)。容器内禁用自监督:`HERMES_GATEWAY_NO_SUPERVISE=1`(`hermes_cli/container_boot.py:225`)。

### 1.3 docker-compose.windows.yml(Docker Desktop,来源:`docker-compose.windows.yml`)

与主 compose 的差异:无 host 网络(Docker Desktop 不支持)→ 显式端口映射 `127.0.0.1:9119:9119`(**宿主侧仍是回环独占**,注释明令"do not widen this mapping");卷用 `${USERPROFILE}/.hermes:/opt/data`;镜像用发行版 `nousresearch/hermes-agent:latest`。

**关键运维事实**:容器内 dashboard 必须绑 `0.0.0.0`(端口映射的流量到达容器桥接口而非容器回环)。对服务器而言这是**非回环绑定 ⇒ 认证门强制生效**(2026-06 加固后 `--insecure` 无效、零 auth provider 时 fail-closed 直接退出),配合 `restart: unless-stopped` 会表现为**崩溃循环**。首启动前必须二选一:在 `%USERPROFILE%\.hermes\config.yaml` 配 `dashboard.basic_auth.username + password_hash`(哈希命令见该文件注释),或 `hermes dashboard register` 注册 OAuth。(此文件旧版曾用主 compose 明令禁止的 `--insecure --host 0.0.0.0`,本月已修,见 CHANGES-2026-07。)

### 1.4 deploy/ 目录(协作后端 + 云连接器 + 托管恢复链路)

这些脚本服务于"手机端托管协作"部署(dashboard collaboration 插件的远端 worker 拓扑,见 `docs/architecture/mobile-hosted-collaboration.md`)。全部 `set -Eeuo pipefail` + `umask 077`。

| 路径 | 用途(取自各文件头注释) |
|---|---|
| `deploy/dbb3/dbb3_cloud_connector.py` | DBB3/PC/HK → Hermes 协作连接器:小型轮询桥,为每个租约的云 run 建幂等 Kanban 根、上报紧凑 checkpoint、上传产物;checkpoint 文件即恢复边界(重启复用同一 idempotency key/游标/产物键,不产生重复工作) |
| `deploy/dbb3/dbb3-cloud-connector.service` | 上者的 systemd unit 模板 |
| `deploy/dbb3/install-dbb3-cloud-connector-user.sh` | 把连接器装成 **user service**(root 只用于 root 属主源路径与既有 root:hermes token;长驻进程归 hermes 用户);带安装锁 `/run/lock/hermes-agent/cloud-connector-install.lock` |
| `deploy/dbb3/test-install-dbb3-cloud-connector-user-rollback.sh` | 上述安装器的回滚测试 harness(需 root) |
| `deploy/dbb3/deploy-collaboration-dashboard.sh` | 从本仓向 `HERMES_DBB3_REMOTE`(默认 ssh 别名 dbb3-hermes)分发 collaboration 面板版本(scp 到 `~/.hermes/deploy/collaboration-<ver>`) |
| `deploy/dbb3/install-collaboration-release.sh` | 远端侧安装 collaboration 发布件(带 health 文件清理 trap) |
| `deploy/dbb3/hermes-services.sudoers`、`hermes-collaboration-deploy.sudoers` | 限定 sudo 白名单 |
| `deploy/pc/install-pc-cloud-connector-user.sh` | 复用 dbb3 安装器,把共享连接器装进 WSL 的隔离 `pc-primary` user service |
| `deploy/pc/run-pc-cloud-connector.sh` | WSL 侧运行包装:state 根、源路径、token 文件(默认 `/etc/pc-team/cloud_connector_token`)、云端 URL(默认 `https://…/api/plugins/collaboration`)、`HERMES_HOME` 均可 env 覆盖 |
| `deploy/hk/install-hk-worker.sh`、`deploy/hk/install-hk-cloud-connector-user.sh` | 香港 worker 首次引导与隔离 `hk-primary` user service:独立源码 checkout、`hk-worker` profile/skills、token、state 和 fabric 自动更新 timer |
| `deploy/public/install-collaboration-backend.sh` | 公网主机 root 侧**事务化安装器**:调用方以非特权账号上传 stage,经 sudo 调本脚本;staged Python/manifest 校验 + 带认证的 connector-health 预检通过前**不替换任何文件**;`mutated` 标志决定回滚是否执行 restore(修复"未变更也误回滚"误报,见 CHANGES) |
| `deploy/public/deploy-collaboration-backend.sh` | 从本仓推送 stage 到公网主机(默认走 WireGuard 地址,`HERMES_PUBLIC_REMOTE` 覆盖) |
| `deploy/public/configure-connector-credential.sh` | 写 connector 凭据:`/etc/hermes-agent/hermes-agent.env` + connector-token map `/etc/hermes-agent/collaboration-connector-tokens.json` |
| `deploy/public/nginx-00-hermes-security.conf`、`nginx-daxueshenmai.top.conf` | 反代与安全头配置样例 |
| `deploy/public/managed-nodes.server.json` | 受管节点清单(服务器侧) |
| `deploy/recovery/configure-main-managed-installation-ssh.sh` | 恢复链路的 sshd 配置事务化写入(mktemp 候选 + 原子 rename + `changed` 标志;回滚区分"恢复失败(1)"与"已恢复但 reload 失败(2)") |
| `deploy/recovery/install-wsl-managed-installation.sh` / `install-dbb3-managed-installation-receiver.sh` | 安装受管安装接收器(WSL / DBB3;receiver 跑在 `hermes_cli/managed_node_recovery_service.py` 独立 token 控制面,见 API-HTTP §7 #4) |
| `deploy/recovery/hermes-managed-*.service`、`hermes-wsl-*.service` | receiver/watchdog/tunnel 的 systemd unit |
| `deploy/recovery/recover-dbb3.sh` | 恢复接收器的**固定 argv 目标**(reset-failed + restart mihomo/gateway 等;故意不可参数化) |
| `deploy/recovery/recover-wsl.ps1`、`install-windows-managed-recovery.ps1`、`run-windows-recovery-*.ps1`、`run-pc-cloud-connector-hidden.vbs` | Windows 侧恢复 receiver/tunnel/watchdog 与隐藏启动 |
| `deploy/recovery/managed-installations.*.json`、`managed-nodes.*.json`、`sshd-hermes-recovery.conf` | 节点/安装清单与 sshd 片段 |

---

### 1.5 GitHub Actions production fan-out

The `deploy-three-endpoints.yml` workflow is the release gate for the public
backend and its fabric nodes. A successful push to `main`, published release,
or explicit manual run first waits for the same commit's complete `CI`
workflow, then dispatches the unsigned release event to the iOS repository.
The hosted workflow is dispatcher-plus-workers only: DBB3, PC/WSL, and HK use
independent profiles, skills, tokens, and state while sharing the same source
commit. Worker updates use the authenticated WebSocket channel with the
durable queue as replay fallback. By default the fan-out dispatches
`hermes-backend-release-unsigned`; signed opt-in uses
`hermes-backend-release` so both child workflows run. The unsigned artifact is
the normal delivery because
the operator signs that artifact with their own certificate tool. Set the
repository variable `HERMES_IOS_SIGNED_BUILD=1` only when the target iOS
repository has valid EAS credentials; that mode starts both the unsigned IPA
verification and the signed production EAS workflow. Scheduled drift-healing
runs intentionally skip the iOS fan-out so they do not enqueue an app build
every two hours.

Configure the following repository settings before enabling this fan-out:

- Secret `HERMES_IOS_WORKFLOW_TOKEN`: a short-lived or narrowly scoped token
  with Contents write and Actions write access to the target iOS repository. It
  must be able to dispatch events, rerun failed child workflows, and read
  workflow runs.
- Repository variable `HERMES_IOS_REPOSITORY`: optional; defaults to
  `given33/hermes-ios` and should be set when the iOS repository moves.
- Repository variable `HERMES_IOS_SIGNED_BUILD`: optional and defaults to
  `0`. Set it to `1` only after configuring `EXPO_TOKEN` and `EXPO_PROJECT_ID`
  in the iOS repository.
- The unsigned target workflow must keep `repository_dispatch` enabled for
  event types `hermes-backend-release-unsigned` and the legacy
  `hermes-backend-release`; the signed workflow listens only to the latter.
  A missing token, an unobservable dispatch, or a failed unsigned iOS run
  fails the fan-out job after the backend deployment has already completed,
  leaving an explicit GitHub check to prevent silent drift. The signed EAS job
  remains an explicit opt-in and fails closed when its credentials are absent.

The `deploy-site.yml` workflow deploys GitHub Pages and triggers the Vercel
deploy hook for release events, matching `main` pushes, and manual runs. The
Vercel hook secret is `VERCEL_DEPLOY_HOOK`.

---

## 2. 进程与端口

### 2.1 端口总表(默认值,均可配)

| 进程/平台 | 默认绑定 | 来源 | 何时监听 |
|---|---|---|---|
| Dashboard/serve(FastAPI) | `127.0.0.1:9119` | `hermes_cli/subcommands/dashboard.py:27,30`(`--port 9119`,0=OS 自派;`--host 127.0.0.1`) | `hermes dashboard`/`serve` 运行时 |
| Gateway REST(api_server) | `127.0.0.1:8642` | `gateway/platforms/api_server.py:121` | 配置了 api_server 平台或设 `API_SERVER_KEY`/`API_SERVER_ENABLED` |
| 通用 webhook 平台 | `:8644`(host 未设按非回环 fail-closed) | `gateway/platforms/webhook.py:100` | webhook 平台启用时 |
| BlueBubbles webhook | `127.0.0.1:8645` | `gateway/platforms/bluebubbles.py:49` | BlueBubbles 平台启用时 |
| 凭据代理 `hermes proxy` | `127.0.0.1:8645` | `hermes_cli/proxy/server.py:51` | `hermes proxy` 运行时。**注意与 BlueBubbles 默认同端口**——两者同时启用必须改其一 |
| MS Graph webhook | `0.0.0.0:8646` | `gateway/platforms/msgraph_webhook.py:34` | msgraph 平台启用时 |
| WhatsApp Cloud webhook | `0.0.0.0:8090` | SRS-features §F 表 | whatsapp_cloud 平台启用时 |
| Teams webhook | `:3978`(`TEAMS_PORT`) | `docker-compose.yml` 注释 | Teams 平台启用时 |
| 其余 13 个监听器(插件平台 webhook、Node sidecar、OAuth 回环、iOS MCP) | 见 API-HTTP §7 | — | 各自启用时 |

### 2.2 gateway(`gateway/run.py`,23,097 行)

- 生命周期:`hermes gateway run|start|stop|status`;`run` 为前台长驻(`acp`、`gateway run`、`cron run|tick` 在 CLI 常规分发前被拦截,SRS §B)。存在性:`~/.hermes/gateway.pid` + `gateway_state.json` + `gateway-locks/`(Windows 用 1MB 偏移字节范围锁)。
- 启动即调 `ensure_utf8_stdio()` + `configure_windows_stdio()`(显式入口点修复,不再是 import 副作用;见 §7.2)。
- 内含:平台适配器长连接/监听、api_server、cron ticker 线程(60s tick,`~/.hermes/cron/.tick.lock` 跨进程去重)、kanban 派发器(`kanban.dispatch_in_gateway: True`)。
- 优雅停机:`agent.restart_drain_timeout: 0`(默认立即打断;正值须远小于 systemd `TimeoutStopSec`,否则 SIGKILL 竞态留下陈旧锁,来源:`hermes_cli/config.py:1021-1035` 注释)。适配器断连超时 `HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT`。
- 重启风暴防护(两层,`hermes_cli/config.py:3035-3063`):`gateway.restart_loop_guard`(窗口内 `max_restarts: 3` 次被打断的 boot ⇒ 跳过 auto-resume)与 `gateway.respawn_storm`(`max_starts: 5`/`window_seconds: 120`,超限指数退避再起;env `HERMES_GATEWAY_MAX_STARTS`/`HERMES_GATEWAY_START_WINDOW_S` 覆盖)。退出诊断 `HERMES_GATEWAY_EXIT_DIAG`(默认开,`hermes_cli/gateway.py:4884`)。
- Windows:`HERMES_GATEWAY_DETACHED` 控制脱附式启动(`hermes_cli/gateway.py:1709`);UAC 提权安装经 `HERMES_GATEWAY_ELEVATED_HANDOFF` 等交接变量(`hermes_cli/gateway_windows.py:247-249`)。

### 2.3 dashboard(`hermes_cli/web_server.py`,20,194 行)

- 同一 `start_server()` 服务 `dashboard`(建/服 SPA,开浏览器)与 `serve`(headless,`HERMES_SERVE_HEADLESS=1`,SPA 请求一律 404 JSON;`hermes_cli/web_server.py:18801`)。
- 会话 token:`HERMES_DASHBOARD_SESSION_TOKEN` 可钉死(默认每进程 `secrets.token_urlsafe(32)`,`web_server.py:311`)——多 worker 部署必须钉死否则"注入=校验"前提破产(模块内 `DashboardRuntimeState` 类逐项记载了单进程约束,本月新增,见 CHANGES)。
- 认证细节全在 ARCHITECTURE §4.3/§5.2 与 API-HTTP §2;运维要点见本文件 §5.1。
- 桌面(Electron)以 `hermes serve --host 127.0.0.1 --port 0` 拉起后端,就绪信号写 `HERMES_DESKTOP_READY_FILE` 指定的 JSON(`web_server.py:20001`)。

### 2.4 relay(`gateway/relay/`,EXPERIMENTAL)

- 用途:「Gateway Gateway」中继——gateway **主动外拨** WebSocket 到连接器(无入站监听端口);`GATEWAY_RELAY_URL` env 或 config `gateway.relay_url` 非空即激活 relay 平台,未设完全无影响(来源:`gateway/relay/__init__.py` 模块 docstring)。
- 认证(`gateway/relay/auth.py`):①WS upgrade —— gateway 携 `Authorization: Bearer make_upgrade_token(gateway_id, secret)`(HMAC-SHA256,base64url(payload:exp:sig)),由连接器侧校验;②入站投递签名 `x-relay-timestamp`+`x-relay-signature` —— **UNWIRED**:`verify_delivery_signature` 有实现有测试但**零生产调用点**,因为 Python gateway 目前没有入站 HTTP 投递路由(入站事件走自家外拨的、已认证的 WS)。**谁新增 HTTP 投递端点谁必须接线该校验器**(docstring 明文;`tests/architecture/test_wired_security_controls.py` 已把它登记为待接线控制)。双方案均支持主/次双 secret 轮换窗口。
- 公开 API(模块名/descriptor 字段/传输协议)在 ≥2 个真实 Class-1 平台落地前**可能不打招呼就改**。

---

## 3. 配置总参考(config.yaml)

### 3.1 读写纪律(一句话版;全文见 ARCHITECTURE §5.1)

读:`load_config()` / `load_config_readonly()` / `read_raw_config()`(原始文档;解析失败**降级为 `{}` + 告警**)/ **`read_raw_config_strict()`**(解析失败**抛异常**——任何"读出来再整文档写回"的路径必须用 strict 版,否则一个坏缩进会把整份配置替换成你那一节;来源:`hermes_cli/config.py:7132` 与 `_persist_migration` docstring,config.py:5808)。写:唯一认可模式 `mutate_config(fn)`(锁内重读 → 变更 → 原子写);跨进程锁 `config_write_lock()` 锁 `<config>.lock` 旁文件,10s 超时降级带警告继续;锁不可用的文件系统(NFS/SMB)现被显式识别(`_LOCK_UNSUPPORTED`,按路径记忆 + 一次性进程级警告,不再每次写烧满 10s;config.py:7268-7299)。

### 3.2 顶层键总表

来源:`hermes_cli/config.py` `DEFAULT_CONFIG`(L1000-3490,`_config_version: 33`)。**77 个字面键/76 个唯一键**——`kanban` 在 L1539 与 L2855 出现两次,Python dict 字面量语义**后者生效**;被遮蔽的 `kanban.auto_subscribe_on_create` 因读取方自带 `default=True`(`tools/kanban_tools.py:1224`)而行为不变,但**在 DEFAULT_CONFIG/`/api/config/defaults` 里看不到它**。`mcp_servers:` 是纯用户节(不在 DEFAULT_CONFIG)。

表内"默认"给标量或代表性子键;完整子键与逐行注释直接读 DEFAULT_CONFIG 对应行(锚点已给)。

| 键(行) | 类型/默认 | 含义与关键子键 |
|---|---|---|
| `model`(1001) | str `""`(用户侧常写成映射 `{provider, default, base_url, api_mode}`) | 主模型选择;空=解析链决定(CLI flag → config → env → auth.json active_provider,SRS §A.1) |
| `providers`(1002) | map `{}` | 按 provider 名的用户覆盖(自定义端点等;`hermes model`/dashboard 写入) |
| `fallback_providers`(1003) | list `[]` | 故障转移链(`hermes fallback` 管理;循环内 `_try_activate_fallback` 消费) |
| `credential_pool_strategies`(1004) | map `{}` | 每 provider 凭据池轮换策略(SRS §A.1) |
| `toolsets`(1005) | list `["hermes-cli"]` | 默认工具集预设 |
| `max_concurrent_sessions`(1008) | int/None | 全局活跃聊天会话上限(CLI+TUI+消息平台;None/0 不限) |
| `max_live_sessions`(1013) | int 16 | 内存内 TUI/desktop/dashboard 会话软 LRU 上限,超出驱逐无客户端的 detached 会话 |
| `agent`(1014) | 映射 | 核心循环:`max_turns: 90`、`gateway_timeout: 1800`(纯闲置才触发)+ `gateway_timeout_warning: 900`、`restart_drain_timeout: 0`、`api_max_retries: 3`、`clarify_timeout: 3600`、`gateway_notify_interval: 180`、`gateway_auto_continue_freshness: 3600`、`local_stream_stale_timeout: 900`、`image_input_mode: auto`、`tool_use_enforcement/intent_ack_continuation: auto`、`task_completion_guidance/parallel_tool_call_guidance/environment_probe: true`、`coding_context: auto` + `coding_instructions`、`verify_on_stop: auto` + `max_verify_nudges: 3`、`reasoning_overrides: {}`、`disabled_toolsets: []`、`environment_hint`、`service_tier` |
| `terminal`(1201) | 映射 | 终端后端:`backend: local`(docker/singularity/modal/daytona/ssh)、`cwd: "."`、`timeout: 180`、`home_mode: auto`、`shell_init_files`+`auto_source_bashrc: true`、容器镜像/资源(`docker_image`、`container_cpu: 1`/`memory: 5120`/`disk: 51200`、`container_persistent: true`)、`docker_volumes/docker_env/docker_forward_env/docker_extra_args`、`docker_network: true`(false=--network=none)、`docker_run_as_host_user: false`、`docker_mount_cwd_to_workspace: false`、`persistent_shell: true`、`env_passthrough`、`daemon_term_grace_seconds: 2.0` |
| `web`(1298) | 映射 | web_search/web_extract 后端选择 + `extract_char_limit: 15000` |
| `browser`(1305) | 映射 | 浏览器工具:`inactivity_timeout: 120`、`command_timeout: 30`、`headed: false`、`allow_private_urls: false`、`engine: auto`、`cdp_url`、`restrict_evaluate/allow_unsafe_evaluate: false`、`dialog_policy: must_respond`(+`dialog_timeout_s: 300`)、`camofox.*`(持久身份/回环重写) |
| `checkpoints`(1357) | 映射 | 文件系统检查点(/rollback):`enabled: false`(opt-in)、`max_snapshots: 20`、`max_total_size_mb: 500`、`max_file_size_mb: 10`、`auto_prune: true`、`retention_days: 7` |
| `context_file_max_chars`(1392) | None | SOUL.md/AGENTS.md 等上下文文件截断上限(null=随模型窗口 20K-500K 自适应) |
| `file_read_max_chars`(1397) | int 100_000 | 单次 read_file 上限,超限要求 offset+limit |
| `mcp_discovery_timeout`(1412) | float 1.5 | agent 构建时等待 MCP 后台发现的上限(只影响第 1 轮快照;下一轮自动补上) |
| `mcp`(1416) | 映射 | `auto_reload_on_config_change: true`(自动重载会**作废提示词缓存**,可关掉改用 /reload-mcp) |
| `tool_output`(1442) | 映射 | 工具输出截断:`max_bytes: 50_000`、`max_lines: 2000`、`max_line_length: 2000` |
| `tool_loop_guardrails`(1451) | 映射 | 重复失败工具调用的软警告(默认开)/硬停(默认关)阈值 |
| `compression`(1466) | 映射 | 上下文压缩:`enabled: true`、`threshold: 0.50`(<512K 窗口模型下限 0.75)、`target_ratio: 0.20`、`protect_last_n: 20`/`protect_first_n: 3`、`abort_on_summary_failure: false`、`codex_gpt55_autoraise: true`(Codex OAuth 路线 85%)、`in_place: true`(同 id 原地压缩,#38763)、`hygiene_hard_message_limit: 5000` |
| `kanban`(1539,**被 2855 遮蔽**) | 映射 | 字面量含 `auto_subscribe_on_create: true`(创建任务的会话自动订阅完成/阻塞通知)——语义生效但不出现在合并后的 DEFAULT_CONFIG,见本节开头 |
| `prompt_caching`(1552) | 映射 | Anthropic 缓存 `cache_ttl: "5m"`(仅 5m/1h 合法) |
| `openrouter`(1571) | 映射 | `response_cache: true` + `response_cache_ttl: 300`、`min_coding_score: 0.65`(pareto-code 路由) |
| `bedrock`(1579) | 映射 | AWS 区域、模型发现(`discovery.*`)、Guardrails(`guardrail.*`) |
| `auxiliary`(1623) | 映射 | 17 个辅助任务的 provider/model/base_url/api_key/timeout/extra_body/reasoning_effort:vision(120s)、web_extract(360s)、compression(120s)、skills_hub/approval/mcp/title_generation(30s)、memory_query_rewrite(8s)、tts_audio_tags、triage_specifier(120s)、kanban_decomposer(180s)、profile_describer/goal_judge(60s)、curator(600s)、monitor(60s)、background_review(120s)、moa_reference/moa_aggregator(900s);`transient_retries: 2` |
| `display`(1843) | 映射 | 全部界面显示项:`interface: cli`、`busy_input_mode: interrupt`、`show_reasoning: true`、`streaming: false`(CLI 内)、`memory_notifications: on`、`language: en`(仅静态 UI 文案)、`inline_diffs/file_mutation_verifier/turn_completion_explainer: true`、`interim_assistant_messages: true`、`tool_progress_grouping: accumulate`、`reasoning_style: code`、`runtime_footer.enabled: false`、`ephemeral_system_ttl: 0`、`platforms.{telegram:{streaming:true},discord:{streaming:false}}` 每平台覆盖、`pet.*` 吉祥物 |
| `dashboard`(2055) | 映射 | `theme: default`、`turn_isolation: false`、`show_token_analytics: false`(本地估算与账单差 10-100 倍,注释详述)、`oauth.client_id/portal_url`(env 覆盖见 §4)、`basic_auth.{username,password_hash,password,secret,session_ttl_seconds}`(自托管密码门)、`drain_auth.{scope,min_secret_chars:43}`、`public_url`(反代场景钉死 OAuth redirect 权威)、`trusted_proxies`(见 §5.1) |
| `privacy`(2162) | 映射 | `redact_pii: false`(开启则哈希用户 ID、剥电话号) |
| `tts`(2171) | 映射 | `provider: edge`;每后端子节(edge/elevenlabs/openai/gemini/xai/mistral/minimax/kittentts/neutts/piper/deepinfra)含音色/模型;各支持 `max_text_length` 覆盖 |
| `stt`(2250) | 映射 | `enabled: true`、`echo_transcripts: true`、`provider: local`(faster-whisper;groq/openai/mistral/elevenlabs/deepinfra) |
| `voice`(2279) | 映射 | CLI 语音:`record_key: ctrl+b`、静音阈值/时长、`auto_tts: false` |
| `human_delay`(2288) | 映射 | 出站消息拟人延迟:`mode: off`、`min_ms/max_ms` |
| `context`(2300) | 映射 | `engine: compressor`(或 context_engine 插件名) |
| `memory`(2305) | 映射 | `memory_enabled/user_profile_enabled: true`、`write_approval: false`、`memory_char_limit: 2200`/`user_char_limit: 1375`、`provider: ""`(外部记忆插件名,一次一个)——详见 SRS §K |
| `delegation`(2334) | 映射 | 子 agent:`model/provider/base_url/api_key/api_mode`(空=继承)、`max_iterations: 50`、`max_summary_chars: 24000`、`child_timeout_seconds: 0`、`max_concurrent_children: 3`、`max_spawn_depth: 1` + `orchestrator_enabled: true`、`subagent_auto_approve: false`(子线程审批一律非交互:false=自动拒)、`inherit_mcp_toolsets: true`、`reasoning_effort` |
| `prefill_messages_file`(2398) | str `""` | 每次 API 调用注入的 few-shot 消息文件(不进会话/日志) |
| `goals`(2408) | 映射 | /goal 循环 `max_turns: 20`(judge 失败 fail-open) |
| `moa`(2419) | 映射 | MoA preset:`default_preset/active_preset`、`save_traces: false`(+`trace_dir`,JSONL 全量轨迹)、`presets.default`(references gpt-5.5 + deepseek-v4-pro,aggregator claude-opus-4.8) |
| `skills`(2446) | 映射 | `external_dirs: []`(只读共享技能目录)、`template_vars: true`、`inline_shell: false`(!`cmd` 预执行,信任门)+ `inline_shell_timeout: 10`、`guard_agent_created: false`(hub 安装恒扫描)、`write_approval: false`(SRS §K.4) |
| `curator`(2497) | 映射 | 技能后台维护:`enabled: true`、`interval_hours: 168`、`min_idle_hours: 2`、`stale_after_days: 30`、`archive_after_days: 90`(只归档不删)、`consolidate: false`、`prune_builtins: true`、`backup.{enabled:true,keep:5}` |
| `honcho`(2540) | map `{}` | honcho 的 hermes 侧覆盖(真源在 `~/.honcho/config.json`) |
| `timezone`(2544) | str `""` | IANA 时区(空=服务器本地) |
| `slack`(2547)/`discord`(2555)/`whatsapp`(2636)/`telegram`(2644)/`mattermost`(2655)/`matrix`(2663) | 映射 | 各平台行为:@mention 门、白名单频道、`channel_prompts`、Discord 另有 auto_thread/多 bot 互吹抑制/`missed_message_backfill`/WS 存活探针 4 键/`max_attachment_bytes: 32MiB`/`voice_fx.*` 语音混音器、Telegram `extra.rich_messages/rich_drafts` |
| `approvals`(2677) | 映射 | 危险命令审批:`mode: smart`(manual/off)、`timeout: 60`、`cron_mode: deny`、`deny: []`(用户 fnmatch 硬拒,先于 yolo)、`mcp_reload_confirm: true`、`destructive_slash_confirm: true` |
| `command_allowlist`(2711) | list `[]` | "always" 批准积累的永久放行模式 |
| `quick_commands`(2713) | map `{}` | 绕过 agent 的用户快捷命令 |
| `platform_hints`(2729) | map `{}` | 每平台 system prompt 提示 append/replace |
| `hooks`(2738)+`hooks_auto_accept`(2744) | map/bool | shell 脚本钩子(事件→{matcher,command,timeout});首次注册需同意,存 `~/.hermes/shell-hooks-allowlist.json`;auto_accept 供无 TTY 场景 |
| `personalities`(2748) | map `{}` | 自定义人格 |
| `security`(2751) | 映射 | `allow_private_urls: false`(SSRF 总开关,见 §5.4)、`redact_secrets: true`、tirith 预执行扫描 4 键、`website_blocklist.*`、`acked_advisories: []`(`hermes doctor --ack`)、`allow_lazy_installs: true` |
| `cron`(2780) | 映射 | `provider: ""`(内置 60s ticker;"chronos"=NAS 托管,子节 `chronos.{portal_url,callback_url,expected_audience,nas_jwks_url}`——jwks 空=拒绝一切 fire token)、`wrap_response: true`、`mirror_delivery: false`、`max_parallel_jobs: None`、`output_retention: 50`、`session_db_timeout_seconds: 10` |
| `kanban`(2855,**生效版**) | 映射 | 派发器:`dispatch_in_gateway: true`、`dispatch_interval_seconds: 60`、`failure_limit: 2`、`worker_log_rotate_bytes: 2MiB`/`backup_count: 1`、`orchestrator_profile`/`default_assignee`、`max_in_progress_per_profile: None`、`auto_decompose: true`(+`per_tick: 3`)、`dispatch_stale_timeout_seconds: 14400` |
| `code_execution`(2911) | 映射 | `mode: project`(strict=隔离临时目录+自带 python);两模式同样做 env 秘密剥离 |
| `tools`(2935) | 映射 | `tool_search.{enabled:auto, threshold_pct:10, search_default_limit:5, max_search_limit:20}`(大工具面渐进披露;核心工具永不延迟) |
| `logging`(2958) | 映射 | `level: INFO`、`max_size_mb: 5`、`backup_count: 3` → `~/.hermes/logs/agent.log`(INFO+)与 `errors.log`(WARNING+) |
| `model_catalog`(2969) | 映射 | 远端模型清单 `enabled: true`、`url`(docs 站)、`ttl_hours: 1`、`providers` 覆盖 |
| `network`(2986) | 映射 | `force_ipv4: false`(IPv6 坏链路时跳过 AAAA) |
| `gateway`(2995) | 映射 | `delivery_ledger: true`(至少一次投递,崩溃后重投带"recovered reply"标记)、`platform_connect_timeout: 30`、`write_sessions_json: true`(legacy 镜像)、`scale_to_zero.idle_timeout_minutes: 5`、`restart_loop_guard`/`respawn_storm`(§2.2)、`message_timestamps.enabled: false`、`max_inbound_media_bytes: 128MiB`、媒体投递门(`strict: false`、`media_delivery_allow_dirs`、`trust_recent_files(+_seconds: 600)`,§5.4)、`api_server.max_concurrent_runs: 10` |
| `streaming`(3144) | 映射 | 平台流式:`enabled: false`、`transport: auto`(draft/edit)、`edit_interval: 0.8`、`buffer_threshold: 24`、`cursor`、`fresh_final_after_seconds: 0` |
| `sessions`(3181) | 映射 | state.db 自动维护:`auto_prune: false`(opt-in)、`retention_days: 90`、`vacuum_after_prune: true`、`min_interval_hours: 24`、`write_json_snapshots: false` |
| `onboarding`(3216) | 映射 | 一次性提示闩 `seen.*`、`profile_build: ask` |
| `updates`(3227) | 映射 | `pre_update_backup: quick`(quick/full/off,§6.3)、`backup_keep: 5`、`non_interactive_local_changes: stash`、`refresh_cua_driver: true` |
| `lsp`(3286) | 映射 | `enabled: true`、`wait_mode: document` + `wait_timeout: 5.0`、`install_strategy: auto`(装进 `<HERMES_HOME>/lsp/bin/`)、`servers.*` 每服务器覆盖;仅 git 工作区激活 |
| `x_search`(3327) | 映射 | xAI x_search:`model: grok-4.5`、`timeout_seconds: 180`、`retries: 2` |
| `secrets`(3348) | 映射 | 外部密钥源:`bitwarden.{enabled:false, access_token_env:BWS_ACCESS_TOKEN, project_id, cache_ttl_seconds:300, override_existing:true, auto_install:true, server_url}`、`onepassword.{enabled:false, env:{VAR:op://…}, account, service_account_token_env, binary_path, cache_ttl_seconds:300, override_existing:true}`;mapped 源恒先于 bulk 源,先到先得 |
| `paste_collapse_threshold`(3436)/`_fallback`(3437)/`_char_threshold`(3438) | int 5/5/2000 | 粘贴坍缩成文件引用的行数/字符阈值(0=禁用) |
| `computer_use`(3441) | 映射 | `cua_telemetry: false`(默认替用户关掉 cua-driver 的 PostHog 遥测) |
| `desktop`(3453) | 映射 | Electron 启动:`electron_flags: []`、`disable_gpu: auto` |
| `vertex`(3478) | 映射 | `project_id: ""`、`region: global`(Gemini 3.x 预览必须 global);桥接到 `VERTEX_PROJECT_ID/REGION` env,显式 env 胜 |
| `_config_version`(3489) | int 33 | 迁移系统版本号;所有迁移写盘必须走 `_persist_migration`(config.py:5808,fail-closed,§3.1) |

---

## 4. 环境变量总参考(HERMES_*)

**提取口径**:对 `agent/ tools/ gateway/ hermes_cli/ plugins/ cron/ providers/ tui_gateway/ acp_adapter/` 与根模块的生产代码(排除 tests)枚举 `os.environ.get`/`os.getenv` 直读(232 个)、`os.environ[...]`/`env_var_enabled()`/`setdefault` 读(20 个)与经命名常量间接读(6 个)。`HERMES_SESSION_*` 家族由 `gateway/session_context.py` 的 `get_session_env()` 统一供给(gateway 为子 agent/工具进程**写入**的会话上下文,同名 env 直读仅作回退)。非 `HERMES_` 前缀的平台凭据(`TELEGRAM_BOT_TOKEN`、`API_SERVER_KEY`、`GATEWAY_RELAY_URL`、`TEAMS_*`…)不在本表,见各平台文档与 `docker-compose.yml`。下表"读取位置"为首个生产读点。

### 4.1 核心路径 / 身份 / 配置

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_HOME | 活动 home(profile 目录);一切状态的根,须在业务 import 前设定 | `agent/lsp/install.py:125` 等(全仓 713 处引用;解析权威 `hermes_constants.get_hermes_home`) |
| HERMES_REAL_HOME | 显式声明真实 OS 用户 home(容器/HOME 重写场景的信任候选) | `hermes_constants.py:742` |
| HERMES_PROFILE / HERMES_PROFILE_NAME | 活动 profile 名(`-p` 派生进程携带);写审批 scope 回退 | `tools/kanban_tools.py:830` / `tools/write_approval.py:104` |
| HERMES_OWNER_ID / HERMES_IOS_OWNER_ID / HERMES_OWNER_EMAIL | 写审批 owner scope 回退链;OWNER_EMAIL 同时是移动端注册门(QQ 邮箱正则) | `tools/write_approval.py:96-98`、`hermes_cli/dashboard_auth/owner_mobile.py` |
| HERMES_LEGACY_OWNER_ID | 显式迁移旧 owner 记录(不自动继承,注释:认证设置不得使普通账号继承本地/E2E 记录) | `plugins/collaboration/dashboard/plugin_api.py:9736` |
| HERMES_MANAGED | 声明本安装由哪个包管理器拥有(粗粒度写锁) | `hermes_cli/config.py:340` |
| HERMES_MANAGED_DIR | managed scope 目录(IT 推送、用户不可变配置层;默认 /etc/hermes) | `hermes_cli/doctor.py:647`(消费在 `hermes_cli/managed_scope.py`) |
| HERMES_IGNORE_USER_CONFIG | =1 时共享加载器视用户 config.yaml 为不存在(仅 load 路径;raw 读/写不受影响,防止覆盖保存) | `hermes_cli/config.py:7722` |
| HERMES_UID / HERMES_GID | 容器场景宿主属主重映射(Windows 上忽略) | `hermes_cli/config.py:780-781` |
| HERMES_HOME_MODE | HERMES_HOME 目录权限(八进制;默认 0o700) | `hermes_cli/config.py:843` |
| HERMES_CONTAINER / HERMES_SKIP_CHMOD | 任一存在即跳过 home 权限收紧(容器/特殊文件系统) | `hermes_cli/config.py:863` |
| HERMES_TIMEZONE | IANA 时区透传(code_execution 子进程等) | `tools/code_execution_tool.py:1064` |
| HERMES_LANGUAGE | 静态 UI 文案语言(env > config display.language) | `agent/i18n.py:243` |
| HERMES_BUNDLED_LOCALES / HERMES_BUNDLED_PLUGINS / HERMES_BUNDLED_SKILLS / HERMES_OPTIONAL_SKILLS / HERMES_OPTIONAL_MCPS / HERMES_BUNDLES_DIR | 打包安装重定位各内置资源目录 | `agent/i18n.py:106`、`hermes_cli/plugins.py:62`、`hermes_constants.py:256/217/236`、`agent/skill_bundles.py:72` |
| HERMES_BIN_DIR / HERMES_BIN | Hermes 自管 bin 目录 / hermes 可执行路径覆盖(kanban worker 派生用) | (bin_dir 5 处)/`hermes_cli/kanban_db.py:8140` |
| HERMES_NODE / HERMES_NODE_TARGET_MAJOR | node 可执行覆盖 / 目标 Node 主版本(默认 22) | `hermes_cli/main.py:1861`、`hermes_constants.py:331` |
| HERMES_SKIP_NODE_BOOTSTRAP | 跳过 Node 自动引导 | `hermes_cli/main.py:1745` |
| HERMES_PYTHON_SRC_ROOT | 源码根覆盖(引导守卫) | `hermes_bootstrap.py:150` |
| HERMES_LAZY_INSTALL_TARGET / HERMES_DISABLE_LAZY_INSTALLS | 运行期 pip 懒安装重定向目标 / =1 禁用(密封 venv;有 target 时仍可装进数据卷) | `hermes_bootstrap.py:175`、`tools/lazy_deps.py:452` |
| HERMES_NONINTERACTIVE | 显式声明无人值守(安装向导等不发问) | `hermes_cli/setup.py:292` |
| HERMES_QUIET | 压制杂项启动输出 | `hermes_cli/main.py:1837` |
| HERMES_INTERACTIVE | 标记交互式 CLI 语境(工具可用性门;经 contextvars 修复 ACP 线程池竞态 GHSA-96vc-wcxf-jjff) | `hermes_cli/doctor.py`(setdefault)等 |
| HERMES_SAFE_MODE | --safe-mode 标记(禁用户配置/规则/plugins/MCP) | (subscript 读,`hermes_cli` 启动链) |
| HERMES_REVISION | 更新检查横幅用的嵌入版本号(打包安装注入) | `hermes_cli/banner.py:309` |

### 4.2 Agent 循环 / API 调用

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_MAX_ITERATIONS | 每轮迭代预算(默认 90;gateway 把 config agent.max_turns 桥接到此) | `gateway/run.py:1489` |
| HERMES_MODEL / HERMES_MAX_TOKENS | 模型与输出上限覆盖(cron/gateway 路径) | `cron/scheduler.py:3056`、`gateway/run.py:2088` |
| HERMES_INFERENCE_PROVIDER / HERMES_INFERENCE_MODEL | oneshot/嵌入调用的 provider/model(设 provider 必须同时给 model) | `hermes_cli/main.py:2987`、`hermes_cli/oneshot.py:205` |
| HERMES_API_TIMEOUT | 主 API 调用超时(7 处消费,`hermes_cli/timeouts.py` 族) | (get_provider_request_timeout) |
| HERMES_API_CALL_STALE_TIMEOUT / HERMES_STREAM_STALE_TIMEOUT / HERMES_LOCAL_STREAM_STALE_TIMEOUT | 无进展 API 调用/流的僵死检测;本地端点专用顶棚(默认 900) | `run_agent.py:1300`、(stream 4 处)、`agent/chat_completion_helpers.py:3585` |
| HERMES_API_RETRY_DELAY_SECONDS / HERMES_API_RETRY_STATUS_LIVE / HERMES_API_RETRY_CLIENT_ERRORS | 重试附加延迟(0-600s)/ 重试时发状态 / 4xx 也重试 | `agent/agent_init.py:1600/1606/1609` |
| HERMES_EPHEMERAL_SYSTEM_PROMPT | 覆盖 system prompt(gateway;先于 config agent.system_prompt) | `gateway/run.py:5103` |
| HERMES_PREFILL_MESSAGES_FILE | few-shot 预填消息文件(相对路径基于 ~/.hermes) | `gateway/run.py:5071` |
| HERMES_ENVIRONMENT_HINT | 嵌入方环境说明注入 system prompt(胜过 config agent.environment_hint) | `agent/prompt_builder.py:1245` |
| HERMES_PLATFORM / HERMES_SESSION_PLATFORM | 当前平台提示(system prompt 平台 hint,不 import gateway) | `agent/prompt_builder.py:1521` |
| HERMES_VERIFY_ON_STOP / HERMES_FILE_MUTATION_VERIFIER / HERMES_TURN_COMPLETION_EXPLAINER | 三个收尾行为开关的 env 覆盖(对应 config 同名键) | `agent/verification_stop.py:147`、`run_agent.py:3004/3101` |
| HERMES_AGENT_TIMEOUT / HERMES_AGENT_TIMEOUT_WARNING / HERMES_AGENT_NOTIFY_INTERVAL | gateway 桥接的闲置超时/预警/进度通知间隔(config agent.gateway_* 的 env 面) | (subscript 写读,gateway/run.py) |
| HERMES_AUTO_CONTINUE_FRESHNESS | 崩溃续跑注记的新鲜度窗(默认 3600;0=总注入) | `gateway/session.py:51` |
| HERMES_YOLO_MODE | 跳过全部危险命令审批;**import 时冻结**(运行中改环境变量无效——防 skill 提权) | `tools/approval.py:35` |
| HERMES_EXEC_ASK | 终端命令逐条确认模式(subscript 读) | (tools/terminal_tool 链) |
| HERMES_CONCURRENT_TOOL_TIMEOUT_S | 并行工具批的单调用超时 | `agent/tool_executor.py:99` |
| HERMES_PLUGIN_PAYLOAD_MAX_CHARS | 插件 Hook 载荷截断(默认 50000) | `run_agent.py:2397` |
| HERMES_ACCEPT_HOOKS | 非交互接受 shell hook 注册(=--accept-hooks) | `agent/shell_hooks.py:846` |
| HERMES_IGNORE_RULES | 跳过上下文文件与记忆(--ignore-rules) | `tui_gateway/server.py:5155` |
| HERMES_WRITE_SAFE_ROOT | 写安全根(os.pathsep 分隔;根外写一律拒) | `agent/file_safety.py:84` |
| HERMES_DISABLE_FILE_STATE_GUARD | =1 关闭文件新鲜度守卫(read-before-write) | `tools/file_state.py:271` |
| HERMES_TERMINAL_SECURITY_MODE | codex app-server 权限档位映射(auto→workspace-write) | `agent/transports/codex_app_server_session.py:218` |
| HERMES_SIGTERM_GRACE | CLI 收到信号后打断 agent 的宽限秒数(默认 1.5) | `cli.py:15222` |
| HERMES_EXIT_WATCHDOG_S | 退出看门狗(默认 30s 强杀滞留线程) | `cli.py:1087` |
| HERMES_DEFER_AGENT_STARTUP | =1 延迟 agent 重依赖加载 | `cli.py:1022` |
| HERMES_SESSION_ID / HERMES_SESSION_KEY / HERMES_SESSION_SOURCE / HERMES_SESSION_CHAT_ID / HERMES_SESSION_THREAD_ID / HERMES_SESSION_USER_ID / HERMES_SESSION_CHAT_NAME | 会话上下文家族:gateway 为工具/子进程注入;读经 `gateway/session_context.get_session_env`(直读为回退) | `tools/kanban_tools.py:127/1253`、`agent/background_review.py:607` 等 |
| HERMES_GATEWAY_SESSION / HERMES_CRON_SESSION | 标记"运行于 gateway 会话/cron 会话"语境 | (subscript 读;gateway/run.py、cron/scheduler.py) |

### 4.3 Gateway / 平台

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_ALLOW_ROOT_GATEWAY | 放行 euid=0 的 gateway(默认拒启) | `hermes_cli/gateway.py:4760` |
| HERMES_GATEWAY_DETACHED | Windows 脱附式启动 | `hermes_cli/gateway.py:1709` |
| HERMES_GATEWAY_NO_SUPERVISE | 容器内禁自监督 | `hermes_cli/container_boot.py:225` |
| HERMES_S6_SUPERVISED_CHILD | 标记自身是 s6 受监督子进程 | `hermes_cli/gateway.py:6592` |
| HERMES_GATEWAY_MAX_STARTS / HERMES_GATEWAY_START_WINDOW_S | respawn 风暴断路器覆盖(config gateway.respawn_storm) | `hermes_cli/gateway.py:4951/4957` |
| HERMES_GATEWAY_EXIT_DIAG | 退出诊断日志(默认 1) | `hermes_cli/gateway.py:4884` |
| HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT | 平台连接超时(config gateway.platform_connect_timeout 桥接;显式设置胜) | `gateway/run.py:1826` |
| HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT | 停机时每适配器断连超时 | `gateway/run.py:3763` |
| HERMES_RESTART_DRAIN_TIMEOUT | /restart 排空超时(config agent.restart_drain_timeout 的 env 面) | `gateway/run.py:5360` |
| HERMES_GATEWAY_BUSY_INPUT_MODE / HERMES_GATEWAY_BUSY_TEXT_MODE / HERMES_GATEWAY_BUSY_ACK_ENABLED / HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED | 忙时新消息策略(interrupt/queue/steer)与确认气泡开关 | `gateway/run.py:5323/5963/5985`、`gateway/platforms/base.py:2577` |
| HERMES_BACKGROUND_NOTIFICATIONS | 后台进程观察者消息级别(all/error/off) | `gateway/run.py:5386` |
| HERMES_FILTER_SILENCE_NARRATION | 过滤"我保持沉默"类旁白(config gateway.filter_silence_narration 覆盖) | `gateway/delivery.py:383` |
| HERMES_TOOL_PROGRESS / HERMES_TOOL_PROGRESS_MODE | 工具进度显示(gateway;per-platform 解析回退) | `gateway/run.py:18967` |
| HERMES_HUMAN_DELAY_MODE / _MIN_MS / _MAX_MS | 出站拟人延迟(off/custom;默认 800-2500ms) | `gateway/platforms/base.py:5077-5089` |
| HERMES_MEDIA_DELIVERY_STRICT / HERMES_MEDIA_ALLOW_DIRS / HERMES_MEDIA_TRUST_RECENT_FILES / HERMES_MEDIA_TRUST_RECENT_SECONDS | 媒体投递门(config gateway.strict/media_delivery_allow_dirs/trust_recent_files 的桥接 env) | `gateway/platforms/base.py:973-982` |
| HERMES_KANBAN_ATTACHMENTS_ROOT / HERMES_KANBAN_HOME | kanban 附件根覆盖 | `gateway/platforms/base.py:1182/1185` |
| HERMES_TELEGRAM_NOTIFICATIONS / HERMES_TELEGRAM_DISABLE_FALLBACK_IPS / HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS | Telegram:通知级别 / 禁 DNS 回退 IP / 追问宽限(默认 3.0s) | `plugins/platforms/telegram/adapter.py:9213/3516`、`gateway/run.py:10599` |
| HERMES_MATRIX_TEXT_BATCH_DELAY_SECONDS / _SPLIT_DELAY_SECONDS | Matrix 文本合批延迟(0.6/2.0s) | `plugins/platforms/matrix/adapter.py:988/991` |
| HERMES_SIMPLEX_TEXT_BATCH_DELAY | SimpleX 文本合批延迟(0.8s) | `plugins/platforms/simplex/adapter.py:195` |
| HERMES_MEET_URL / _OUT_DIR / _HEADED / _AUTH_STATE / _GUEST_NAME / _DURATION / _MODE / _REALTIME_MODEL / _REALTIME_VOICE / _REALTIME_INSTRUCTIONS / _REALTIME_KEY / _LOBBY_TIMEOUT | Google Meet bot 子进程全套参数(transcribe/realtime 模式) | `plugins/google_meet/meet_bot.py:448-459,619` |
| _HERMES_GATEWAY(内部,带下划线前缀) | 终端工具判断"跑在 gateway 进程内" | `tools/terminal_tool.py:2368` |

### 4.4 Cron

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_CRON_TIMEOUT | 单任务不活动超时(claim TTL 联动下限) | `cron/jobs.py:209` |
| HERMES_CRON_SCRIPT_TIMEOUT | 纯脚本任务超时 | `cron/scheduler.py:2032` |
| HERMES_CRON_SESSION_DB_TIMEOUT | 任务内 SessionDB 初始化上限(默认 10s;0=不限) | `cron/scheduler.py:2812` |
| HERMES_CRON_MAX_PARALLEL | 每 tick 并行任务数(config cron.max_parallel_jobs 覆盖;1=串行) | `cron/scheduler.py:3955` |
| HERMES_CRON_AUTO_DELIVER_PLATFORM / _CHAT_ID / _THREAD_ID | 任务投递目标注入(派生的任务进程读) | (cron/scheduler 派生链,各 5 处) |
| HERMES_MACHINE_ID | tick 认领者标识(缺省 hostname+pid;正确性靠文件锁不靠它) | `cron/jobs.py:1740` |

### 4.5 Kanban

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_KANBAN_TASK / HERMES_KANBAN_RUN_ID | 派发器注入:本 worker 的任务 id / 运行 id(触发 worker 语义:终局工具守卫、goal 模式等) | `agent/kanban_stop.py:31`、`tools/kanban_tools.py:112` |
| HERMES_KANBAN_BOARD / HERMES_KANBAN_DB | 钉板 / 直接钉 DB 文件(解析顺序见 SRS §J) | `agent/skill_utils.py:255`、`hermes_cli/kanban_db.py:530` |
| HERMES_KANBAN_WORKSPACES_ROOT | 工作区根覆盖 | `hermes_cli/kanban_db.py:552` |
| HERMES_KANBAN_DISPATCH_IN_GATEWAY | 派发器开关 env 覆盖 | `gateway/kanban_watchers.py:143` |
| HERMES_KANBAN_CLAIM_LOCK / HERMES_KANBAN_CLAIM_TTL_SECONDS / HERMES_KANBAN_CRASH_GRACE_SECONDS / HERMES_KANBAN_BUSY_TIMEOUT_MS | 认领锁文件 / 认领 TTL / 崩溃回收宽限 / SQLite busy 超时 | `tools/kanban_tools.py:264`、`hermes_cli/kanban_db.py:206/246/1305` |
| HERMES_KANBAN_GOAL_MODE / HERMES_KANBAN_STOP_NUDGE | worker goal 模式 / 终局 nudge 开关 | `cli.py:16039`、`agent/kanban_stop.py:31` |
| HERMES_TENANT | kanban 项的租户标记回退 | `tools/kanban_tools.py:1082` |

### 4.6 Dashboard / 移动端 / 通知

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_DASHBOARD_SESSION_TOKEN | 钉死回环模式会话 token(缺省每进程随机;多 worker 必设) | `hermes_cli/web_server.py:311` |
| HERMES_TRUSTED_PROXIES | 受信反代 IP/CIDR 清单(逗号分隔);**默认空=完全忽略 X-Forwarded-For**,peer 即客户端;胜过 config `dashboard.trusted_proxies` | `hermes_cli/dashboard_auth/client_ip.py:51,84` |
| HERMES_DASHBOARD_PUBLIC_URL | 公网 URL 权威(OAuth redirect_uri 构造;设了则忽略 X-Forwarded-Prefix) | `hermes_cli/dashboard_auth/prefix.py:223` |
| HERMES_DASHBOARD_OAUTH_CLIENT_ID / HERMES_DASHBOARD_PORTAL_URL | Nous OAuth 门的 client_id / portal(胜过 config dashboard.oauth.*;Fly 平台密钥注入路径) | `plugins/dashboard_auth/nous/__init__.py:577/592` |
| HERMES_DASHBOARD_BASIC_AUTH_USERNAME / _PASSWORD / _PASSWORD_HASH / _SECRET / _TTL_SECONDS | 密码门各键的 env 面(胜过 config dashboard.basic_auth.*) | `plugins/dashboard_auth/basic/`(config.py:2100-2114 注释列全) |
| HERMES_DASHBOARD_DRAIN_SECRET | drain 服务凭据(≥43 url-safe-b64 字符,弱密钥注册即拒) | `plugins/dashboard_auth/drain/__init__.py:239` |
| HERMES_DASHBOARD_WS_HOST | WS 回连 host 覆盖(容器场景) | `hermes_cli/web_server.py:17666` |
| HERMES_SERVE_HEADLESS | =1 headless 后端(不服 SPA) | `hermes_cli/web_server.py:18801` |
| HERMES_WEB_DIST | SPA dist 目录覆盖 | (subscript 读,web_server) |
| HERMES_MOBILE_REGISTRATION_ENABLED | 开放移动端 owner 注册(与 OWNER_EMAIL 同为前置条件) | `hermes_cli/dashboard_auth/owner_mobile.py:174` |
| HERMES_MOBILE_API_KEY | iOS 原生静态 API key provider(设了即启用;`/api/env` 永不回显它) | `hermes_cli/dashboard_auth/mobile_api_provider.py:16`、`web_server.py:7746` |
| HERMES_QQ_SMTP_USERNAME / _AUTH_CODE / _HOST / _PORT | 移动注册验证码的 QQ SMTP(默认 smtp.qq.com:465) | `owner_mobile.py:263-270` |
| HERMES_APNS_KEY_ID / _TEAM_ID / _PRIVATE_KEY / _BUNDLE_ID / _ENVIRONMENT | APNs provider-token 推送配置 | `hermes_cli/dashboard_auth/mobile_notifications.py:42-43,559-560,808` |
| HERMES_IOS_DATA_KEY / HERMES_DATA_ENCRYPTION_KEY | iOS intelligence 存储加密密钥(缺省生成本地开发密钥) | `hermes_cli/ios_intelligence.py:878-879` |
| HERMES_IOS_INTELLIGENCE_DIR | iOS intelligence 存储目录覆盖 | `hermes_cli/ios_mcp_server.py:562` |
| HERMES_QWEATHER_API_KEY / _API_BASE_URL、HERMES_AMAP_WEB_API_KEY / _API_BASE_URL / _WEB_API_BASE_URL | iOS 智能卡天气/地图上游 | `hermes_cli/ios_intelligence.py:3735-3739,3815-3820` |
| HERMES_DESKTOP | =1 桌面形态(dashboard 进程内起 cron ticker;禁会话池共享) | `hermes_cli/main.py:12552` |
| HERMES_DESKTOP_READY_FILE / HERMES_DESKTOP_CHILD_PID / HERMES_DESKTOP_DISABLE_GPU / HERMES_DESKTOP_TERMINAL | Electron 就绪握手 / 更新时排除子进程 / GPU 策略 / 内嵌终端 hint | `web_server.py:20001`、`hermes_cli/main.py:6334`、config 桥、`agent/system_prompt.py:143` |

### 4.7 写审批 / 协作连接器

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_WRITE_APPROVAL_REQUIRE_DIGEST | **默认 "1"**:approve 必须回显 payload_digest(防混淆代理);=0 仅为兼容旧客户端(自担风险);reject 永不要求 | `hermes_cli/account_write_approvals.py:127,512` |
| HERMES_COLLABORATION_CONNECTOR_TOKEN / _TOKEN_FILE / _TOKENS / _TOKENS_FILE | connector Bearer 凭据(单值/文件/JSON map id→token/映射文件;未配置 → connector 路由 503 fail-closed) | `plugins/collaboration/dashboard/plugin_api.py:383-386,414,180` |
| HERMES_REMOTE_RUN_WAIT_SECONDS | 远端 run 协调等待上限(默认 86400) | `plugin_api.py:6021` |

### 4.8 TUI / CLI 显示

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_TUI | =1 强制 Ink TUI(=--tui) | `hermes_cli/main.py:286` |
| HERMES_TUI_RESUME / HERMES_TUI_TOOLSETS / HERMES_TUI_SKILLS / HERMES_TUI_CHECKPOINTS / HERMES_TUI_PASS_SESSION_ID / HERMES_TUI_PROVIDER / HERMES_TUI_MAX_TURNS | TUI 会话参数注入(resume id、工具集、技能、检查点、provider、轮数) | `tui_gateway/server.py:5153-5155,2513,2966,4592,4602` |
| HERMES_TUI_DIR / HERMES_TUI_FORCE_BUILD | TUI 源目录(--dev)/ 强制重建 | `hermes_cli/main.py:1878/1711` |
| HERMES_TUI_SIDECAR_URL | stdio TUI 把事件镜像到 dashboard /api/pub | `tui_gateway/entry.py:40` |
| HERMES_TUI_RPC_POOL_WORKERS | 长任务 RPC 线程池(默认 8) | `tui_gateway/compute_host.py:610` |
| HERMES_TUI_SESSION_TTL_S / HERMES_TUI_WS_ORPHAN_REAP_GRACE_S / HERMES_TUI_SLASH_TIMEOUT_S / HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S / HERMES_TUI_GATEWAY_NO_FLUSH | TUI 会话 TTL(6h)/ WS 孤儿回收宽限(20s)/ slash 超时(45s)/ 停机宽限 / 禁 flush(诊断) | `tui_gateway/server.py:910,161,144`、`tui_gateway/entry.py:64`、`tui_gateway/transport.py:58` |
| HERMES_TUI_TOOL_PROGRESS | TUI 工具进度模式(off/new/all/verbose) | `tui_gateway/server.py:2951` |
| HERMES_TUI_THEME / HERMES_TUI_BACKGROUND / HERMES_TUI_NO_EARLY_DISABLE / HERMES_TUI_DISABLE_MOUSE / HERMES_TUI_INLINE | 主题亮暗 hint / 背景色 hint / 鼠标残留诊断 / 禁鼠标 / 内联模式 | `cli.py:2303/2312`、`hermes_cli/main.py:305`、(subscript) |
| HERMES_COMPUTE_HOST_CHILD / HERMES_COMPUTE_HOST_HEARTBEAT_SECS | turn 隔离 compute host 子进程标记/心跳(15s) | `tui_gateway/server.py:1219`、`compute_host.py:149` |
| HERMES_ISO_CERTIFY_SYNTH_TURN | 隔离认证合成 turn 测试缝 | `tui_gateway/synthetic_turn.py:42` |
| HERMES_VOICE / HERMES_VOICE_TTS / HERMES_VOICE_DEBUG | 语音模式运行时标记 / 回复 TTS / 语音子进程日志 | `tui_gateway/server.py:14833/14838`、`hermes_cli/voice.py:239` |
| HERMES_SPINNER_PAUSE | 暂停 CLI spinner(录屏/测试) | `agent/display.py:1170` |
| HERMES_FAST_STARTUP_BANNER | 快速横幅路径 | `cli.py:3591` |
| HERMES_TERMUX_DISABLE_FAST_CLI / HERMES_TERMUX_FORCE_SKILLS_SYNC / HERMES_TERMUX_PREFETCH_UPDATES | Termux 启动微优化开关 | `hermes_cli/main.py:377/858/898` |
| HERMES_PET_IMAGE_PROVIDER / HERMES_PET_REFERENCE_MAX_BYTES | 宠物生成图像 provider 钉死 / 参考图上限(16MiB) | `agent/pet/generate/imagegen.py:47`、`tui_gateway/server.py:7881` |

### 4.9 工具 / 终端 / 浏览器 / 媒体

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_GIT_BASH_PATH | Windows 上 bash.exe 显式路径(终端工具是"persistent POSIX bash shell",`tools/terminal_tool.py:968`) | `tools/environments/local.py:631` |
| HERMES_DOCKER_BINARY | docker/podman 可执行覆盖 | `tools/environments/docker.py:286` |
| HERMES_ALLOW_PRIVATE_URLS | =true 放行私网/回环 URL(SSRF 总开关 env 面;胜过 config security.allow_private_urls) | `tools/url_safety.py:218` |
| HERMES_SKIP_SSL_GUARD / HERMES_CA_BUNDLE | 跳过 SSL 篡改守卫 / 自定义 CA(次序:参数 > HERMES_CA_BUNDLE > SSL_CERT_FILE) | `agent/ssl_guard.py:29`、`agent/ssl_verify.py:50` |
| HERMES_REDACT_SECRETS | 默认 true;=false 关闭输出脱敏(gateway/CLI 启动会记警告) | `agent/redact.py:69` |
| HERMES_PROVIDER_ENV_BLOCKLIST / HERMES_PROVIDER_ENV_FORCE_PREFIX | 供应商 env 透传的黑名单/强制前缀(17/7 处消费,tools/env_passthrough.py 族) | (env_passthrough) |
| HERMES_RPC_DIR / HERMES_RPC_TOKEN / HERMES_RPC_SOCKET | execute_code 文件 RPC 目录 / 令牌 / socket | `tools/code_execution_tool.py:475/445`、(subscript) |
| HERMES_VISION_DOWNLOAD_TIMEOUT / HERMES_VISION_MAX_CONCURRENCY | 视觉图片下载超时 / 编码并发上限(下限 1) | `tools/vision_tools.py:57/139` |
| HERMES_LOCAL_STT_COMMAND / HERMES_LOCAL_STT_LANGUAGE | 本地 STT 外部命令 / 语言 | `tools/transcription_tools.py:1494` 等 |
| HERMES_COMPUTER_USE_BACKEND / HERMES_CUA_DRIVER_CMD | CUA 后端选择(默认 cua)/ cua-driver 命令钉死 | `tools/computer_use/tool.py:158`、`cua_backend.py:140` |
| HERMES_COPILOT_ACP_COMMAND / HERMES_COPILOT_ACP_ARGS | copilot-acp 外部进程命令/参数 | `agent/copilot_acp_client.py:64/71` |
| HERMES_DEBUG_INTERRUPT | 中断循环诊断日志 | `tools/environments/base.py:34` |
| HERMES_SKILL_DIR | 技能模板变量(SKILL.md 内 ${HERMES_SKILL_DIR} 替换源) | (skills 链,5 处) |
| HERMES_WORKFLOW_AUDIT_KEY | workflows 插件审计签名密钥 | `plugins/workflows/store.py:177` |

### 4.10 记忆 / 插件

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_HONCHO_HOST | honcho peer/host 覆盖 | `plugins/memory/honcho/client.py:65` |
| HERMES_PGVECTOR_PASSWORD | mem0 OSS 管理容器的 Postgres 口令覆盖(缺省:安装时生成随机口令,0o600 存 `<HERMES_HOME>/pgvector-password`) | `plugins/memory/mem0/_setup.py`(_PGVECTOR_PASSWORD_ENV) |
| HERMES_ENABLE_PROJECT_PLUGINS | 允许发现 `./.hermes/plugins`(项目插件;Python 永不自动 import) | `hermes_cli/web_server.py:19355` |
| HERMES_PLUGINS_DEBUG | 插件加载调试日志(import 时读一次) | `hermes_cli/plugins.py:96` |

### 4.11 供应商 / 凭据 / 计费

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_API_KEY / HERMES_BASE_URL | custom provider 的 key/端点(TUI 状态页也读) | `tui_gateway/server.py:15492/15494` |
| HERMES_PORTAL_BASE_URL | Nous Portal base(或 NOUS_PORTAL_BASE_URL) | `hermes_cli/auth.py:2099` |
| HERMES_CODEX_BASE_URL / HERMES_QWEN_BASE_URL / HERMES_XAI_BASE_URL | 各 OAuth 供应商端点覆盖 | `hermes_cli/auth.py:3764/2483`、`agent/auxiliary_client.py:1854` |
| HERMES_NOUS_TIMEOUT_SECONDS / HERMES_NOUS_MIN_KEY_TTL_SECONDS | Nous 请求超时(15s)/ key 最小 TTL(1800s) | `hermes_cli/nous_auth_keepalive.py:34`、`agent/auxiliary_client.py:897` |
| HERMES_SHARED_AUTH_DIR | 跨 profile 共享 auth 存储目录 | `hermes_cli/auth.py:4748` |
| HERMES_OAUTH_TRACE | OAuth 流调试日志 | `hermes_cli/auth.py:876` |
| HERMES_OPENROUTER_CACHE / HERMES_OPENROUTER_CACHE_TTL | OpenRouter 响应缓存开关/TTL(胜过 config openrouter.*) | `agent/auxiliary_client.py:646/658` |
| HERMES_CODEX_TTFB_STRICT / HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS | Codex 首字节看门狗严格模式/大请求禁用阈值 | `agent/chat_completion_helpers.py:710` |
| HERMES_SPOTIFY_CLIENT_ID | Spotify OAuth 应用覆盖 | (hermes_cli/auth.py 族,7 处) |
| HERMES_DEV_CREDITS / HERMES_DEV_CREDITS_FIXTURE / HERMES_DEV_BILLING_FIXTURE / HERMES_DEV_SUBSCRIPTION_FIXTURE | 计费/订阅 UI 假数据夹具(仅显式启用;防伪造真实余额) | `agent/credits_tracker.py:657`、`agent/billing_usage.py:270`、`agent/billing_view.py:364`、`agent/subscription_view.py:381` |

### 4.12 诊断 / 杂项

| 变量 | 作用 | 读取位置 |
|---|---|---|
| HERMES_DISABLE_WINDOWS_UTF8 | =1 退回 cp1252 stdio(诊断编码问题用) | `hermes_cli/stdio.py:131` |
| HERMES_DUMP_REQUESTS / HERMES_DUMP_REQUEST_STDOUT | 倾印 API 请求(文件/标准输出) | `agent/conversation_loop.py`(env_var_enabled) |
| HERMES_LANGFUSE_PUBLIC_KEY / HERMES_LANGFUSE_SECRET_KEY | Langfuse 观测导出(回退无前缀 LANGFUSE_*) | `plugins/observability/langfuse/__init__.py:168` |
| HERMES_SCALE_TO_ZERO | NAS Labs 缩零印记(功能开关本体,非 config 键) | (gateway relay/idle 链,7 处) |

---

## 5. 安全模型(运维视角)

原理与逐路由细节见 ARCHITECTURE §4.3/§5.2 与 API-HTTP §2;本节只讲"运维必须知道/必须配置"的部分。

### 5.1 Dashboard 认证与客户端 IP

- **绑定即策略**:回环绑定(127.0.0.1/localhost/::1)= 临时 token 模式(token 注入 SPA HTML);任何非回环绑定 = 强制账号门(OAuth/密码),`--insecure`/`allow_public` **接受但忽略**(2026-06 hermes-0day 后封死),零 provider 时 fail-closed 拒绝启动(`SystemExit`)。LAN/RFC1918 一律按公网对待。
- **密码登录限流**:每 IP 10 次/60s **且** 每账号 20 次/300s(键上限 4096,溢出坍缩到共享桶更严;`hermes_cli/dashboard_auth/routes.py`);移动端 owner 注册/登录复用同一预算(`owner_mobile.py`)。
- **限流键的可信性 = HERMES_TRUSTED_PROXIES**(`hermes_cli/dashboard_auth/client_ip.py`,本月新增,修 H1):默认(未配置)**完全忽略 X-Forwarded-For**,以传输层 peer 为准——直连部署无法伪造;配置后仅当 peer 是受信代理才**从右向左**走 XFF,取第一个非受信条目(nginx `$proxy_add_x_forwarded_for` 追加语义的正确读端;最左段恰是攻击者可控段)。非法条目终止行走。config 面 `dashboard.trusted_proxies`,env 胜。**部署在反代后必须配置它**,否则限流/审计按代理 IP 记(全体请求同桶,可能误伤;但绝不会更宽松)。
- **运维动作**:公网部署三选一——`dashboard.basic_auth`(username+password_hash;`secret` 建议钉死否则会话不跨重启)、Nous OAuth(`HERMES_DASHBOARD_OAUTH_CLIENT_ID`)、自建 provider 插件。多 uvicorn worker 必须钉 `HERMES_DASHBOARD_SESSION_TOKEN` + 外置 OAuth 状态(见 `web_server.py` `DashboardRuntimeState` 注记的单进程约束)。
- 秘密比较三面统一走根模块 `hermes_secret_compare.py`(常量时间 / 双侧 UTF-8 / 空值 fail-closed);新 HTTP 面禁止手写 `==`。

### 5.2 秘密文件权限(0600/0700 策略)

| 文件/目录 | 策略 | 来源 |
|---|---|---|
| `~/.hermes/`(HERMES_HOME) | 目录 0o700(`HERMES_HOME_MODE` 覆盖;managed/容器可跳过) | `hermes_cli/config.py:843,863` |
| `auth.json` | O_EXCL 原子创建 + 0o600 + 跨进程文件锁 | `hermes_cli/auth.py`(SRS §A.1) |
| `.env` | doctor --fix 创建即 chmod 0o600;写路径用 `write_secret_file` | `hermes_cli/doctor.py`(§7.1)、`utils.py::write_secret_file` |
| `write-approvals.db`(含 -wal/-shm) | 0o600;专属父目录才 0o700(默认在共享 home 根,不越权翻整根) | `hermes_cli/account_write_approvals.py:243-273` |
| `dashboard/mobile-auth.db` | 目录 0o700、文件 0o600、WAL | `mobile_device_store.py`(SRS §G.3) |
| memory 插件密钥(mem0/hindsight 的 .env 与 profile env) | 统一经 `utils.write_secret_file`:temp 文件**先** fchmod 0o600 再写入秘密字节、fsync、原子换入——TOCTOU 安全(write_text+chmod 的窗口被消除) | `utils.py`(本月新增)、`plugins/memory/{mem0/_setup.py,hindsight/__init__.py}` |
| mem0 OSS Postgres | 无硬编码口令:随机生成、0o600 持久于 `<HERMES_HOME>/pgvector-password`;`HERMES_PGVECTOR_PASSWORD` 覆盖 | `plugins/memory/mem0/_setup.py` |
| connector token | 部署脚本 umask 077;文件 `/etc/hermes-agent/collaboration-connector-token(s.json)` | `deploy/public/configure-connector-credential.sh` |

**新代码规则**:写任何含密钥的纯文本 → `utils.write_secret_file(path, text, mode=0o600)`;JSON → `atomic_json_write(..., mode=)`;绝不 `write_text` 后补 chmod。

### 5.3 写审批(memory/skills)

完整状态机见 SRS §K.4。运维要点:默认门**关**(`memory.write_approval`/`skills.write_approval: false`);开门后审批面在 `/memory`·`/skills` 斜杠命令、CLI `hermes memory`、iOS(collaboration 插件 mobile 门面)。`HERMES_WRITE_APPROVAL_REQUIRE_DIGEST` 默认 "1"——批准必须绑定所见 payload 的摘要,仅在对接旧客户端时临时 =0(恢复混淆代理风险,修完即撤)。审批库是 `write-approvals.db`,备份必带(§6)。

### 5.4 Webhook 重放 / SSRF / 媒体投递

- **Feishu 重放防护**(`plugins/platforms/feishu/adapter.py:231-240,3563-3693`,本月加固):签名校验(encrypt_key 配置时强制,常量时间)**先于** url_verification 回显;时间戳新鲜度窗 1 小时(匹配飞书重试指引)+ `(timestamp,nonce)` 单次使用去重(容量 4096 有界);nonce **仅在 2xx 出口提交**——飞书对非 2xx 会带同一组头重试,提前记账会把重试当重放丢事件;签名检查与提交之间无 `await`(窗口保持关闭)。
- **Slack 附件 SSRF/凭据保护**(`plugins/platforms/slack/adapter.py:408-478`):`url_private(_download)` 是事件 JSON 里的攻击者可控值——"remote files" 可指向任意外部 URL;下载前主机钉死到 Slack 自有域(`slack.com`/`slack-files.com` 及其子域;https-only;`user@host` 混淆与相似域 fail-closed),再过 `tools/url_safety.is_safe_url` 私网解析检查 + 重定向守卫——xoxb token 与服务端抓取绝不流向非 Slack 主机。守卫 `_check_slack_download_url` 已登记进 `tests/architecture/test_wired_security_controls.py`(接线测试,防退化成死代码)。
- **BlueBubbles webhook 认证**(`gateway/platforms/bluebubbles.py`):`constant_time_equals(token, self.password)`——修掉旧 `token != self.password`(非常量时间,且空口令时 `"" != ""` 为假 ⇒ **fail-open 无认证触发 agent**);该口令同时是 Hermes 出向访问 BlueBubbles 服务器的凭据,泄露=双向失守。`access_log=None` 防口令进访问日志。
- **通用 URL 安全**:`tools/url_safety.py`(browser/web/图源共用):私网/回环拒绝(`security.allow_private_urls` / `HERMES_ALLOW_PRIVATE_URLS` 放行)、重定向落点复查。浏览器工具另有 `browser.allow_private_urls` 与本地自动降级(`auto_local_for_private_urls`)。
- **媒体投递门**(`gateway/platforms/base.py:973-1385`):agent 输出的裸文件路径要成为平台附件,默认模式过系统路径/凭据denylist + **秘密基名黑名单**(本月新增:`.env` 家族全量、OpenSSH 默认私钥名、`.netrc`/`_netrc`/`.pgpass`/`.htpasswd`、`server.key`/`tls.key`/`apns.key` 等常见私钥名——`.pub` 可投递;**`.key` 后缀不整体封杀**,Keynote 演示文稿仍可交付;大小写不敏感匹配)。strict 模式(`gateway.strict: true`)改为 allowlist+新鲜度窗(公网 gateway 推荐)。入站媒体缓存上限 `gateway.max_inbound_media_bytes: 128MiB`。
- **"必须有生产调用点"的执法**:`tests/architecture/test_wired_security_controls.py` 登记 15 个安全控制符号(含 `_derive_payload_summary`、`_check_slack_download_url`、`neuter_async_httpx_del`、relay 的 `verify_delivery_signature` 等);`archlint.py:539/580` 的 `find_internal_symbol_references`/`find_any_symbol_references` 连同定义模块内引用一起计数——模块私有守卫只被同文件调用不再被误判为零调用死代码。新增安全校验器时**必须**同步登记,否则"声明存在、运行不存在"(FINAL-REPORT 跨仓库主题)重演。

---

## 6. 备份与迁移

### 6.1 HERMES_HOME 下的状态清单(哪些文件承载什么)

| 路径 | 内容 | 备份必要性 |
|---|---|---|
| `config.yaml`(+旁边的 `.lock`) | 全部配置(含 dashboard.basic_auth 凭据、平台节) | **必须**(lock 文件不用备) |
| `.env` | 全部 API key/平台 token(密钥唯一正源) | **必须**(0600) |
| `auth.json` | OAuth 凭据/凭据池(SRS §A.1) | **必须** |
| `state.db`(SQLite WAL) | 会话/消息/用量/路由/压缩世系的**权威库** | **必须**(连同 `-wal`/`-shm` 或先 checkpoint) |
| `sessions/` | legacy 路由镜像 `sessions.json` 与可选 JSON 快照 | 建议(可再生) |
| `kanban.db` + `kanban/`(boards/workspaces/logs/current) | 看板(注意:在**共享根**,不随 profile;SRS §J) | 必须(协作数据) |
| `write-approvals.db` | 写审批状态机 + 待审 payload 明文 | **必须**(0600) |
| `dashboard/mobile-auth.db` | 移动设备/令牌哈希/APNs | 必须(丢失=所有设备重登) |
| `memories/MEMORY.md`、`USER.md`(+可能的 `.bak.<ts>`) | 内置记忆 | **必须** |
| `memory_store.db` / `mem0.json` / `supermemory.json` / `byterover/` | 各 memory 插件本地态 | 用哪个备哪个 |
| `skills/`(含 `.curator_backups/`)、`skill-bundles/`、`plugins/` | 技能/技能包/用户插件 | **必须** |
| `cron/jobs.json` + `cron/output/` | 定时任务(**按 profile**)与输出 | **必须** |
| `pending/{memory,skills}/` | 旧版 JSON 待审(已迁 DB,首次读取时一次性入库) | 迁移期保留 |
| `profiles/<name>/…` | 每 profile 完整重复上述结构 | **必须**(逐 profile) |
| `logs/`、`checkpoints/`、`cache/`、`images/`、`state-snapshots/`、`backups/` | 日志/文件检查点/缓存/上传图/快照/更新备份 | 可不备 |
| `collaboration/rooms.json`、`single.json` | 群聊/单聊(collaboration 插件) | 必须(用了就备) |
| `gateway.pid`、`gateway_state.json`、`gateway-locks/`、`cron/.tick.lock` | 进程运行态 | **不要恢复**(恢复后删除) |
| `shell-hooks-allowlist.json`、`webhook_subscriptions.json`、`.anthropic_oauth.json`、`mcp-tokens/`、`pairing/` | hook 同意/webhook HMAC/凭据 | **必须** |
| HERMES_HOME **之外**:`~/.honcho`、`~/.hindsight`、`~/.openviking` 等 | 外部 memory provider 状态 | 由 provider `backup_paths()` 声明,`hermes backup` 自动收进档案 `_external/` 子树(SRS §K.2) |

### 6.2 官方通道(推荐)

- `hermes backup`:整个 HERMES_HOME 打 zip(默认 `~/hermes-backup-<timestamp>.zip`;不含代码库);`--quick` 只快照关键小文件(config、state.db、.env、auth、cron)。来源:`hermes_cli/subcommands/backup.py:19-33`。恢复:`hermes import`(provider 外部路径还原到原位;home 外路径为安全计跳过)。
- Dashboard 面:`POST /api/ops/backup` + `/api/ops/backup/download`、`/api/ops/import(-upload)`(API-HTTP §2.10)。
- `hermes update` 前自动备份:`updates.pre_update_backup: quick`(默认;`full` = quick + 整 home zip 到 `<HERMES_HOME>/backups/`,保留 `backup_keep: 5` 份;`off` 关闭;`--backup`/`--no-backup` 单次覆盖)。quick 快照进 `<HERMES_HOME>/state-snapshots/`,`/snapshot` 命令可恢复;>1GiB 的单文件(如膨胀的 state.db)跳过并告警。来源:`hermes_cli/config.py:3227-3252`。

### 6.3 手工冷备/迁移程序(安全顺序)

1. `hermes gateway stop`(必要时先 `/restart` 排空;确认 `gateway.pid` 消失)并关闭 dashboard/desktop。
2. SQLite 一致性:进程都停了直接拷 `state.db`+`-wal`+`-shm` 即可;不停机备份则用 `hermes backup`(或对每个 db 执行 `sqlite3 x.db ".backup …"`,`待确认`:仓库未内置该命令封装)。
3. 打包 §6.1 标"必须"的路径(整 home 最简单:`hermes backup`)。
4. 迁移到新机:装同版本 hermes → 解包到目标 home → **删除运行态**(`gateway.pid`、`gateway_state.json`、`gateway-locks/`、`cron/.tick.lock`)→ 权限复查(`.env`/`auth.json`/两个 db 600,home 700)→ `hermes doctor` 全绿再 `hermes gateway start`。
5. 换 HERMES_HOME 位置:导出 `HERMES_HOME=/new/path`(所有入口一致设置——systemd unit、shell profile、compose env)。profile 用户注意 kanban 在共享根(`<root>/kanban.db`),不要只迁单个 profile 目录。
6. 回滚:`hermes update` 出问题 → `<HERMES_HOME>/backups/` 里最近的 full zip 用 `hermes import` 还原,或 `/snapshot` 恢复 quick 快照;代码侧 `hermes update` 的 stash 策略保证本地改动在 git stash 里可找回(`updates.non_interactive_local_changes: stash`)。

---

## 7. 故障排查

### 7.1 `hermes doctor`(来源:`hermes_cli/doctor.py`,2,618 行)

`hermes doctor [--fix] [--ack <advisory-id>]`。`--fix` 自动修可修项(建目录/建 `.env`(0600)/SOUL.md 模板等);`--ack` 快路径:确认安全公告后静默其启动横幅(写 config `security.acked_advisories`)。入口即显式调 `ensure_utf8_stdio()`(box-drawing 横幅在 cp1252 下会崩,doctor.py:651-657)。检查节(`_section` 枚举):

| 节(行号) | 检测内容 |
|---|---|
| Security Advisories(706) | `security_advisories.detect_compromised()` 对照已装包版本;命中未 ack → fail + 完整处置文本(卸载+轮换凭据);已 ack 仍在盘 → warn |
| MCP Server Security(752) | `mcp_security.validate_mcp_server_entry` 扫 `mcp_servers.*` 可疑 stdio 命令(供应链注入面) |
| Python Environment(776) | ≥3.11 ok / 3.10 warn |
| SSL / CA Certificates(804) | 证书链可用性(`check_certificates`) |
| Required Packages(807) | 必需依赖齐备 |
| Configuration Files(836) | managed scope 状态;`.env` 存在+有 provider key(读文件钉 UTF-8——GBK locale 下 read_text 会崩的教训);config.yaml 存在 + `model.provider/default` 合法性(经 `read_raw_config`——mtime 缓存、锁护、解析失败告警,**不再裸 yaml.safe_load**) |
| Config Structure(1249) | 顶层键结构/陈旧根键迁移(经 `mutate_config`,锁内重检测再改——并发写者可能已修好) |
| xAI Model Retirement(1275) | 2026-05-15 退役模型引用 |
| Auth Providers(1300) | 各 OAuth/key 供应商登录态 |
| Directory Structure(1356) | home 与 `cron/ sessions/ logs/ skills/ memories/` 子目录;SOUL.md(空模板提示);MEMORY.md/USER.md 尺寸;state.db 完整性(sqlite3 打开) |
| Command Installation(1558) | `hermes` 命令安装/PATH |
| External Tools(1632) | git/node/rg/docker/agent-browser 等(Termux 有专属提示与豁免) |
| API Connectivity(1946) | 并行探针(线程池,~最慢单项≈2s):OpenRouter(200/401/402 分诊)、Nous、Anthropic、AWS 凭据链等 |
| Tool Availability(2376) | 各工具 check_fn 汇总 |
| Skills Hub(2406) | hub 源可达性 |
| Memory Provider(2447) | 活跃 provider 的 is_available/配置完整性 |
| Profiles(2554) | 各 profile 健康 |
| s6 Supervision(446)/Gateway Service(519) | 容器 s6 树 / systemd·loginctl linger(`sudo loginctl enable-linger`) |
| Context Engineering(206) | SOUL.md/AGENTS.md/记忆等上下文文件的体量与重复度 |

### 7.2 常见故障速查

| 症状 | 原因与处置 | 来源 |
|---|---|---|
| Windows 控制台 `UnicodeEncodeError: 'charmap' codec…` 启动即死 | cp1252/cp437/cp932 stdio。修复是**入口点显式调用**:`ensure_utf8_stdio()`(跨平台流重包)+ `configure_windows_stdio()`(控制台码页 65001、`PYTHONIOENCODING`/`PYTHONUTF8` setdefault、EDITOR=notepad 兜底、PATH 预挂)。已接线入口:`cli.py`、`hermes_cli/main.py`、`gateway/run.py`、`run_agent.py`、doctor。**import hermes_cli 不再有此副作用**(改动说明见 CHANGES);诊断旧行为:`HERMES_DISABLE_WINDOWS_UTF8=1` | `hermes_cli/stdio.py`、`hermes_cli/__init__.py` |
| 刚装完 Windows,报 rg/bash/grep 不存在 | 安装会话的 User PATH 广播未达;首启动已自动预挂 `%LOCALAPPDATA%\hermes\git\*` 等;换新终端亦可 | `hermes_cli/stdio.py:218-274` |
| 端口占用 | dashboard:`--port 0` 让 OS 自派;api_server:端口冲突是**不可重试致命错**(检查 8642);`hermes proxy` 与 BlueBubbles **默认同 8645**,并用必改 | §2.1 |
| docker-compose.windows 的 dashboard 崩溃循环 | 非回环绑定 ⇒ 认证 fail-closed;先配 `dashboard.basic_auth` 或 OAuth(§1.3) | `docker-compose.windows.yml` 注释 |
| gateway 被 supervisor 打成重启风暴 | `restart_loop_guard`(跳过肇事会话 auto-resume)+ `respawn_storm`(指数退避);诊断看 `HERMES_GATEWAY_EXIT_DIAG` 输出与 `logs/errors.log` | §2.2 |
| config.yaml 被写成只剩几行/丢节 | 历史病根:fail-open raw 读(损坏→`{}`)+ 整文档写。现:迁移链 `_persist_migration` 与 `/model` 全局持久化均 **strict 读 + 全程锁**,损坏文件=中止并报错,不再静默重写;修复:按报错改好 YAML 缩进即可,LKG 缓存保证读路径期间安全键不失效 | `hermes_cli/config.py:5808,7132`;tests `TestSaveConfigPartialWritePreservation` |
| NFS/SMB 上每次写配置卡 10s | 旧行为:锁不可用被当成争用烧满超时。现:`_LOCK_UNSUPPORTED` 按路径记忆 + 一次性警告,直接无锁降级 | `hermes_cli/config.py:7268-7299`;tests `tests/architecture/test_config_write_lock.py` |
| 公网 dashboard 登录被限流误伤 / 怀疑限流被绕 | 配 `HERMES_TRUSTED_PROXIES`(§5.1);绕过已在本月修死(默认忽略 XFF) | `client_ip.py` |
| memory 写入报 "Refusing to write … drift … .bak" | 外部写者改了 MEMORY.md;按错误提示把 `.bak.<ts>` 里的内容经 `memory(action=add)` 逐条搬回,清理原文件后重试(#26045) | `tools/memory_tool.py:93-120` |
| state.db 膨胀(数百 MB) | 开 `sessions.auto_prune: true`(+`vacuum_after_prune`)或手动 `hermes sessions prune`;JSON 快照默认已关 | config §3.2 `sessions` |
| dashboard 内嵌终端(Chat 页)在 Windows 上不可用 | PTY 桥仅 POSIX(`_PTY_AVAILABLE`),按提示装进 WSL2 | ARCHITECTURE §F.3(API-HTTP §2.4) |
| 飞书事件"丢了" | 先查签名/时间戳(1h 窗)与 nonce 去重;注意非 2xx 时飞书原样重试且 nonce 不落账,重试应能补投(§5.4) | `plugins/platforms/feishu/adapter.py` |
| 子 agent 突然全部连接错误 | 曾因凭据轮换强关共享 client 误杀在途请求;现仅摘引用、GC 收尾(H7 修复)。若复现,收集 `logs/errors.log` 中 auxiliary_client 段 | `agent/auxiliary_client.py`(CHANGES) |
| cron 任务不触发 | gateway 必须在跑(ticker 在其内);查 `~/.hermes/cron/.tick.lock` 是否被陈旧进程持有、`cron/jobs.json` 的 next_run_at;托管模式(chronos)则查 `/api/cron/fire` 的 JWT 配置(`nas_jwks_url` 空=拒绝一切) | SRS §I |
| 日志在哪 | `~/.hermes/logs/agent.log`(INFO+)、`errors.log`(WARNING+),`logging.*` 控制轮转;gateway 平台各自有前缀日志行;`hermes logs` 子命令过滤查看 | config §3.2 `logging` |

---

## 附:新功能落点速查(给"以后加功能"的自己)

| 你要加的 | 去哪里 | 本文相关节 |
|---|---|---|
| 新配置键 | `DEFAULT_CONFIG`(带注释)+ 读取走 `load_config()`;绝不裸 YAML(棘轮测试会拒);需要 env 覆盖就在读取点实现 env-wins 并**同步登记进 §4 表** | §3 |
| 新环境变量 | 命名 `HERMES_<域>_<义>`;读取点写清默认与语义注释;更新 §4 | §4 |
| 新 HTTP 端点/服务 | 先读 API-HTTP §1 的三面格局;秘密比较用 `hermes_secret_compare`;公开路径进 `PUBLIC_API_PATHS` 要过评审;若涉及入站投递签名(relay),**必须接线 `verify_delivery_signature`** 并登记 wired-security 测试 | §2、§5 |
| 新平台适配器 | `plugins/platforms/<name>/`;webhook 必带签名校验 + 重放窗(参照 Feishu §5.4);默认绑定回环或 fail-closed | §5.4 |
| 新秘密落盘 | `write_secret_file` / `atomic_json_write(mode=0o600)`;更新 §5.2 表与备份清单 §6.1 | §5.2、§6 |
| 新后台状态文件 | 放 HERMES_HOME 下、写进 §6.1 备份表;跨进程写用文件锁(参照 config_write_lock 模式) | §6 |
| 新安全守卫 | 实现 + 生产调用点 + 登记 `tests/architecture/test_wired_security_controls.py`——三件缺一即"声明存在、运行不存在" | §5.4 |

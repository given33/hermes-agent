# WSL gateway / kanban 部署说明

WSL 上 hermes gateway 与 pc connector 的 HERMES_HOME 不同，需要显式统一 kanban DB：

## 1. kanban DB 统一
pc connector 用 `HERMES_HOME=/home/hermes/.hermes`（systemd 单元），
gateway 用 `HERMES_HOME=/mnt/d/Hermes/home`（工作区）。两者 kanban DB 不同，
connector 创建的任务 gateway 看不到。修复：gateway 加
`HERMES_KANBAN_HOME=/home/hermes/.hermes` 环境变量。

## 2. worker profile 可见性
gateway 的 dispatcher 按任务的 assignee（远端 acct-* profile）spawn worker，
profile 必须存在于 gateway 的 HERMES_HOME/profiles/ 下。
缺 profile 时任务停在 ready 永远不 spawn。修复：
`cp -r /home/hermes/.hermes/profiles/<acct-profile> /mnt/d/Hermes/home/profiles/`

## 3. ios-* MCP 会阻塞 gateway 启动
WSL 配置里有 19 个 ios-* MCP（手机专用，指向 127.0.0.1:876x），
启动时逐个连接失败重试（3 次 × 1-4s 退避），拖慢 dispatcher 初始化。
WSL 上不需要它们：config.yaml 里给每个 `ios-*` 加 `enabled: false`。

## 4. gateway systemd 用户服务
`~/.config/systemd/user/hermes-wsl-gateway.service`：
```
[Service]
Environment=HERMES_HOME=/mnt/d/Hermes/home
Environment=HERMES_KANBAN_HOME=/home/hermes/.hermes
ExecStart=/usr/local/bin/hermes gateway run --accept-hooks
Restart=always
```
`loginctl enable-linger hermes` 保证 WSL 启动即运行。

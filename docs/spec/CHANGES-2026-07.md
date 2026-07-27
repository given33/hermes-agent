# 本次审计与优化变更说明(CHANGES-2026-07)

**口径**:基于 2026-07-27 最终复验时 `git status --porcelain` / `git diff --stat` 对基线 `5bc6b9f75` 的工作区快照:**52 个修改文件,+3,898/-531 行**,另有 14 组未跟踪新文件(`hermes_secret_compare.py`、`hermes_cli/dashboard_auth/client_ip.py`、`tests/architecture/` 全套、新回归测试、`docs/spec/` 与 `docs/architecture/layering.md`)。本文件描述当前已落盘并复验的改动，不代表 C1/H2/H3/H5 等长期架构重构已经完成。
**发现编号**(C1/C2、H1-H8、M*、L*)引自审计总报告 `C:/Users/given/hermes-audit/FINAL-REPORT.md`(仓库外);逐条原始定位在该报告 `findings/` 分文件。
**主题分组**:§1 安全修复 / §2 稳定性 / §3 架构约束 / §4 性能 / §5 文档;§6 兼容性与逃生阀总表。

---

## 1. 安全修复

### 1.1 登录限流 XFF 伪造绕过(H1 —— 审计定位的最高优先可利用项)

- **问题**:三个模块各带一份 `_client_ip`,无条件取 `X-Forwarded-For` **最左段**做密码登录限流键与审计 IP。XFF 是纯客户端输入;追加型代理(nginx `$proxy_add_x_forwarded_for`)下最左段恰是攻击者可控段。每请求换一个伪造 XFF ⇒ 每次落新桶 ⇒ **无限口令爆破**;移动端注册/登录端点同病。
- **现在**:新共享模块 `hermes_cli/dashboard_auth/client_ip.py`(193 行)——默认(未配置受信代理)**完全忽略 XFF**、以传输层 peer 为准;配置 `HERMES_TRUSTED_PROXIES`(env,胜)或 `dashboard.trusted_proxies`(config)后,仅当 peer 受信才**自右向左**走链,取第一个非受信条目;非法条目终止行走。`routes.py`/`middleware.py`/`token_auth.py` 三处统一改调它;`owner_mobile.py` 的移动登录另叠加**每账号**预算(纯 IP 桶对多源攻击者失效)。
- **影响面**:dashboard 密码登录、移动端注册/验证码/登录/刷新、全部审计日志 `ip` 字段。
- **兼容性**:直连部署零感知(更严);**反代部署必须配置 `HERMES_TRUSTED_PROXIES`**,否则限流键=代理 IP(全体同桶,可能误伤但绝不更宽)。
- 测试:`tests/hermes_cli/dashboard_auth/test_client_ip_trusted_proxy.py`(新)、`tests/hermes_cli/test_dashboard_auth_password_login.py`(+53 行)。
- 顺手修:`middleware.py` 公开前缀 `startswith` 半数无结尾斜杠(审计 L 项——`/auth/logout` 会匹配假想的 `/auth/logout-all`;当前无可利用路由,但未来加路由会**静默**绕认证)——拆成精确集/前缀集两种语义。

### 1.2 BlueBubbles webhook:非常量时间比较 + 空口令 fail-open(M)

- **问题**:`gateway/platforms/bluebubbles.py:883` 用 `token != self.password`;①逐字节短路泄露匹配前缀(该口令同时是 Hermes 出向访问 BlueBubbles 服务器的凭据);②口令未配置时 `"" != ""` 为假 ⇒ **无凭据请求被放行**,webhook 变成无认证触发 agent 的入口。全仓其余平台 webhook 均用 `compare_digest`,唯此一处例外。
- **现在**:改用新根模块 `hermes_secret_compare.constant_time_equals`(常量时间/双侧 UTF-8 编码/**空值 fail-closed**)。
- **影响面**:iMessage(BlueBubbles)入站;空口令部署此后一律 401——**必须配置 BlueBubbles 服务器密码**。
- 测试:`tests/gateway/test_bluebubbles.py`(+55 行)。

### 1.3 写守卫大小写绕过(M)+ 拒绝理由错报(L)

- **问题**:`agent/file_safety.py` 凭证写拒绝用大小写敏感比较,黑名单是小写字面量;`realpath` 不做大小写归一 ⇒ macOS(APFS 默认大小写不敏感)上写 `~/.SSH/authorized_keys` 判为放行、OS 实际写入真 `~/.ssh/authorized_keys`——提示注入可植入 SSH 公钥。同文件读守卫却已归一化(两守卫不一致)。另:session-state 分支返回布尔 True(非文档化类别),错落到"凭证文件"文案。
- **现在**:`_fold()` = `os.path.normcase` + `casefold`,**无条件**双侧折叠(平台探测不可靠;折叠只可能多拒,误伤面是无人合法书写的 `~/.SSH/…`);读守卫的精确名单同样折叠。`_classify_write_denial` 规范化返回 `'credential' | 'session_state' | 'safe_root' | None`,session-state 有了正确文案("改写会伪造对话历史")。
- **影响面**:全部文件写工具与 ACP 垫片(`file_operations/file_tools/credential_files/image_source` 消费方)。
- 测试:`tests/agent/test_file_safety_case_folding.py`(新)。

### 1.4 写审批混淆代理:payload_digest 绑定(M)+ DB 权限收紧(M)

- **问题**:`hermes_cli/account_write_approvals.py` 的裁决只绑 `(approval_id, revision)`;审批 UI 展示的 `summary` 是 agent 在 stage 时可控的自由文本,与 `payload` 无绑定——被操纵的 agent 可用"记得买牛奶"式 summary 掩护恶意 skill payload 骗取真人批准(apply==stage 本身可证,缺口在"人批的"与"payload"之间)。另:该 DB 落盘走默认 umask(0644/0755),`payload_json` 明文含记忆与 skill 内容,同类的 mobile-auth.db 却早已 0600/0700。
- **现在**(+181 行):①每次读出附 `payload_digest`(canonical-JSON SHA-256)与 **`derived_summary`**(服务端从 payload 本身派生的描述,UI 有了 agent 控制不了的展示物);②`claim_decision(approve)` **默认必须**回显 digest,常量时间比对,不符/缺失 → `ApprovalPayloadMismatch`;`HERMES_WRITE_APPROVAL_REQUIRE_DIGEST=0` 仅为旧客户端过渡(注释明言恢复混淆代理风险);**reject 永不要求**(丢弃是安全动作)。③`_connect()` 强制 DB 与 `-wal`/`-shm` 0o600;专属父目录才 0o700(默认在共享 home 根,不越权)。移动客户端(collaboration 插件 `POST /mobile/write-approvals/{id}/decision`)已回显 digest。
- **影响面**:memory/skills 写审批全链(CLI、gateway、iOS)。
- 测试:`tests/hermes_cli/test_account_write_approval_binding.py`(新)。
- 关联修复:遗留 CLI/gateway 审批路径(`hermes_cli/write_approval_commands.py`,+67 行)接入 `account_write_approval_apply` 的 prepare/apply 收敛(before/after 摘要;stage 与 approve 之间目标被改 → 拒绝;已到位 → 幂等成功)——补上审计 M 项"遗留路径缺 TOCTOU 收敛"。

### 1.5 秘密落盘 TOCTOU 与 memory 插件密钥(M ×3)

- **问题**:`utils.atomic_yaml_write` 无 mode 参数;mem0/hindsight 三处 API key 用 `write_text` 落盘(umask 默认=世界可读,且"先写后 chmod"存在窗口、崩溃即永久);mem0 OSS 硬编码 Postgres 口令 `hermes` 且 `-p {port}:5432` 绑所有网卡。
- **现在**:`utils.write_secret_file(path, text, mode=0o600)`(+81 行)——temp 文件**先** `fchmod` 再写任何秘密字节、fsync、原子换入、保属主;mem0 `_setup.py`(+77)与 hindsight(+17)全部改走它;mem0 的 pgvector 容器**无硬编码口令**:安装时生成随机口令、0o600 持久于 `<HERMES_HOME>/pgvector-password`,`HERMES_PGVECTOR_PASSWORD` 可覆盖。
- 测试:`tests/test_utils_secret_write.py`(新)、`tests/plugins/memory/test_mem0_setup.py`(+60)。

### 1.6 Feishu webhook 重放防护(新增)

- **问题**:签名把 timestamp 折进摘要但从不对钟——截获的 `(timestamp, nonce, signature, body)` 四元组**永久可重放**;且 url_verification 回显在签名校验之前(签名是唯一认证器时,未认证 POST 可拿到攻击者数据的回显)。
- **现在**(`plugins/platforms/feishu/adapter.py`,+85 行):签名常量时间校验(encrypt_key 配置时强制)**先于**一切回显;时间戳新鲜度窗 1 小时(匹配飞书自身重试指引)+ `(timestamp,nonce)` 单次使用去重(容量 4096 有界淘汰);**nonce 仅在 2xx 出口提交**——飞书对非 2xx 用同一组头重试,提前记账会把合法重试当重放丢事件;签名检查到提交之间无 `await`,窗口不重开。
- 测试:`tests/gateway/test_feishu.py`(+60)。

### 1.7 Slack 附件 SSRF / bot token 外流(新增)

- **问题**:`url_private(_download)` 直接来自事件 JSON;Slack "remote files" 允许任意工作区成员/共享频道访客把它指到**任意外部 URL**——服务端带 `xoxb` token 抓取 = 凭据外送 + 对内网(元数据端点)的 credentialed SSRF 探针。
- **现在**(`plugins/platforms/slack/adapter.py`,+107 行):下载前主机钉死到 Slack 自有域(`slack.com`/`slack-files.com` 及子域;https-only;`user@host`、相似域 fail-closed),再过 `tools/url_safety` 私网检查与重定向守卫(`_ssrf_redirect_guard` 同款)。守卫 `_check_slack_download_url` 登记进接线测试(§3.4)。

### 1.8 媒体投递的秘密基名黑名单(新增;gateway/platforms/base.py)

- **问题**:投递 denylist 只按**位置**(`~/.hermes/.env` 拒,`~/code/clientA/.env` 放)——一条提示注入的 `MEDIA:` 标签即可把任意 checkout 里的 `.env` 发进聊天。
- **现在**:`_MEDIA_DELIVERY_DENIED_BASENAMES`——`.env`/`.env.*`/`.envrc` 全量、OpenSSH 默认私钥名(`id_rsa` 等 6 种;`.pub` 可投递)、`.netrc`/`_netrc`/`.pgpass`/`.htpasswd`、常见 TLS/APNs 私钥名(`server.key`/`tls.key`/`apns.key`…);大小写不敏感(与 1.3 同理)。**`.key` 后缀不整体封杀**——Apple Keynote 也是 `*.key` 且在投递类型表里,枚举具体私钥名而非一刀切。
- **影响面**:全部平台适配器的出站附件(共享助手统一执行)。测试:`tests/gateway/test_platform_base.py`(+225)。

### 1.9 秘密比较统一 + relay 校验器"悬空"显式化(H2 局部 / M)

- `hermes_secret_compare.py`(新,仓库根):dashboard(FastAPI)、api_server(aiohttp)、BlueBubbles 三面共用的常量时间比较(`bearer_matches`/`constant_time_equals`);api_server `_check_auth` 删掉手抄副本("agreement by comment"正是三面漂移的根源,kanban dashboard 事故的复刻路径)。
- `gateway/relay/auth.py`(+28):给 `verify_delivery_signature` 补上 **UNWIRED 大字注记**——实现完整、8 条测试、**零生产调用点**(Python gateway 目前无入站 HTTP 投递路由);谁加该端点谁必须接线,并已登记进接线测试(§3.4)。docstring 不再谎称"gateway verifies before accepting"。

### 1.10 Windows compose 反模式移除(L)

`docker-compose.windows.yml`(+20/-x):删除主 compose 明令禁止的 `--insecure --host 0.0.0.0` 组合;改为宿主侧 `127.0.0.1:9119:9119` 映射(回环独占)+ 容器内 `0.0.0.0` 绑定 + **首启动前必须配置 auth provider** 的完整注释(fail-closed 会表现为崩溃循环,注释给出 password_hash 生成命令)。

---

## 2. 稳定性

### 2.1 辅助客户端凭据轮换误杀在途请求(H7)

- **问题**:`agent/auxiliary_client.py` 在 401/429 轮换时对**共享缓存 client** 调 close;锁只护缓存字典不护在途使用——MoA 扇出下一个 advisor 触发轮换会把另一个 advisor 硬切成假 `APIConnectionError`(即 run2 "double-advisor collapse" 的失败形状),且违反同文件自订规则。
- **现在**(+50 行):驱逐=**仅摘引用**,绝不强关;下次查找 miss 即用新凭据重建,旧 client 由引用计数/GC 在在途用户释放后收尾(`neuter_async_httpx_del()` 先解除 SDK `__del__` 的坑,使 GC 收尾安全);容量驱逐同规则。测试:`tests/agent/test_auxiliary_client.py`。
- **影响面**:全部 auxiliary 调用(压缩/视觉/MoA/标题…)在凭据轮换/池切换瞬间的并发正确性。

### 2.2 Bedrock client 构建竞态(M)

`agent/bedrock_adapter.py`(+60):`boto3.client()` 走模块级默认 botocore Session,**构建期非线程安全**——MoA 多线程首次并发调用间歇 `KeyError: 'credential_provider'` / `UnknownServiceError`。现为双检锁:无锁快路径读缓存(GIL 下 dict 读原子、条目只整体替换),boto3 import 在锁外(可能触发 pip 安装),锁内复检后仅一个线程构建;建成的 client 本身线程安全(boto3 文档)。

### 2.3 LSP:fire-and-forget task 被 GC + `_docs` 无界增长(M ×2)

`agent/lsp/client.py`(+115):①事件循环只持任务**弱引用**,裸 `create_task()` 的 server→client 请求分发可能被 GC 回收——rust-analyzer/vtsls 等永远等不到 `workspace/configuration` 应答而阻塞;现以 `self._pending_dispatch` 强引用集 + done-callback 摘除。②`_docs` 每条持全文+诊断、从不 didClose,长会话内存单调涨;现 `MAX_OPEN_DOCS = 64`,超限按最久未开逐出并配对发 `textDocument/didClose`(server 同步丢副本),诊断-only 条目直接裁。测试:`tests/agent/lsp/test_client_docs_bounds.py`(新)。

### 2.4 主循环 4 处零日志吞错(M)+ steer 静默丢失

`agent/conversation_loop.py`(+58):MoA 解码失败、steer 多模态注入失败、`pre_api_request` / `post_api_request` Hook 失败——全部从 `except: pass` 改为 `logger.warning` 带上下文;steer 注入失败时**保持未注入状态重新排队**给 post-tool 排水(旧代码标记已注入 ⇒ 用户的 steer 静默蒸发)。

### 2.5 媒体下载重试(PR #2982)

`gateway/platforms/base.py::cache_image_from_url` 及 Slack/Mattermost 下载路径增加对 5xx/超时的有限重试;测试 `tests/gateway/test_media_download_retry.py`(新,125 行,覆盖三处)。

### 2.6 多进程 config 读-改-写互斥补齐(H6 的 gateway 侧散点)

- `gateway/slash_commands.py`(+294/-x):`/model --global` 两处持久化从"裸 `yaml.safe_load` + `save_config`"改为**整周期持锁**:`config_write_lock()` 内 `read_raw_config_strict()` → 变更 → `save_config`(save 自身的锁可重入嵌套)。strict 读=损坏的 config.yaml 在此**抛错跳过写入**(旧行为读成 `{}` 后 `merge_existing=False` 写回 ⇒ 整份配置只剩 model 节)。
- `plugins/platforms/telegram/adapter.py`(+118):新建 DM topic 的 thread_id 回写、`plugins/platforms/yuanbao.py`(+18):home channel 回写——均改 `mutate_config()`(锁内重读当前文档再变更;旧裸读写会丢并发写者刚保存的键)。
- `hermes_cli/doctor.py`(+85):config 检查改 `read_raw_config`(带缓存/锁/解析告警,raw 语义故意保留——doctor 看用户写了什么);陈旧根键迁移改 `mutate_config` 且**锁内重检测**。

### 2.7 迁移链 fail-closed(新发现,基线可复现的数据丢失)

- **问题**:`hermes_cli/config.py::_persist_migration`(18 个迁移调用点的唯一汇聚口,`hermes update`/首启/`doctor --fix`/建 profile 都无人值守地跑它)沿用"fail-open raw 读 + 全文档写"。**基线实证**:一个坏缩进的 config.yaml 被静默替换成 4 行,model 与平台凭据全丢。`require_readable_config_before_write` 堵不住——它证明字节可读,不证明能解析。
- **现在**:`_persist_migration` 持 `config_write_lock()` 并先 `read_raw_config_strict()`;损坏文档 ⇒ 中止迁移并报错("Config format update failed"),绝不静默重写。`read_raw_config_strict`(config.py:7132,新)与 fail-open 的 `read_raw_config` 并列,docstring 写明选择规则:**任何"读出来再整文档写回"的路径必须用 strict**。
- 测试:`tests/hermes_cli/test_config.py::TestSaveConfigPartialWritePreservation`(4 例,新)、`tests/hermes_cli/test_read_raw_config_strict.py`(新)。

### 2.8 部署脚本回滚正确性(L ×2)

- `deploy/public/install-collaboration-backend.sh`(+31):`mutated` 标志——首个就地替换**之前**失败(stop 或快照失败)不再跑 restore_*(旧行为把"无备份文件"误报为恢复失败并把服务留在停止态),只确保服务重新启动。
- `deploy/recovery/configure-main-managed-installation-ssh.sh`(+75):`changed` 标志仅在原子 rename 前置位;回滚返回值区分 1(恢复失败,盘上可能还是新配置)/ 2(盘上已恢复,仅 reload 未完成);目录 fsync 失败不再把成功恢复翻成失败。修审计"SSH 回滚误报"项。

---

## 3. 架构约束(C1/H3/H4/H5 的当期处置:圈住 + 棘轮,不是一次性重写)

### 3.1 `tests/architecture/` 执法套件(新目录;C1 的制度化response)

六个测试 + `archlint.py` + `baselines/`(**基线只许收紧**,改善后 `archlint.py --write-baselines` 锁战果):

| 测试 | 冻结/执法内容 |
|---|---|
| `test_dependency_direction.py` | 跨包 import 边与**延迟 import 计数**棘轮("a deferred import is a cycle that only fails when the function first runs") |
| `test_bare_config_reads.py` | 裸 `yaml.safe_load(config.yaml)` 冻结允许清单(23 处,gateway/run.py 独占 7)——新增即红 |
| `test_private_symbol_imports.py` | 跨包私有符号 import(438 条)冻结 |
| `test_config_write_lock.py` | 锁语义契约(§4.1 的三态、降级、可重入)——6 例通过 |
| `test_ignore_user_config_convergence.py` | 两个配置加载器对 `HERMES_IGNORE_USER_CONFIG` 语义一致(H4 的收敛证明) |
| `test_wired_security_controls.py` | **15 个安全控制符号必须有生产调用点**(6→15;新入册:`_derive_payload_summary`、`_check_slack_download_url`、`neuter_async_httpx_del`、relay `verify_delivery_signature` 等);`archlint.py:539/580` 新增 `find_internal_symbol_references`/`find_any_symbol_references`——旧检测器跳过定义模块,同文件调用的模块私有守卫呈现零调用、与死代码不可分 |

全套 30 例通过。配套度量文档 `docs/architecture/layering.md`(新,180 行)。

### 3.2 import 副作用清除(H3 的最尖锐点)

- **问题**:`hermes_cli/__init__.py` 在 **import 瞬间**改写全进程 stdout/stderr 并 setdefault `PYTHONUTF8`/`PYTHONIOENCODING`——任何 `from hermes_cli.config import …`(全仓最常见 import)都非纯导入,且与 pytest 流捕获打架。
- **现在**:包导入**零副作用**;修复函数化为 `hermes_cli.ensure_utf8_stdio()`(幂等、流已是 UTF-8 时 no-op),由**进程入口点**显式调用:`cli.py`(模块初始化早段)、`hermes_cli/main.py`、`gateway/run.py::main`、`run_agent.py::main`(hermes-agent console script 不经前两者,必须自调)、`hermes doctor`;`hermes_cli/stdio.py` re-export 两个修复为单 import(照顾延迟 import 棘轮)。
- **兼容性**:自行嵌入 Hermes、依赖旧 import 副作用的宿主,需在自己的入口显式调 `ensure_utf8_stdio()`。测试:`tests/run_agent/test_entrypoint_stdio_repair.py`(新)。

### 3.3 配置双轨收敛(H4,就地弃用)

`cli.py` 的 `CLI_CONFIG` 标注 **DEPRECATED(in place)**:共享加载器已补齐 `HERMES_IGNORE_USER_CONFIG` 语义(config.py:7722,仅 load 路径——raw 读/保存仍看真实磁盘,防"带 flag 保存把配置写成默认值"),两加载器无行为差后,外部 importer 迁移到 `load_config[_readonly]()`;`tools/delegate_tool.py`(+27)已把 `CLI_CONFIG` 降为回退。收敛由 §3.1 的 convergence 测试钉死。

### 3.4 单进程全局的显式化(H5,containment)

`hermes_cli/web_server.py`(+25):新增 `DashboardRuntimeState` 类,把模块级可变全局(`session_token`、OAuth/PKCE 流表、`action_procs` Popen 句柄、reveal 限速表…)收编为**单一具名 owner**,逐项写明多 worker 部署会怎么坏(worker A 注入的 token 被 worker B 401;OAuth 回调路由到别的 worker 即死;限速预算 ×N)与各自的正确外置方案。**行为不变**——这是把隐性约束变成显性文档+聚合点,重写另案。

---

## 4. 性能

- **配置锁在 NFS/SMB 上的 10s 尾延迟消除**:`config_write_lock` 三态化(`_LOCK_ACQUIRED/_CONTENDED/_UNSUPPORTED`,config.py:7268-7299)——锁不受支持的文件系统按路径记忆 + 一次性进程级警告,直接无锁降级;旧行为把"不支持"当"争用"烧满 10s 超时,gateway 事件循环随之卡顿。
- **raw 配置读缓存**:`read_raw_config()` 按 (path, mtime_ns, size) 缓存,doctor/gateway/斜杠命令的高频 raw 读不再每次解析。
- **Bedrock 无锁快路径**(§2.2)与 **LSP 文档上界**(§2.3)分别消除首调用毛刺与长会话内存爬升。
- `tools/terminal_tool.py`(+17):工具描述改为 "persistent POSIX bash shell"(terminal_tool.py:968)——旧文案 "a Linux environment" 在 Windows/macOS 宿主上与环境提示自相矛盾,诱导模型发 Linux 特有命令徒耗轮次(提示词级修正,零运行时成本)。

---

## 5. 文档

- `docs/spec/`(新目录):`ARCHITECTURE.md`(344 行)、`API-HTTP.md`(457)、`API-EXTENSION.md`(532)、`SRS-features.md`(D/E 工具与平台能力矩阵及 K 记忆子系统、L agent 核心均已按源码补齐)、`OPERATIONS.md`(运维手册:部署/端口/配置总表/全量 HERMES_* 环境变量/安全模型/备份/排障)、本文件。
- `docs/architecture/layering.md`(新):分层目标 vs 实测、棘轮口径——`tests/architecture/baselines/` 不一致时以 baselines 为准。

---

## 6. 兼容性与逃生阀总表

| 变更 | 默认行为变化 | 逃生阀 / 迁移动作 |
|---|---|---|
| XFF 信任(§1.1) | 反代后限流键变为代理 IP(更严不更宽) | 配 `HERMES_TRUSTED_PROXIES=<代理IP/CIDR,…>`(或 config `dashboard.trusted_proxies`) |
| 写审批 digest(§1.4) | approve 缺 digest 一律拒 | 旧客户端过渡期 `HERMES_WRITE_APPROVAL_REQUIRE_DIGEST=0`(自担混淆代理风险,修完即撤) |
| BlueBubbles 空口令(§1.2) | 空口令从 fail-open 变 fail-closed | 给 BlueBubbles 服务器设口令并写入平台配置 |
| stdio 修复入口化(§3.2) | `import hermes_cli` 不再改流/环境 | 嵌入方在自己入口调 `hermes_cli.ensure_utf8_stdio()`;诊断编码问题 `HERMES_DISABLE_WINDOWS_UTF8=1` 退回 cp1252 |
| strict 配置读(§2.6/2.7) | 损坏 config.yaml:写路径从"静默重写"变"报错中止" | 无逃生阀(这是修复本体);按报错修 YAML;读路径 LKG 缓存与 `read_raw_config` 降级 `{}`+告警不变 |
| 媒体基名黑名单(§1.8) | 项目 `.env`/私钥文件不可作为附件投递 | 无(agent 可引用非秘密行为文本);Keynote `.key` 专门放行 |
| Feishu 重放窗(§1.6) | >1h 旧签名请求被拒 | 时钟偏差超 1h 的部署先校时(窗口值为源码常量) |
| mem0 pgvector(§1.5) | 新装无固定口令 | 自管口令用 `HERMES_PGVECTOR_PASSWORD`;既有容器沿用已存文件 |
| 配置锁三态(§4) | NFS/SMB 首写打一条警告 | 无需动作;警告仅一次 |
| Windows compose(§1.10) | dashboard 需先配 auth 才能起 | 见 `docker-compose.windows.yml` 注释(password_hash 生成命令) |

**新增测试清单**(本次快照内):`tests/architecture/`(6 文件+archlint+baselines)、`test_client_ip_trusted_proxy.py`、`test_account_write_approval_binding.py`、`test_read_raw_config_strict.py`、`test_file_safety_case_folding.py`、`test_client_docs_bounds.py`(lsp)、`test_utils_secret_write.py`、`test_media_download_retry.py`、`test_entrypoint_stdio_repair.py`;显著扩充:`test_platform_base.py`(+225)、`test_config.py`(TestSaveConfigPartialWritePreservation)、`test_feishu.py`(+60)、`test_bluebubbles.py`(+55)、`test_dashboard_auth_password_login.py`(+53)、`test_mem0_setup.py`(+60)、`test_collaboration_dashboard.py`、`test_approved_command_clean_slate.py`、`test_delegate.py`、`test_auxiliary_client.py`。

**审计发现处置对照**:C1(§3.1 棘轮圈住,重构未做)、C2(iOS 仓,不在本仓快照)、H1(§1.1 已修)、H2(§1.9 比较统一 + 路由 split-brain 仍在)、H3(§3.2 最尖锐点已修,181k 行 God 包仍在)、H4(§3.3 语义收敛,双加载器仍并存)、H5(§3.4 containment)、H6(§2.6 散点收口,允许清单冻结)、H7(§2.1 已修)、H8(iOS 仓);M/L 项处置见各节。未处置项以 findings 分文件为准。

---

## 7. iOS Studio 记忆契约补齐

- 新增 `GET /api/hermes/memory` 与 `PUT /api/hermes/memory`，按严格校验后的 Profile 读取和更新 `memories/MEMORY.md`、`SOUL.md`、`memories/USER.md`，并返回三份内容及各自真实修改时间。
- PUT 仅接受 `memory`、`soul`、`user` 三个 section；写入统一经过 `write_secret_file(..., mode=0o600)` 的原子替换边界。替换失败时保留旧内容，不留下截断文件。
- 旧的 `PUT /api/profiles/{name}/soul` 同步收口到相同原子写原语，避免新旧入口产生不同的数据安全语义。
- `tests/hermes_cli/test_studio_memory_endpoint.py` 覆盖空 Profile、三类内容、跨 Profile 隔离、未知/穿越 Profile、非法 section、原子失败保留和会话鉴权；移动 bearer 真实中间件测试同时验证该端点可由登录后的 iOS 客户端访问。
- `tests/plugins/test_collaboration_ios_contract.py` 固化 iOS 文件查询 `type` 别名、显式 `file_type` 优先级，以及附件消息/Profile/turn 标识的跨仓库持久化契约。

---

## 8. 最终复验增量

- 遗留 CLI/gateway 写审批入口移除不受保护的直接应用回退。`prepare_write_approval` 或收敛检查异常时，审批保持 pending 并返回失败，不再执行陈旧 payload；`test_legacy_approval_fails_closed_when_convergence_check_errors` 固化该行为。
- Windows 测试隔离补齐：Feishu 的清空环境测试保留独立 `HERMES_HOME`，配置测试以 UTF-8 读取，并仅在宿主缺少 POSIX `env/sh` 时跳过额外 shell-source 验证。产品语义不因宿主平台变化。
- 复验结果：后端改动面 `1,664` 项 pytest 通过，另有 `49` 个 subtests 通过、`15` 项按可选依赖/平台能力跳过；架构门禁 `30/30` 通过；改动模块 `compileall` 通过。各分组互不重叠，单独的故障定位重跑未重复计入总数。
- 架构结论保持克制：C1/H2/H3/H5 是受棘轮、接线检查和兼容层约束的长期重构项，不应标记为“彻底消除”。

---

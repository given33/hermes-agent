# Hermes Agent 扩展点与扩展接口文档（API-EXTENSION）

**状态**：描述性文档，基于 2026-07-26 的源码逐点核对。所有签名、行号、字段名均取自实际代码；与代码不一致时以代码为准。
**配套文档**：[`ARCHITECTURE.md`](ARCHITECTURE.md)（整体架构）、[`API-HTTP.md`](API-HTTP.md)(HTTP/WS 接口)、[`../architecture/layering.md`](../architecture/layering.md)（分层与棘轮）。

本文覆盖六大一等扩展点 + 一组次级注册项：

| 扩展点 | 核心契约文件 | 注册入口 | 发现机制 |
|---|---|---|---|
| 工具（Tool） | `tools/registry.py` | `registry.register(...)` | AST 扫描 `tools/*.py` 顶层 `registry.register(` 调用 |
| 插件（Plugin）与 Hook | `hermes_cli/plugins.py` | `plugin.yaml` + `def register(ctx)` | bundled → user → project → pip entry-points |
| 平台适配器（Platform Adapter） | `gateway/platforms/base.py` | `ctx.register_platform(...)` 或内置 if/elif | `gateway/platform_registry.py` 延迟加载器 |
| 模型 Provider Profile | `providers/base.py` | `providers.register_provider(profile)` | bundled `plugins/model-providers/` → user 目录 → legacy `providers/*.py` |
| 记忆后端（Memory Provider） | `agent/memory_provider.py` | `ctx.register_memory_provider(provider)` | `plugins/memory/` 下 `kind: exclusive` 插件 |
| Dashboard 插件（前端 + API） | `hermes_cli/web_server.py::_mount_plugin_api_routes` | `dashboard/manifest.json` 文件系统约定 | 扫描 `~/.hermes/plugins/*/dashboard/manifest.json` 与仓库 `plugins/*/dashboard/manifest.json` |

在动手扩展之前，先读 `AGENTS.md` 中的 **Footprint Ladder**（足迹阶梯）：优先级从低到高依次是「扩展现有工具 → CLI 命令 + skill → `check_fn` 门控工具 → 插件 → MCP catalog → 核心工具」。默认落点是插件；只有确有必要时才向核心加代码。

---

## 1. 工具（Tools）

### 1.1 注册函数签名

`tools/registry.py` L365-379：

```python
def register(
    name: str,
    toolset: str,
    schema: dict,
    handler: Callable,
    check_fn: Optional[Callable[[], bool]] = None,
    requires_env: Optional[List[str]] = None,
    is_async: bool = False,
    description: str = "",
    emoji: str = "",
    max_result_size_chars: Optional[int] = None,
    dynamic_schema_overrides: Optional[Callable] = None,
    override: bool = False,
) -> None: ...
```

要点（均已核对源码）：

- `schema` 为发送给模型的 JSON Schema 工具定义（OpenAI function-calling 格式）。
- `check_fn`：无参函数，返回 bool 决定工具此刻是否可用。结果**缓存 30 秒**，失败后有 60 秒宽限（避免瞬时探测失败导致工具闪断）。
- `override=True` 允许覆盖已注册的同名工具，但受 `plugins.entries.<plugin_id>.allow_tool_override` 配置门控，未授权时抛 `PermissionError`。
- **没有** `init_fn` / `cleanup` 参数——工具生命周期钩子不存在，初始化逻辑应放在 handler 内部惰性执行。
- `dispatch()` 会把 handler 抛出的异常包装成 `{"error": ...}` 返回给模型，不会让异常穿透会话循环。
- 辅助函数 `tool_error(msg)` / `tool_result(...)` 用于构造规范返回值。

### 1.2 发现与暴露：两个独立条件

一个工具要真正出现在模型面前，需要同时满足：

1. **被发现**：`discover_builtin_tools()` 用 AST 扫描 `tools/*.py`（排除 `__init__.py`、`registry.py`、`mcp_tool.py`），寻找**模块顶层**的 `registry.register(` 调用并 import 该模块。实际 import 发生在 `tools/model_tools.py:188`。因此注册调用必须写在模块顶层，不能藏在函数里。
2. **被工具集收录**：工具名必须出现在当前解析出的 toolset 中。toolset 定义在 `tools/toolsets.py` 的 `TOOLSETS` 字典（每项含 `description` / `tools` / `includes`），由 `resolve_toolset()` 解析；`_HERMES_CORE_TOOLS`（L31-84）是永远存在的核心工具名单。

这就是 AGENTS.md 所说的「注册双文件模式」：新工具通常要改 `tools/<your_tool>.py`（注册）和 `tools/toolsets.py`（收录）两个文件。

### 1.3 最小可行示例（改编自 `tools/close_terminal_tool.py`，树内真实注册）

```python
# tools/my_tool.py
from tools import registry
from tools.registry import tool_error, tool_result

MY_SCHEMA = {
    "name": "my_tool",
    "description": "一句话说明模型何时应调用它。",
    "parameters": {
        "type": "object",
        "properties": {"target": {"type": "string"}},
        "required": ["target"],
    },
}

def _handle(args: dict, **kwargs) -> dict:
    target = args.get("target", "")
    if not target:
        return tool_error("target is required")
    return tool_result(f"done: {target}")

def _available() -> bool:          # check_fn：仅在环境就绪时暴露
    return True

registry.register(
    name="my_tool",
    toolset="terminal",            # 或在 toolsets.py 新建工具集
    schema=MY_SCHEMA,
    handler=_handle,
    check_fn=_available,
    emoji="🔧",
)
```

然后在 `tools/toolsets.py` 把 `"my_tool"` 加进目标 toolset 的 `tools` 列表。

**插件路径的替代方案**：不改核心目录，用 `ctx.register_tool(...)`（见 §2.4）在插件里注册，效果等同，且符合 Footprint Ladder 的推荐落点。

---

## 2. 插件系统（Plugins）

### 2.1 plugin.yaml 与 PluginManifest

`hermes_cli/plugins.py`（2,466 行）。Manifest 字段：`name`、`version`、`description`、`author`、`requires_env`、`provides_tools`、`provides_hooks`、`source`、`path`、`kind`（默认 `"standalone"`）、`key`。

`kind` 合法值（`_VALID_PLUGIN_KINDS`，L135 附近）：

| kind | 语义 | 加载策略 |
|---|---|---|
| `standalone` | 普通插件 | 需用户在 `plugins.enabled` 显式开启（`None` = 什么都不启用；`plugins.disabled` 优先生效） |
| `backend` | 后端基础设施插件 | **bundled（仓库自带）时自动加载** |
| `exclusive` | 互斥类（记忆后端） | 走 memory 发现流程，经 `memory.provider` 选择 |
| `platform` | 聊天平台适配器 | bundled 时注册为 `platform_registry` 延迟加载器，配置该平台后才真正 import |
| `model-provider` | 模型供应商 profile | 走 providers 发现流程（§4） |

kind 会在缺失时通过嗅探 `__init__.py` 内容自动纠正（auto-coercion）。

### 2.2 发现顺序与安全门

1. 仓库自带 `plugins/`（跳过 `memory/`、`context_engine/`、`platforms/`、`model-providers/` 这些作为分类目录的子树——它们各有专属发现流程）；
2. 用户目录 `~/.hermes/plugins/`；
3. 项目目录 `./.hermes/plugins/`——**默认不加载**，须设 `HERMES_ENABLE_PROJECT_PLUGINS`（对应 GHSA-5qr3-c538-wm9j：克隆仓库不应能注入代码）；
4. pip entry points，group 名 `hermes_agent.plugins`。

`HERMES_SAFE_MODE` 跳过全部插件。支持扁平与两级分类目录布局。插件模块以 `hermes_plugins.<slug>` 名义 import。

### 2.3 register(ctx) 与 PluginContext 全量 API

插件入口是模块级 `def register(ctx):`。`PluginContext` 提供的注册方法（行号为 `hermes_cli/plugins.py` 中定义处）：

| 方法 | 行号 | 用途 |
|---|---|---|
| `ctx.llm`（property） | 351 | 取辅助 LLM 客户端 |
| `ctx.profile_name` | 370 | 当前 profile 名 |
| `ctx.register_tool(...)` | 391 | 注册工具（转发到 `tools/registry.py`） |
| `ctx.inject_message(...)` | 476 | 向会话注入消息 |
| `ctx.register_cli_command(...)` | 504 | 注册 `hermes <cmd>` CLI 子命令 |
| `ctx.register_command(...)` | 529 | 注册 `/xxx` 斜杠命令 |
| `ctx.dispatch_tool(...)` | 585 | 以编程方式调用已注册工具 |
| `ctx.register_context_engine(...)` | 616 | 注册上下文引擎（§7.1） |
| `ctx.register_image_gen_provider(...)` | 648 | 注册图像生成 provider（§7.2） |
| `ctx.register_dashboard_auth_provider(...)` | 675 | 注册 Dashboard 认证 provider（§7.3）；**这是插件面向 dashboard 的唯一注册钩子，不存在通用的 dashboard 路由注册 API**（路由挂载走 §6 的文件系统约定） |
| `ctx.register_video_gen_provider(...)` | 715 | 视频生成 provider |
| `ctx.register_web_search_provider(...)` | 742 | Web 搜索 provider |
| `ctx.register_browser_provider(...)` | 770 | 浏览器后端 provider |
| `ctx.register_secret_source(...)` | 802 | 密钥来源 |
| `ctx.register_tts_provider(...)` | 849 | TTS provider |
| `ctx.register_transcription_provider(...)` | 887 | 语音转写 provider |
| `ctx.register_platform(...)` | 931 | 注册聊天平台（§3.4） |
| `ctx.register_slack_action_handler(...)` | 987 | Slack 交互动作处理器 |
| `ctx.register_auxiliary_task(...)` | 1047 | 辅助模型任务位 |
| `ctx.register_hook(...)` | 1158 | 注册 Hook（§2.5） |
| `ctx.register_middleware(...)` | 1177 | 注册中间件（§2.6） |
| `ctx.register_skill(...)` | 1198 | 注册 skill |

记忆插件（`kind: exclusive`）通过 `_ProviderCollector` 垫片额外获得 `register_memory_provider`（§5.3）。

### 2.4 最小可行插件（改编自树内 `plugins/disk-cleanup/`）

```
~/.hermes/plugins/my-plugin/
├── plugin.yaml
└── __init__.py
```

```yaml
# plugin.yaml
name: my-plugin
version: "0.1.0"
description: 示例插件
kind: standalone
```

```python
# __init__.py
def register(ctx):
    ctx.register_hook("post_tool_call", _after_tool)
    ctx.register_hook("on_session_end", _on_end)
    ctx.register_command("my-cmd", handler=_slash, description="示例斜杠命令")

def _after_tool(**kwargs): ...
def _on_end(**kwargs): ...
def _slash(args, **kwargs): return "ok"
```

启用：在 `config.yaml` 的 `plugins.enabled` 列表中加入 `my-plugin`（standalone 插件不启用就不会加载）。

### 2.5 Hook 目录（VALID_HOOKS，`hermes_cli/plugins.py` L135-215）

| Hook | 触发时机 | 返回值是否被采纳 |
|---|---|---|
| `pre_tool_call` / `post_tool_call` | 每次工具调用前/后 | 否（观察者） |
| `transform_terminal_output` | 终端输出入上下文前 | 是（变换器） |
| `transform_tool_result` | 工具结果入上下文前 | 是（变换器） |
| `transform_llm_output` | 模型输出展示前 | 是（变换器） |
| `pre_llm_call` | 模型调用前 | 是：返回 `{"context": ...}` 会注入到用户消息 |
| `post_llm_call` | 模型调用后 | 否 |
| `pre_verify` | 验证步骤前 | 是：`{"action", "message"}` |
| `pre_api_request` | Provider HTTP 请求发出前（观察者） | **否**（见 §2.7） |
| `post_api_request` | Provider HTTP 响应后（观察者） | 否 |
| `api_request_error` | Provider 请求异常 | 否 |
| `on_session_start` / `on_session_end` / `on_session_finalize` / `on_session_reset` | 会话生命周期 | 否 |
| `subagent_start` / `subagent_stop` | 子代理生命周期 | 否 |
| `pre_gateway_dispatch` | gateway 消息分发前 | 是：`{"action": "skip"|"rewrite"|"allow"}` |
| `pre_approval_request` / `post_approval_response` | 危险命令审批前/后 | 否 |
| `kanban_task_claimed` / `kanban_task_completed` / `kanban_task_blocked` | 看板任务事件 | 否 |

调用侧 `invoke_hook` 对每个回调单独 try/except——**一个坏插件不能拖垮主流程**；遥测 schema 版本 `hermes.observer.v1`。

### 2.6 中间件（Middleware）

`hermes_cli/middleware.py`：`VALID_MIDDLEWARE = {tool_request, tool_execution, llm_request, llm_execution}`，且 L26-27：

```python
API_REQUEST_MIDDLEWARE = LLM_REQUEST_MIDDLEWARE
```

即 `ctx.register_middleware("api_request", cb)` 是 `llm_request` 的**别名**。与 Hook 不同，中间件可以**改写请求**：`apply_llm_request_middleware` 采纳回调返回的 `{"request": {...}}` 来替换 provider 调用参数。执行位置在 `agent/conversation_loop.py` 约 L1360-1377，紧接其后才触发 `pre_api_request` 观察者钩子。

### 2.7 `pre_api_request` / `api_request` 语义澄清（易混淆点）

仓库中**不存在字面名为 `api_request` 的 Hook**。两条机制并存：

1. **观察者 Hook**（返回值被忽略，异常仅记录日志——树内注释：*"a broken plugin must never break the API call"*）：
   - `pre_api_request`（`agent/conversation_loop.py:1379-1441`）：kwargs 含 `task_id`、`turn_id`、`api_request_id`、`session_id`、`conversation_history`、`request_messages`、`approx_input_tokens`、`middleware_trace`、`request`（脱敏后的请求负载）。
   - `post_api_request`（`conversation_loop.py:4583-4629`）：含 `response`、`usage`、`finish_reason`、`api_duration`。
   - `api_request_error`：由 `agent/run_agent.py:2587` 触发。
2. **中间件别名**：`ctx.register_middleware("api_request", cb)` = `llm_request` 中间件，**可以**改写 provider kwargs（§2.6）。

想「观察」请求用 Hook；想「改写」请求用 middleware。

---

## 3. 平台适配器（Platform Adapters）

### 3.1 基类契约

`gateway/platforms/base.py`（6,026 行）。`BasePlatformAdapter(ABC)` 定义于 L2410，构造函数 `__init__(config: PlatformConfig, platform: Platform)`（L2531）。

**恰好四个 @abstractmethod**：

| 方法 | 行号 | 契约 |
|---|---|---|
| `async connect(*, is_reconnect: bool = False) -> bool` | 3076 | 建立连接；返回是否成功 |
| `async disconnect()` | 3096 | 断开并清理 |
| `async send(chat_id, content, reply_to=None, metadata=None) -> SendResult` | 3101 | 发送消息 |
| `async get_chat_info(chat_id)` | 5854 | 查询会话元信息 |

入站消息路径：gateway 调用 `adapter.set_message_handler(handler)`（L2981），适配器收到平台消息后构造 `MessageEvent` 并 `await self._message_handler(event)`。

`MessageEvent`（L1851）字段：`text`、`message_type`、`source`、`raw_message`、`message_id`、`platform_update_id`、`media_urls/types`、`reply_to_*`、`auto_skill`、`channel_prompt/context`、`internal`、`metadata`、`timestamp`。
`SendResult`（L1990）字段：`success`、`message_id`、`error`、`raw_response`、`retryable`、`retry_after`、`continuation_message_ids`、`error_kind`。

### 3.2 能力声明与可选方法

能力属性（类属性，按需覆写）：`supports_code_blocks`、`supports_status_text`、`supports_async_delivery`、`splits_long_messages`、`typed_command_prefix`、`supports_inchannel_continuable`、`interactive_resume`、`gateway_runner`、`REQUIRES_EDIT_FINALIZE`。

可选方法（存在即被调用）：`edit_message(finalize=)`、`delete_message`、`create_handoff_thread`、`format_message`、`truncate_message`、`set_status_text`、`enforces_own_access_policy`、`authorization_is_upstream`、`supports_draft_streaming`、`build_source`。

### 3.3 HTTP 事件回调对：`verify_http_event_request` / `dispatch_http_event`

这是一对**鸭子类型**（duck-typed，`getattr` 探测）的可选方法，不在 ABC 中。调用方是 `gateway/platforms/api_server.py::_handle_platform_event_callback`（L1304-1391），路由 `/platform/<name>`：

- 适配器缺任一方法 → 503；
- `verify_http_event_request` 收到原始 `Authorization` 头（同步实现会被 `to_thread` 包装），必须返回 `(ok: bool, code: str)`；**抛异常按验证失败处理（fail closed）→ 401**；
- 验证通过后 `result = await dispatch_http_event(payload_dict)` → 作为 JSON 响应返回。

树内实现范例 `plugins/platforms/google_chat/adapter.py`：

- `verify_http_event_request`（L1520）：解析 Bearer → `_verify_google_id_token`（校验 audience 与 service-account email claim）；
- `dispatch_http_event`(L1495)：跳过 BOT 发送者、去重、`_dispatch_message`、返回 `{}`。

适用场景：平台以 HTTP 回调（而非长连接）推送事件，且 gateway 已开启 api_server 平台时，可复用其端口而不必自建 aiohttp 监听。

### 3.4 注册路径（推荐：插件）

`gateway/run.py::_create_adapter`（L9742）的决策顺序：**先查 `gateway.platform_registry.platform_registry.is_registered(platform.value)`**，命中则 `create_adapter()`（并设 `adapter.gateway_runner = self`）；否则落入内置 if/elif 链。

**插件路径**（`gateway/platforms/ADDING_A_PLATFORM.md` 推荐）：

```
plugins/platforms/my_chat/
├── plugin.yaml          # kind: platform
└── adapter.py
```

```python
# plugin.yaml → kind: platform；bundled 时被注册为延迟加载器，
# 用户配置该平台后才真正 import adapter 模块。
def register(ctx):
    ctx.register_platform(
        name="my_chat",
        label="My Chat",
        adapter_factory=lambda config, platform: MyChatAdapter(config, platform),
        required_env=["MY_CHAT_TOKEN"],
        check_fn=lambda: bool(os.environ.get("MY_CHAT_TOKEN")),
        emoji="💬",
    )
```

`PlatformEntry` 可携带的完整字段：`name`、`label`、`adapter_factory`、`check_fn`、`validate_config`、`is_connected`、`required_env`、`install_hint`、`setup_fn`、`source`、`plugin_name`、`allowed_users_env`、`allow_all_env`、`max_message_length`、`pii_safe`、`emoji`、`allow_update_command`、`platform_hint`、`env_enablement_fn`、`apply_yaml_config_fn`、`cron_deliver_env_var`、`standalone_sender_fn`。注册表支持 `register_deferred(name, loader)` 延迟加载，同名**后注册者胜**（last-writer-wins）。

内置路径（不推荐）需按 ADDING_A_PLATFORM.md 走完 16 步（枚举、配置解析、`_create_adapter` 分支、doctor、dashboard 目录等）。

### 3.5 适配器最小骨架

```python
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult

class MyChatAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # 建连 / 起轮询任务；入站消息:
        # await self._message_handler(MessageEvent(text=..., source=..., ...))
        return True

    async def disconnect(self) -> None: ...

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="...")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}
```

---

## 4. 模型 Provider Profile

### 4.1 契约

`providers/base.py`：`ProviderProfile`（L38；哨兵 `OMIT_TEMPERATURE` 在 L21）。字段与默认值：

```python
name: str
api_mode: str = "chat_completions"      # 或 "codex_responses" 等
aliases: tuple = ()
display_name: str = ""
description: str = ""
signup_url: str = ""
env_vars: tuple = ()                     # API key 所在环境变量
base_url: str = ""
models_url: str = ""
auth_type: str = "api_key"               # api_key | oauth_device_code | oauth_external | copilot | aws_sdk
supports_health_check: bool = True
supports_vision: bool = False
supports_vision_tool_messages: bool = True
fallback_models: tuple = ()
hostname: str = ""
default_headers: dict = {}
fixed_temperature = None                 # OMIT_TEMPERATURE 表示"不发送该字段"
default_max_tokens = None
default_aux_model: str = ""
```

钩子方法（按需覆写）：`get_hostname`、`prepare_messages`、`build_extra_body`、`build_api_kwargs_extras`、`default_vision_model`、`get_max_tokens`、`fetch_models`。

### 4.2 注册与发现

`providers/__init__.py`：

- `register_provider(profile)`（L53）：写入注册表，**别名一并注册，同名后写者胜**；
- `get_provider_profile(name)`（L65）触发惰性 `_discover_providers()`（L140），顺序：
  1. 仓库自带 `plugins/model-providers/<name>/`（import 为 `plugins.model_providers.<safe>`）；
  2. 用户 `$HERMES_HOME/plugins/model-providers/`（import 为 `_hermes_user_provider_<safe>`）;
  3. legacy `providers/<name>.py`。

### 4.3 树内真实示例（`plugins/model-providers/xai/__init__.py`，逐字）

```python
from providers import register_provider
from providers.base import ProviderProfile

register_provider(ProviderProfile(
    name="xai",
    aliases=("grok", "x-ai", "x.ai"),
    api_mode="codex_responses",
    env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1",
    ...
))
```

新增供应商的最小路径：建 `~/.hermes/plugins/model-providers/<name>/__init__.py`，模块顶层调用 `register_provider(ProviderProfile(...))` 即可，无需改核心代码。

---

## 5. 记忆后端（Memory Providers）

### 5.1 MemoryProvider ABC

`agent/memory_provider.py`（316 行）。

**必须实现**（@abstractmethod）：

| 成员 | 行号 | 契约 |
|---|---|---|
| `name`（property） | 47 | 唯一标识 |
| `is_available()` | 54 | **禁止网络调用**，只做本地快速检查 |
| `initialize(session_id, **kwargs)` | 62 | kwargs 含 `hermes_home`、`platform`、`agent_context`、`user_id` 等 |
| `get_tool_schemas()` | 135 | 返回该后端向模型暴露的工具 schema 列表 |

**有默认实现，可覆写**：`system_prompt_block()`(85)、`prefetch()`(94)、`queue_prefetch()`(108)、`sync_turn(user_content, assistant_content, *, session_id="", messages=None)`(116)、`handle_tool_call()`(144)、`shutdown()`(152)。

**可选生命周期钩子**：`on_turn_start`(157)、`on_session_end`(166)、`on_session_switch`(176)、`on_pre_compress`(220)、`on_delegation`(232)、`get_config_schema`(245)、`save_config`(263)、`on_memory_write`(280)、`backup_paths`(299)。

**注意**：`post_setup` **不在 ABC 中**——它是鸭子类型钩子，仅被 `hermes_cli/memory_setup.py:225-227/281-283` 调用（provider 配置保存后的安装动作）。

### 5.2 MemoryManager 的强制规则

`agent/memory_manager.py`（`MemoryManager` L354）：

- builtin provider 永远第一；**最多一个外部 provider**（`add_provider` L394 拒绝第二个）；
- 外部 provider 的工具**不得遮蔽核心工具名**（`_HERMES_CORE_TOOLS` 校验，L420-444）；
- `prefetch_all` 对外部 provider 挂看门狗超时；
- `sync_all` 在**单 worker 后台执行器**上串行执行（保证 turn 顺序；树内注释记录过一个 provider 卡死曾阻塞 ~298s 的事故）；`messages` kwarg 仅在 provider 签名声明接受时传入；
- `shutdown_all` 先排空执行器，再逆序 shutdown。

### 5.3 打包与选择

记忆后端以 `plugins/memory/<name>/` 插件形式发布，`plugin.yaml` 写 `kind: exclusive`；激活由 `config.yaml` 的 `memory.provider` 决定（同类互斥）。加载优先走 `register(ctx)`（ctx 是 `_ProviderCollector` 垫片），否则回退扫描模块内 `MemoryProvider` 子类。`register_cli(subparser)` 只对**当前激活**的 provider 调用（挂 `hermes memory <provider> ...` 子命令）。

树内自带后端：`byterover`、`hindsight`、`holographic`、`honcho`、`mem0`、`openviking`、`retaindb`、`supermemory`。

### 5.4 最小骨架（改编自 `plugins/memory/holographic/`）

```python
# plugins/memory/my_mem/__init__.py    (plugin.yaml: kind: exclusive)
from agent.memory_provider import MemoryProvider

class MyMemoryProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "my_mem"

    def is_available(self) -> bool:
        return True                      # 本地检查，勿发网络请求

    def initialize(self, session_id, **kwargs) -> None: ...

    def get_tool_schemas(self) -> list[dict]:
        return []                        # 或返回 memory_search 等工具

def register(ctx):
    ctx.register_memory_provider(MyMemoryProvider())
```

激活：`config.yaml` → `memory.provider: my_mem`。

---

## 6. Dashboard 插件（前端 Tab + 后端 API）

这是文件系统约定，**不是** PluginContext 钩子（`hermes_cli/plugins.py` 中不存在 `register_dashboard_api`）。

### 6.1 约定结构

```
<plugin_dir>/dashboard/
├── manifest.json     # 必须
├── dist/index.js     # 前端入口（manifest.entry 指向）
└── plugin_api.py     # 可选，manifest 的 "api" 键指向；必须暴露模块级 `router` (APIRouter)
```

manifest 示例（树内 `plugins/collaboration/dashboard/manifest.json` 节选）：

```json
{
  "name": "collaboration",
  "label": "群聊与工作流",
  "tab": {"path": "/collaboration", "position": "after:chat", "hidden": true},
  "slots": ["chat:top"],
  "entry": "dist/index.js",
  "api": "plugin_api.py"
}
```

### 6.2 挂载机制与安全门（`hermes_cli/web_server.py`）

- `_discover_dashboard_plugins`（L19130）扫描 `~/.hermes/plugins/*/dashboard/manifest.json` 与仓库 `plugins/*/dashboard/manifest.json`；
- `_mount_plugin_api_routes()`（L19652）把 `api` 文件 import 为 `hermes_dashboard_plugin_<name>` 模块（先注册进 `sys.modules` 再 `exec_module`，以支持 pydantic 前向引用），取其 `router`，`app.include_router(router, prefix=f"/api/plugins/{name}")`（L19760）；
- 安全边界：
  - 项目级（`./.hermes/plugins/`）插件的 API **永不自动 import**（GHSA-5qr3-c538-wm9j，#29156）;
  - 用户级插件必须在 `plugins.enabled` 允许列表中（GHSA-mcfc-hp25-cjv7，#46435）;
  - `api` 路径经 `_safe_plugin_api_relpath`（L19093）做穿越防护，挂载前再次复核路径包含关系；
  - 运行时中间件 `_plugin_api_runtime_gate` 对已禁用插件的路由返回 404，且**在认证之后**执行，防止未认证探测已装插件名（见 API-HTTP.md §2.2）。
- 认证叠加：插件可用 `ctx.register_dashboard_auth_provider` + `register_optional_token_prefix("/api/plugins/<name>/<subtree>", required_scope=...)` 为自己的子树开辟 Bearer token 认证通道——collaboration 插件的 `/connector` 子树即此模式（`plugin_api.py` L466/L535）。

---

## 7. 次级扩展点速览

### 7.1 上下文引擎（Context Engine）

`agent/context_engine.py`（263 行）。`ContextEngine` ABC：抽象成员 `name`(62)、`update_from_response(usage)`(95)、`should_compress()`(107)、`compress(messages, current_tokens, focus_topic, force, memory_context)`(111)。`run_agent` 会读取属性 `last_prompt_tokens`、`threshold_tokens`、`context_length`、`compression_count`。同一时刻仅一个引擎生效，由 `context.engine` 选择（默认 `"compressor"`）。注册：`ctx.register_context_engine(...)`。

### 7.2 图像生成 Provider

`agent/image_gen_provider.py`（393 行）：抽象 `name`(72) 与 `generate(prompt, aspect_ratio, *, image_url=None, reference_image_urls=None, **kwargs)`(165)；辅助 `success_response` / `error_response`。由 `image_gen.provider` 选择。注册：`ctx.register_image_gen_provider(...)`。

### 7.3 Dashboard 认证 Provider 与 Token Seam

`hermes_cli/dashboard_auth/base.py`：`DashboardAuthProvider` ABC，生命周期 `start_login → complete_login → verify_session(每请求) → refresh_session → revoke_session`；数据类 `Session` / `TokenPrincipal(principal, provider, scopes)` / `LoginStart`；异常映射 `ProviderError→503`、`InvalidCodeError→400`、`InvalidCredentialsError→401`（统一文案，*"never distinguishing 'unknown user' from 'wrong password'"*）、`RefreshExpiredError`。

注册（`hermes_cli/dashboard_auth/registry.py`）：`register_provider(provider)`（重名抛 ValueError）。纯 token 型 provider（`supports_session=False, supports_token=True`）配合 `hermes_cli/dashboard_auth/token_auth.py`：

- `register_token_route(path)`——精确路径完全交由 token seam 认证（例：drain 插件注册 `/api/gateway/drain`）;
- `register_optional_token_prefix(prefix, required_scope="dashboard:admin")`——前缀内允许 Bearer 认证，最长前缀的 scope 生效。

多 provider 按注册顺序尝试（stacking）；仅当无人接受且有人抛 `ProviderError` 时返回 503。全部秘密比较必须走仓库根的 `hermes_secret_compare.py`（常量时间、UTF-8、空值 fail-closed）。

### 7.4 其余注册项（一览）

Web 搜索 / 浏览器 / TTS / 转写 / 密钥源 / Slack 动作 / 辅助任务 / CLI 命令 / 斜杠命令 / skill——均为 `PluginContext` 上的对应 `register_*` 方法（§2.3 表），签名详见 `hermes_cli/plugins.py` 对应行号。斜杠命令的中央注册表在 `hermes_cli/commands.py` 的 `COMMAND_REGISTRY`。

---

## 8. 扩展点选型速查

| 需求 | 落点 |
|---|---|
| 给模型加一个能力 | 先考虑扩展现有工具；否则插件内 `ctx.register_tool`；核心工具是最后手段（Footprint Ladder） |
| 观察/改写模型请求 | 观察用 `pre_api_request` Hook；改写用 `register_middleware("api_request", ...)` |
| 接入新聊天平台 | `plugins/platforms/<name>/`，`kind: platform` + `ctx.register_platform`；HTTP 回调型平台实现 `verify_http_event_request`/`dispatch_http_event` 对 |
| 接入新模型供应商 | `plugins/model-providers/<name>/` + `register_provider(ProviderProfile(...))` |
| 换长期记忆后端 | `plugins/memory/<name>/`，`kind: exclusive` + `ctx.register_memory_provider`；用户以 `memory.provider` 切换 |
| 给 Dashboard 加页面/API | `dashboard/manifest.json` 约定（§6）；需要服务对服务认证时叠加 token seam |
| 换上下文压缩策略 | `ctx.register_context_engine` + `context.engine` |
| 自定义登录 | `ctx.register_dashboard_auth_provider`（会话型或 token 型） |

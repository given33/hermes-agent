# Hermes 副作用与外部边界清单
这份清单把 Cordis 论文的 revertible effect 约束映射到 Hermes。`revert` 只表示系统可以依据 acquisition witness 恢复自己独占管理的状态；对已经被外部观察的写入，必须使用 `compensation`、`idempotency`、`outbox` 或 `audit`，不能伪装成回滚。

| 资源/动作 | 典型位置 | 分类 | owner/scope | acquisition witness | 释放/补偿 | 长期运行风险 |
|---|---|---|---|---|---|---|
| plugin tool registration | `hermes_cli/plugins.py`, `tools/registry.py` | 可回滚 | plugin id / plugin load scope | plugin id、tool name、handler owner | 精确 deregister，拒绝 stale owner | reload 后旧 tool 仍可见 |
| plugin hook/middleware | `hermes_cli/plugins.py` | 可回滚 | plugin id / manager scope | callback identity、registration index | 移除精确 callback，不清空其他 plugin | 重复 hook、重复副作用 |
| lease/lock | hosted role、gateway、outbox | 可回滚但通常需 durable fence | turn/attempt/provider generation | lease id、owner、generation、expiry | release once；过期回收；stale writer reject | 进程死后僵尸 claim |
| stream/SSE/subscription | gateway、connector、MCP | 可回滚 | turn/provider/connection scope | connection id、cursor、generation | abort/close，等待 reader 结束 | 半开连接、回调写旧 turn |
| temporary workspace/file | tool execution、artifact builder | 可回滚 | turn/node/workspace scope | absolute path、creation digest、owner | 删除或归档，禁止删除新 owner 路径 | retry 覆盖新工作区 |
| hosted role claim | collaboration dashboard | 可回滚/状态补偿 | conversation/turn/role/attempt | claim token、execution owner、lease | claim release、terminal fence | remote/local 双重执行 |
| provider binding | model/MCP/connector/broker | 可回滚配置，调用本身不可回滚 | turn capability scope | provider id/version/generation | drain dependent 后 unload | 混合版本、旧引用 |
| event append | hosted event protocol | 不可删除的可观测 emission | account/conversation/turn | event id、cursor、idempotency key | append-only；幂等重放；audit | 误删导致客户端无法恢复 |
| user/external message | chat/channel/webhook | 不可逆 emission | account/turn/delivery | delivery id、external idempotency key | compensation message 或 provider-specific undo | 重复发送、无法证明撤回 |
| remote command/deploy/payment | external provider | 不可逆 emission | turn/approval/policy scope | request id、approval witness、provider response | provider-specific compensation/manual review | “取消成功”但外部已执行 |
| prompt cache/system prefix | Agent session | 会话内不可随意变更 | session/turn revision | prompt version、cache fingerprint | 只在压缩/恢复或新 revision 重建 | 动态能力注入破坏 cache |
| durable outbox | state store / mobile local store | 可重试、不可静默删除 | owner/account generation | delivery id、epoch、ack | retry/ack/dead-letter | 重复投递和旧账户污染 |

## 运行规则

1. 每个可回滚资源进入 `EffectScope`，disposer 必须由 acquisition 返回并绑定 witness。
2. Effect 按 scope 内 LIFO 释放；子 scope 完成前父 scope 不得标记 closed。
3. durable emission 不进入普通 undo 栈；它必须有 idempotency key、outbox 或补偿策略。
4. 所有异步完成回调携带 owner、turn/attempt 和 generation；过期回调只能记录并拒绝写入。
5. provider 下线分为 `draining` 和 `unloaded`；dependent teardown 完成前保留 committed binding。
6. 真实 sandbox 需要进程/VM/OS 隔离；EffectScope 和依赖注入本身不是安全沙箱。

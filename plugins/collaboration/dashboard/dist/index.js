(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!SDK || !registry) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useCallback, useEffect, useMemo, useRef, useState } = SDK.hooks;
  const collabApi = (path, options) =>
    SDK.fetchJSON("/api/plugins/collaboration" + path, options);
  const kanbanApi = (path, options) =>
    SDK.fetchJSON("/api/plugins/kanban" + path, options);

  function routeMode() {
    const value = new URLSearchParams(window.location.search).get("mode");
    return value === "workflow" ? "workflow" : "group";
  }

  function go(path) {
    window.location.assign(path);
  }

  function ModeSwitch({ active }) {
    return h(
      "div",
      { className: "hc-mode-switch", role: "tablist", "aria-label": "会话模式" },
      h(
        "button",
        {
          className: active === "single" ? "is-active" : "",
          onClick: () => go("/chat"),
        },
        h("span", { className: "hc-mode-dot" }),
        "单聊",
      ),
      h(
        "button",
        {
          className: active === "group" ? "is-active" : "",
          onClick: () => go("/collaboration?mode=group"),
        },
        h("span", { className: "hc-mode-dot" }),
        "群聊",
      ),
      h(
        "button",
        {
          className: active === "workflow" ? "is-active" : "",
          onClick: () => go("/collaboration?mode=workflow"),
        },
        h("span", { className: "hc-mode-dot" }),
        "工作流",
      ),
    );
  }

  let rpcSequence = 0;

  const STREAM_RECONNECT_MAX_ATTEMPTS = 12;
  const STREAM_RECONNECT_BASE_DELAY_MS = 600;
  const STREAM_RECONNECT_MAX_DELAY_MS = 8000;
  const STREAM_CONNECT_TIMEOUT_MS = 12000;
  const STREAM_TURN_TIMEOUT_MS = 30 * 60 * 1000;
  const STREAM_BACKGROUND_STALE_MS = 30000;

  function latestAssistantText(messages) {
    const latest = [...(messages || [])]
      .reverse()
      .find(
        (message) =>
          message.role === "assistant" && (message.text || message.content),
      );
    return latest?.text || latest?.content || "";
  }

  async function streamProfileTurn(
    profile,
    prompt,
    onEvent,
    existingStoredSessionId = "",
    sessionTitle = "",
  ) {
    return new Promise((resolve, reject) => {
      let socket = null;
      let sessionId = "";
      let storedSessionId = existingStoredSessionId;
      let completed = false;
      let submitted = false;
      let reconnectAttempts = 0;
      let reconnectTimer = null;
      let connectTimer = null;
      let connectionGeneration = 0;
      let rejectCurrentPending = null;
      let waitingForNetwork = false;
      let hiddenAt = document.hidden ? Date.now() : 0;

      const clearConnectTimer = () => {
        if (connectTimer) clearTimeout(connectTimer);
        connectTimer = null;
      };

      const request = (activeSocket, connectionPending, method, params) =>
        new Promise((requestResolve, requestReject) => {
          if (activeSocket.readyState !== WebSocket.OPEN) {
            const error = new Error("流式连接当前不可用");
            error.transient = true;
            requestReject(error);
            return;
          }
          const id = `hc-${++rpcSequence}`;
          connectionPending.set(id, {
            resolve: requestResolve,
            reject: requestReject,
          });
          try {
            activeSocket.send(
              JSON.stringify({ id, jsonrpc: "2.0", method, params }),
            );
          } catch (error) {
            connectionPending.delete(id);
            error.transient = true;
            requestReject(error);
          }
        });

      const closeCurrentSocket = () => {
        clearConnectTimer();
        if (rejectCurrentPending) {
          rejectCurrentPending("流式连接已关闭");
          rejectCurrentPending = null;
        }
        if (!socket) return;
        try {
          socket.close();
        } catch {
          /* already closed */
        }
        socket = null;
      };

      const cleanupConnectivityListeners = () => {
        window.removeEventListener("offline", handleOffline);
        window.removeEventListener("online", handleOnline);
        window.removeEventListener("pageshow", handlePageShow);
        document.removeEventListener("visibilitychange", handleVisibilityChange);
      };

      const fail = (error) => {
        if (completed) return;
        completed = true;
        if (!(error instanceof Error)) error = new Error(String(error));
        error.submitted = submitted;
        error.stored_session_id = storedSessionId;
        clearTimeout(turnTimeout);
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = null;
        cleanupConnectivityListeners();
        closeCurrentSocket();
        reject(error);
      };

      const finish = (result) => {
        if (completed) return;
        completed = true;
        clearTimeout(turnTimeout);
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = null;
        cleanupConnectivityListeners();
        closeCurrentSocket();
        resolve({
          ...result,
          session_id: sessionId,
          stored_session_id: storedSessionId,
        });
      };

      const turnTimeout = setTimeout(() => {
        fail(new Error(`${profile} 执行超时`));
      }, STREAM_TURN_TIMEOUT_MS);

      const scheduleReconnect = (reason, immediate = false) => {
        if (completed || reconnectTimer) return;
        if (navigator.onLine === false) {
          if (!waitingForNetwork) {
            waitingForNetwork = true;
            onEvent({
              type: "connection.waiting",
              payload: {
                reason: reason?.message || String(reason || "设备离线"),
              },
            });
          }
          return;
        }
        waitingForNetwork = false;
        if (reconnectAttempts >= STREAM_RECONNECT_MAX_ATTEMPTS) {
          fail(
            new Error(
              `${profile} 网络连接持续中断，已自动重试 ${STREAM_RECONNECT_MAX_ATTEMPTS} 次`,
            ),
          );
          return;
        }
        reconnectAttempts += 1;
        const backoff =
          STREAM_RECONNECT_BASE_DELAY_MS * 2 ** (reconnectAttempts - 1);
        const jitter = Math.floor(Math.random() * 250);
        const delay = immediate
          ? 0
          : Math.min(backoff + jitter, STREAM_RECONNECT_MAX_DELAY_MS);
        onEvent({
          type: "connection.reconnecting",
          payload: {
            attempt: reconnectAttempts,
            max_attempts: STREAM_RECONNECT_MAX_ATTEMPTS,
            delay_ms: delay,
            reason: reason?.message || String(reason || "连接已断开"),
          },
        });
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          void connect();
        }, delay);
      };

      const restartConnection = (reason) => {
        if (completed) return;
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = null;
        connectionGeneration += 1;
        clearConnectTimer();
        if (rejectCurrentPending) {
          rejectCurrentPending("网络状态已变化");
          rejectCurrentPending = null;
        }
        const activeSocket = socket;
        socket = null;
        if (activeSocket) {
          activeSocket.onerror = null;
          activeSocket.onclose = null;
          try {
            activeSocket.close();
          } catch {
            /* already closed */
          }
        }
        scheduleReconnect(reason, true);
      };

      const connect = async () => {
        if (completed) return;
        if (navigator.onLine === false) {
          scheduleReconnect(new Error("设备当前离线"));
          return;
        }
        const generation = ++connectionGeneration;
        let url;
        try {
          // Gated deployments use single-use tickets. Every retry must obtain
          // a fresh URL instead of reusing the ticket from the broken socket.
          url = await SDK.buildWsUrl("/api/ws");
        } catch (error) {
          scheduleReconnect(error);
          return;
        }
        if (completed || generation !== connectionGeneration) return;

        const activeSocket = new WebSocket(url);
        const connectionPending = new Map();
        socket = activeSocket;
        clearConnectTimer();
        connectTimer = setTimeout(() => {
          if (activeSocket.readyState === WebSocket.CONNECTING) {
            activeSocket.close();
          }
        }, STREAM_CONNECT_TIMEOUT_MS);

        const rejectPending = (message) => {
          const error = new Error(message);
          error.transient = true;
          for (const waiter of connectionPending.values()) {
            waiter.reject(error);
          }
          connectionPending.clear();
        };
        rejectCurrentPending = rejectPending;

        activeSocket.onopen = async () => {
          clearConnectTimer();
          let resumePayload = null;
          try {
          const createSession = async () => {
            const created = await request(activeSocket, connectionPending, "session.create", {
              cols: 100,
              close_on_disconnect: false,
              profile,
              source: "dashboard-unified",
              ...(sessionTitle ? { title: sessionTitle } : {}),
            });
            sessionId = created.session_id;
            storedSessionId =
              created.stored_session_id ||
              created.session_key ||
              created.session_id;
          };
          if (storedSessionId) {
            try {
              const resumed = await request(activeSocket, connectionPending, "session.resume", {
                cols: 100,
                close_on_disconnect: false,
                profile,
                session_id: storedSessionId,
                source: "dashboard-unified",
              });
              resumePayload = resumed;
              sessionId = resumed.session_id;
              storedSessionId =
                resumed.resumed || resumed.session_key || storedSessionId;
            } catch (error) {
              if (error.transient || submitted) throw error;
              storedSessionId = "";
            }
          }
          if (!sessionId) {
            await createSession();
          }

          if (!submitted) {
            await onEvent({
              type: "session.ready",
              payload: {
                session_id: sessionId,
                stored_session_id: storedSessionId,
              },
            });
          }

          if (submitted) {
            reconnectAttempts = 0;
            onEvent({ type: "connection.restored", payload: {} });
            if (
              resumePayload &&
              !resumePayload.running &&
              resumePayload.status !== "streaming"
            ) {
              finish({
                status: "completed",
                text: latestAssistantText(resumePayload.messages),
                recovered: true,
              });
            }
            return;
          }

          const submission = request(activeSocket, connectionPending, "prompt.submit", {
            session_id: sessionId,
            text: prompt,
          });
          // Mark before awaiting the RPC response. If the response packet is
          // lost after the server accepted the prompt, reconnect must resume
          // the turn rather than submitting the same task a second time.
          submitted = true;
          await submission;
        } catch (error) {
          if (completed || error?.transient) return;
          fail(error);
        }
      };

        activeSocket.onmessage = (messageEvent) => {
        let frame;
        try {
          frame = JSON.parse(messageEvent.data);
        } catch {
          return;
        }
        if (frame.id && connectionPending.has(frame.id)) {
          const waiter = connectionPending.get(frame.id);
          connectionPending.delete(frame.id);
          if (frame.error) waiter.reject(new Error(frame.error.message || "RPC 请求失败"));
          else waiter.resolve(frame.result || {});
          return;
        }
        if (frame.method !== "event") return;
        const event = frame.params || {};
        if (sessionId && event.session_id && event.session_id !== sessionId) return;
        onEvent(event);
        if (event.type === "message.complete") {
          finish(event.payload || {});
        } else if (event.type === "error") {
          finish({
            status: "error",
            text: event.payload?.message || "Hermes 执行失败",
          });
        }
      };

        // Safari fires onerror and then onclose for the same radio handoff.
        // onclose is the single reconnect authority so one drop schedules only
        // one retry.
        activeSocket.onerror = () => {};
        activeSocket.onclose = (event) => {
          clearConnectTimer();
          if (socket === activeSocket) socket = null;
          rejectPending("流式连接已断开");
          if (rejectCurrentPending === rejectPending) {
            rejectCurrentPending = null;
          }
          if (!completed) {
            scheduleReconnect(
              new Error(
                event.reason || `连接关闭（${event.code || "未知状态"}）`,
              ),
            );
          }
        };
      };

      function handleOffline() {
        restartConnection(new Error("设备离线"));
      }

      function handleOnline() {
        waitingForNetwork = false;
        restartConnection(new Error("网络已恢复"));
      }

      function handleVisibilityChange() {
        if (document.hidden) {
          hiddenAt = Date.now();
          return;
        }
        const backgroundDuration = hiddenAt ? Date.now() - hiddenAt : 0;
        hiddenAt = 0;
        if (
          backgroundDuration >= STREAM_BACKGROUND_STALE_MS ||
          (!socket && navigator.onLine !== false)
        ) {
          restartConnection(new Error("应用已回到前台"));
        }
      }

      function handlePageShow(event) {
        if (event.persisted || (!socket && navigator.onLine !== false)) {
          restartConnection(new Error("页面已恢复"));
        }
      }

      window.addEventListener("offline", handleOffline);
      window.addEventListener("online", handleOnline);
      window.addEventListener("pageshow", handlePageShow);
      document.addEventListener("visibilitychange", handleVisibilityChange);

      void connect();
    });
  }

  const ACTIVE_CONVERSATION_KEY = "hermes.unified.activeConversation";
  const PENDING_STORED_SESSION_KEY =
    "hermes.unified.pendingStoredSession";

  function loadRememberedConversationId() {
    try {
      const persistentId = window.localStorage.getItem(ACTIVE_CONVERSATION_KEY);
      const legacyId = window.sessionStorage.getItem(ACTIVE_CONVERSATION_KEY);
      const conversationId = persistentId || legacyId || "";
      if (!persistentId && legacyId) {
        window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, legacyId);
      }
      return conversationId;
    } catch {
      return "";
    }
  }

  function rememberConversationId(conversationId) {
    try {
      if (conversationId) {
        window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
        window.sessionStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
      } else {
        window.localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
        window.sessionStorage.removeItem(ACTIVE_CONVERSATION_KEY);
      }
    } catch {
      // Storage can be unavailable in private browsing; server history still works.
    }
  }

  function profileDisplayName(profile) {
    const names = {
      default: "Hermes",
      "dbb3-worker": "DBB3 执行器",
      "pc-worker": "本地执行器",
      reviewer: "Hermes 审阅器",
    };
    return names[profile] || profile || "Hermes";
  }

  function profileAvatar(profile, role) {
    if (role === "user") return "你";
    if (profile === "reviewer") return "R";
    if (profile === "pc-worker") return "PC";
    if (profile === "dbb3-worker") return "DB";
    return "H";
  }

  function buildContinuousPrompt(profile, nextMessage, history) {
    const recent = (history || [])
      .filter((message) => message.kind === "message" && message.content)
      .slice(-24)
      .map((message) => {
        const speaker =
          message.role === "user" ? "用户" : profileDisplayName(message.name);
        return `${speaker}: ${message.content}`;
      })
      .join("\n");
    if (!recent) return nextMessage;
    return [
      "你正在同一个 Hermes 对话会话中继续交流。",
      `当前 Profile：${profile}`,
      "请结合以下会话历史理解指代、上下文和用户偏好，不要把这条消息当成新会话。",
      "",
      "最近会话：",
      recent,
      "",
      `用户的新消息：${nextMessage}`,
    ].join("\n");
  }

  function hasHostedRuntimeRuns(conversation) {
    return Object.values(conversation?.runtime_runs || {}).some(
      (run) => run?.status === "running" && run?.session_id,
    );
  }

  function withHostedRuntimeMessages(conversation) {
    const messages = [...(conversation?.messages || [])];
    const represented = new Set(
      messages
        .map((message) => message.meta?.runtime_session_id)
        .filter(Boolean),
    );
    for (const [profile, run] of Object.entries(
      conversation?.runtime_runs || {},
    )) {
      if (
        run?.status !== "running" ||
        !run?.session_id ||
        represented.has(run.session_id)
      ) {
        continue;
      }
      messages.push({
        id: `hosted-${profile}-${run.session_id}`,
        role: "assistant",
        name: profile,
        content: "任务已由 DBB3 托管，关闭页面或锁屏不会中断执行。",
        status: "streaming",
        kind: "message",
        created_at: run.started_at || Date.now(),
        meta: {
          runtime_session_id: run.session_id,
          connection: {
            status: "hosted",
            text: "DBB3 服务端持续执行",
          },
        },
      });
    }
    return messages;
  }

  function openModelTools() {
    window.dispatchEvent(new CustomEvent("hermes:open-model-tools"));
  }

  function openNavigation() {
    window.dispatchEvent(new CustomEvent("hermes:open-navigation"));
  }

  function AttachmentList({ attachments = [] }) {
    if (!attachments.length) return null;
    return h(
      "div",
      { className: "hc-attachment-list" },
      attachments.map((attachment) => {
        const isImage = String(attachment.mime_type || "").startsWith("image/");
        return h(
          "a",
          {
            key: attachment.id || attachment.download_url,
            className: "hc-attachment-card",
            href: attachment.download_url,
            target: "_blank",
            rel: "noopener",
            download: attachment.name,
          },
          isImage
            ? h("img", {
                className: "hc-attachment-preview",
                src: attachment.download_url,
                alt: attachment.name,
              })
            : h("span", { className: "hc-attachment-file-icon" }, "FILE"),
          h(
            "span",
            { className: "hc-attachment-copy" },
            h("strong", null, attachment.name),
            h(
              "small",
              null,
              attachment.size
                ? `${Math.max(1, Math.round(attachment.size / 1024))} KB`
                : "下载文件",
            ),
          ),
        );
      }),
    );
  }

  function structuredText(value) {
    if (value == null) return "";
    if (typeof value === "string") return value.trim();
    if (Array.isArray(value)) {
      return value
        .map((item) => {
          if (typeof item === "string") return item;
          if (!item || typeof item !== "object") return "";
          return item.text || item.content || item.output_text || item.input_text || "";
        })
        .filter(Boolean)
        .join("\n")
        .trim();
    }
    if (typeof value === "object") {
      const direct = value.text || value.content || value.output || value.result;
      if (typeof direct === "string") return direct.trim();
      try {
        return JSON.stringify(value, null, 2);
      } catch {
        return "";
      }
    }
    return String(value).trim();
  }

  function activityCategory(name) {
    const value = String(name || "").toLowerCase();
    if (value.startsWith("mcp__") || value.startsWith("mcp_")) return "mcp";
    if (value.includes("skill")) return "skill";
    if (/web_search|search_web|browse|browser/.test(value)) return "web";
    if (/terminal|shell|command|exec|bash|powershell/.test(value)) return "command";
    if (/read_file|write_file|patch|filesystem|glob|search_files/.test(value)) return "file";
    if (/delegate|subagent|spawn_agent/.test(value)) return "subagent";
    return "other";
  }

  function buildActivityTimeline(source) {
    const meta = source?.meta || source || {};
    if (Array.isArray(meta.activities)) {
      return meta.activities.map((activity, index) => ({
        id: activity.id || `activity-${index}`,
        kind: activity.kind || "tool",
        category: activity.category || activityCategory(activity.name),
        name: activity.name || (activity.kind === "reasoning" ? "模型思考" : "工具调用"),
        input: structuredText(activity.input || activity.args || activity.args_text),
        output: structuredText(
          activity.output || activity.result || activity.result_text || activity.text,
        ),
        error: structuredText(activity.error),
        preview: structuredText(activity.preview || activity.summary || activity.progress),
        status: activity.status || "completed",
        started_at: activity.started_at || null,
        ended_at: activity.ended_at || null,
        duration_ms:
          activity.duration_ms ??
          (Number.isFinite(activity.duration_s) ? activity.duration_s * 1000 : null),
      }));
    }
    const activities = [];
    if (meta.reasoning) {
      activities.push({
        id: "legacy-reasoning",
        kind: "reasoning",
        category: "reasoning",
        name: "模型思考",
        output: structuredText(meta.reasoning),
        status: "completed",
      });
    }
    for (const [index, tool] of (meta.tools || []).entries()) {
      activities.push({
        id: tool.id || `legacy-tool-${index}`,
        kind: "tool",
        category: tool.category || activityCategory(tool.name),
        name: tool.name || "工具调用",
        input: structuredText(tool.input || tool.args || tool.args_text),
        output: structuredText(tool.output || tool.result || tool.result_text),
        preview: structuredText(tool.preview || tool.summary),
        error: structuredText(tool.error),
        status: tool.status || "completed",
        duration_ms: tool.duration_ms || null,
      });
    }
    return activities;
  }

  function normalizeOfficialMessages(messages, profile) {
    const normalized = [];
    let assistantTurn = [];
    const flushAssistant = () => {
      if (!assistantTurn.length) return;
      const assistantMessages = assistantTurn.filter(
        (message) => String(message.role || "").toLowerCase() === "assistant",
      );
      const source = assistantMessages[assistantMessages.length - 1] || assistantTurn[assistantTurn.length - 1];
      const content = [...assistantMessages]
        .reverse()
        .map((message) => structuredText(message.content))
        .find(Boolean) || "";
      const activities = [];
      const toolsById = new Map();
      let sequence = 0;
      for (const message of assistantTurn) {
        const role = String(message.role || "").toLowerCase();
        if (role === "assistant") {
          const reasoning = structuredText(
            message.reasoning_content || message.reasoning || message.thinking,
          );
          if (reasoning) {
            activities.push({
              id: `reasoning-${++sequence}`,
              kind: "reasoning",
              category: "reasoning",
              name: "模型思考",
              output: reasoning,
              status: "completed",
            });
          }
          for (const call of message.tool_calls || []) {
            const fn = call.function || {};
            const id = call.id || `tool-${++sequence}`;
            const name = fn.name || call.name || "tool";
            const activity = {
              id,
              kind: "tool",
              category: activityCategory(name),
              name,
              input: structuredText(fn.arguments || call.arguments),
              output: "",
              status: "running",
            };
            activities.push(activity);
            toolsById.set(id, activity);
          }
        } else if (role === "tool") {
          const id = message.tool_call_id || message.id;
          const activity = toolsById.get(id);
          if (activity) {
            activity.output = structuredText(message.content);
            activity.error = structuredText(message.error);
            activity.status = activity.error ? "failed" : "completed";
          }
        }
      }
      if (content || activities.length) {
        normalized.push({
          role: "assistant",
          name: profileDisplayName(profile),
          content,
          timestamp: Number(source.timestamp || 0),
          status: "completed",
          meta: { activities },
        });
      }
      assistantTurn = [];
    };
    for (const message of messages || []) {
      const role = String(message.role || "assistant").toLowerCase();
      if (role === "user") {
        flushAssistant();
        const content = structuredText(message.content);
        if (content) {
          normalized.push({
            role: "user",
            name: "user",
            content,
            timestamp: Number(message.timestamp || 0),
            status: "completed",
          });
        }
      } else if (role === "assistant" || role === "tool") {
        assistantTurn.push(message);
      }
    }
    flushAssistant();
    return normalized;
  }

  function mergeConversationIndex(conversations, officialSessions) {
    const mappedSessionIds = new Set(
      (conversations || []).flatMap((conversation) =>
        Object.values(conversation.runtime_sessions || {}),
      ),
    );
    const virtual = (officialSessions || [])
      .filter((session) => session?.id && !mappedSessionIds.has(session.id))
      .map((session) => ({
        id: `official:${session.id}`,
        official_session_id: session.id,
        title: session.title || session.preview || "官方会话",
        message_count: Number(session.message_count || 0),
        updated_at:
          Number(session.last_active || session.started_at || 0) * 1000,
        runtime_sessions: {},
      }));
    return [...(conversations || []), ...virtual].sort(
      (left, right) => Number(right.updated_at || 0) - Number(left.updated_at || 0),
    );
  }

  const ACTIVITY_LABELS = {
    reasoning: "思考",
    command: "命令",
    mcp: "MCP",
    skill: "Skill",
    web: "网页",
    file: "文件",
    browser: "浏览器",
    subagent: "子 Agent",
    other: "工具",
  };

  function formatActivityDuration(activity) {
    let duration = Number(activity.duration_ms);
    if (!Number.isFinite(duration) && activity.started_at && activity.ended_at) {
      duration = Number(activity.ended_at) - Number(activity.started_at);
    }
    if (!Number.isFinite(duration) || duration < 0) return "";
    if (duration < 1000) return `${Math.round(duration)} ms`;
    if (duration < 60000) return `${(duration / 1000).toFixed(duration < 10000 ? 1 : 0)} s`;
    return `${Math.floor(duration / 60000)}m ${Math.round((duration % 60000) / 1000)}s`;
  }

  function ActivityTimeline({ activities }) {
    if (!activities.length) return null;
    return h(
      "div",
      { className: "hc-activity-timeline" },
      activities.map((activity) => {
        const label = ACTIVITY_LABELS[activity.category] || ACTIVITY_LABELS[activity.kind] || "工具";
        const duration = formatActivityDuration(activity);
        const preview = activity.preview ||
          (activity.kind === "reasoning" ? activity.output : activity.input || activity.output) ||
          (activity.status === "running" ? "正在执行" : "已完成");
        return h(
          "details",
          {
            key: activity.id,
            className: `hc-activity-card is-${activity.status || "completed"} is-${activity.category}`,
            open: activity.status === "running",
          },
          h(
            "summary",
            null,
            h("i", { className: "hc-activity-status" }),
            h("span", { className: "hc-activity-kind" }, label),
            h("strong", null, activity.name || label),
            h("span", { className: "hc-activity-preview" }, preview),
            duration ? h("time", null, duration) : null,
            h("span", { className: "hc-activity-chevron", "aria-hidden": "true" }, "›"),
          ),
          h(
            "div",
            { className: "hc-activity-detail" },
            activity.input
              ? h("section", null, h("b", null, "输入"), h("pre", null, activity.input))
              : null,
            activity.output
              ? h(
                  "section",
                  null,
                  h("b", null, activity.kind === "reasoning" ? "思考内容" : "输出"),
                  h("pre", null, activity.output),
                )
              : null,
            activity.error
              ? h("section", { className: "is-error" }, h("b", null, "错误"), h("pre", null, activity.error))
              : null,
          ),
        );
      }),
    );
  }

  async function adoptOfficialStoredSession(
    sessionId,
    availableProfiles,
  ) {
    const encodedSessionId = encodeURIComponent(sessionId);
    const [sessionDetail, sessionMessageData] = await Promise.all([
      SDK.fetchJSON("/api/sessions/" + encodedSessionId),
      SDK.fetchJSON(
        "/api/sessions/" + encodedSessionId + "/messages",
      ),
    ]);
    const adoptionProfile =
      availableProfiles.find((profile) => profile.name === "default")
        ?.name ||
      availableProfiles[0]?.name ||
      "default";
    const importedMessages = normalizeOfficialMessages(
      sessionMessageData.messages || [],
      adoptionProfile,
    );
    const firstUserMessage = importedMessages.find(
      (message) => message.role === "user" && message.content,
    );
    return collabApi("/single/conversations/adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profile: adoptionProfile,
        session_id: sessionId,
        title:
          sessionDetail.title ||
          firstUserMessage?.content.slice(0, 36) ||
          "历史会话",
        messages: importedMessages,
      }),
    });
  }

  function UnifiedMessage({ message }) {
    if (message.kind === "route") {
      const confidence = Number(message.meta?.confidence);
      const sourceLabels = {
        model: "Hermes 模型判断",
        manual: "手动选择",
        rules: "快速判断",
      };
      const source = sourceLabels[message.meta?.source] || "自动判断";
      const profiles = message.meta?.profiles || [];
      return h(
        "article",
        {
          className:
            `hc-route-event is-${message.meta?.mode || "pending"}` +
            (message.status === "streaming" ? " is-streaming" : ""),
          role: "status",
        },
        h("i", { className: "hc-route-event-dot" }),
        h(
          "div",
          { className: "hc-route-event-copy" },
          h("strong", null, message.name || "正在判断任务类型"),
          message.content ? h("span", null, message.content) : null,
        ),
        h(
          "small",
          null,
          source,
          Number.isFinite(confidence) ? ` · ${Math.round(confidence * 100)}%` : "",
          profiles.length > 1 ? ` · ${profiles.join(" · ")}` : "",
        ),
      );
    }
    if (message.kind === "workflow") {
      return h(
        "article",
        { className: `hc-system-event is-${message.kind}` },
        h("span", { className: "hc-system-event-icon" }, "◇"),
        h(
          "div",
          null,
          h("strong", null, message.name),
          h("p", null, message.content),
          message.meta?.task_id
            ? h("small", null, `任务 ${message.meta.task_id}`)
            : null,
        ),
      );
    }
    const activities = buildActivityTimeline(message);
    const attachments = message.meta?.attachments || [];
    const connection = message.meta?.connection || null;
    const isUser = message.role === "user";
    const displayName = isUser ? "你" : profileDisplayName(message.name);
    return h(
      "article",
      {
        className:
          "hc-message " +
          (isUser ? "is-user" : "is-agent") +
          (message.status === "failed" ? " is-failed" : "") +
          (message.status === "streaming" ? " is-streaming" : ""),
      },
      h(
        "span",
        {
          className:
            "hc-avatar " + (isUser ? "is-user-avatar" : "is-hermes-avatar"),
          "aria-hidden": "true",
        },
        isUser
          ? profileAvatar(message.name, message.role)
          : h("img", {
            className: "hc-official-avatar",
            src: "/hermes-official.png",
            alt: "",
            }),
      ),
      h(
        "div",
        { className: "hc-message-stack" },
        h("header", null, h("strong", null, displayName)),
        connection
          ? h(
              "div",
              {
                className: `hc-connection-state is-${connection.status}`,
                role: "status",
              },
              h("i"),
              h("span", null, connection.text),
            )
          : null,
        h(ActivityTimeline, { activities }),
        h(
          "div",
          { className: "hc-message-body" },
          message.content ||
            (message.status === "streaming"
              ? h(
                  "span",
                  { className: "hc-response-pending", "aria-label": "正在回复" },
                  h("i"),
                  h("i"),
                  h("i"),
                )
              : ""),
        ),
        h(AttachmentList, { attachments }),
      ),
    );
  }

  function SingleChat() {
    const [profiles, setProfiles] = useState([]);
    const [conversations, setConversations] = useState([]);
    const [activeId, setActiveId] = useState("");
    const [messages, setMessages] = useState([]);
    const [selectedProfile, setSelectedProfile] = useState("default");
    const [routeMode, setRouteMode] = useState("auto");
    const [content, setContent] = useState("");
    const [composerOverflow, setComposerOverflow] = useState(false);
    const [composerExpanded, setComposerExpanded] = useState(false);
    const [loading, setLoading] = useState(true);
    const [sending, setSending] = useState(false);
    const [hostedRunning, setHostedRunning] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [pendingAttachments, setPendingAttachments] = useState([]);
    const [error, setError] = useState("");
    const streamRef = useRef(null);
    const fileInputRef = useRef(null);
    const composerInputRef = useRef(null);
    const expandedInputRef = useRef(null);
    const pinnedToBottomRef = useRef(true);
    const runtimeSessionsRef = useRef({});

    const loadConversation = useCallback(async (conversationId) => {
      if (!conversationId) {
        setMessages([]);
        return null;
      }
      pinnedToBottomRef.current = true;
      const data = await collabApi(
        "/single/conversations/" + encodeURIComponent(conversationId),
      );
      setMessages(withHostedRuntimeMessages(data.conversation));
      setHostedRunning(hasHostedRuntimeRuns(data.conversation));
      setSelectedProfile(data.conversation.profile || "default");
      runtimeSessionsRef.current = {
        ...(data.conversation.runtime_sessions || {}),
      };
      return data.conversation;
    }, []);

    const loadIndex = useCallback(async () => {
      setLoading(true);
      try {
        const [profileData, conversationData, officialSessionData] = await Promise.all([
          collabApi("/profiles"),
          collabApi("/single/conversations"),
          SDK.fetchJSON("/api/sessions?limit=50&offset=0&order=recent"),
        ]);
        const nextProfiles = profileData.profiles || [];
        let nextConversations = conversationData.conversations || [];
        const pendingStoredSessionId = window.sessionStorage.getItem(
          PENDING_STORED_SESSION_KEY,
        );
        let pendingConversation = pendingStoredSessionId
          ? nextConversations.find((item) =>
              Object.values(item.runtime_sessions || {}).includes(
                pendingStoredSessionId,
              ),
            )
          : null;
        let pendingResumeError = "";
        if (pendingStoredSessionId && !pendingConversation) {
          try {
            const adopted = await adoptOfficialStoredSession(
              pendingStoredSessionId,
              nextProfiles,
            );
            pendingConversation = adopted.conversation;
            nextConversations = [
              pendingConversation,
              ...nextConversations.filter(
                (item) => item.id !== pendingConversation.id,
              ),
            ];
          } catch (err) {
            pendingResumeError =
              err.message || "导入官方历史会话失败";
          }
        }
        setProfiles(nextProfiles);
        if (pendingStoredSessionId) {
          window.sessionStorage.removeItem(PENDING_STORED_SESSION_KEY);
        }
        const rememberedId = loadRememberedConversationId();
        const nextId =
          pendingConversation?.id ||
          nextConversations.find((item) => item.id === rememberedId)?.id ||
          nextConversations[0]?.id ||
          "";
        nextConversations = mergeConversationIndex(
          nextConversations,
          officialSessionData.sessions || [],
        );
        setConversations(nextConversations);
        setActiveId(nextId);
        if (nextId) {
          rememberConversationId(nextId);
        } else {
          rememberConversationId("");
        }
        await loadConversation(nextId);
        if (pendingStoredSessionId && !pendingConversation) {
          setError(
            pendingResumeError || "该官方历史会话暂时无法继续。",
          );
        }
      } catch (err) {
        setError(err.message || "智能会话加载失败");
      } finally {
        setLoading(false);
      }
    }, [loadConversation]);

    useEffect(() => {
      loadIndex();
    }, []);

    useEffect(() => {
      if (!activeId || !hostedRunning) return undefined;
      let inFlight = false;
      const timer = setInterval(async () => {
        if (inFlight) return;
        inFlight = true;
        try {
          const conversation = await loadConversation(activeId);
          if (!hasHostedRuntimeRuns(conversation)) {
            const [indexData, officialData] = await Promise.all([
              collabApi("/single/conversations"),
              SDK.fetchJSON("/api/sessions?limit=50&offset=0&order=recent"),
            ]);
            setConversations(
              mergeConversationIndex(
                indexData.conversations || [],
                officialData.sessions || [],
              ),
            );
          }
        } catch (err) {
          setError(err.message || "后台任务状态刷新失败");
        } finally {
          inFlight = false;
        }
      }, 3000);
      return () => clearInterval(timer);
    }, [activeId, hostedRunning, loadConversation]);

    useEffect(() => {
      const stream = streamRef.current;
      if (!stream || !pinnedToBottomRef.current) return;
      window.requestAnimationFrame(() => {
        stream.scrollTop = stream.scrollHeight;
      });
    }, [messages, sending]);

    useEffect(() => {
      pinnedToBottomRef.current = true;
      const frame = window.requestAnimationFrame(() => {
        const stream = streamRef.current;
        if (stream) stream.scrollTop = stream.scrollHeight;
      });
      return () => window.cancelAnimationFrame(frame);
    }, [activeId]);

    const resizeComposer = useCallback((node = composerInputRef.current) => {
      if (!node) return;
      node.style.height = "auto";
      const computed = window.getComputedStyle(node);
      const lineHeight = Number.parseFloat(computed.lineHeight) || 23;
      const verticalPadding =
        (Number.parseFloat(computed.paddingTop) || 0) +
        (Number.parseFloat(computed.paddingBottom) || 0);
      const twoLineHeight = Math.ceil(lineHeight * 2 + verticalPadding);
      const measuredHeight = node.scrollHeight;
      node.style.height = `${Math.min(measuredHeight, twoLineHeight)}px`;
      node.style.overflowY = "hidden";
      setComposerOverflow(measuredHeight > twoLineHeight + 1);
    }, []);

    useEffect(() => {
      const frame = window.requestAnimationFrame(() => resizeComposer());
      return () => window.cancelAnimationFrame(frame);
    }, [content, resizeComposer]);

    useEffect(() => {
      if (!composerExpanded) return;
      window.requestAnimationFrame(() => expandedInputRef.current?.focus());
    }, [composerExpanded]);

    const createConversation = useCallback(async () => {
      const data = await collabApi("/single/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile: selectedProfile || "default",
          title: "新对话",
        }),
      });
      setActiveId(data.conversation.id);
      rememberConversationId(data.conversation.id);
      setMessages([]);
      setHostedRunning(false);
      setPendingAttachments([]);
      runtimeSessionsRef.current = {};
      setConversations((current) => [data.conversation, ...current]);
      return data.conversation;
    }, [selectedProfile]);

    const selectConversation = useCallback(
      async (conversationId) => {
        if (!conversationId) return;
        pinnedToBottomRef.current = true;
        setLoading(true);
        setError("");
        setPendingAttachments([]);
        try {
          if (conversationId.startsWith("official:")) {
            const sessionId = conversationId.slice("official:".length);
            const adopted = await adoptOfficialStoredSession(sessionId, profiles);
            const conversation = adopted.conversation;
            setConversations((current) => [
              conversation,
              ...current.filter(
                (item) =>
                  item.id !== conversationId && item.id !== conversation.id,
              ),
            ]);
            setActiveId(conversation.id);
            rememberConversationId(conversation.id);
            await loadConversation(conversation.id);
          } else {
            setActiveId(conversationId);
            rememberConversationId(conversationId);
            await loadConversation(conversationId);
          }
        } catch (err) {
          setError(err.message || "历史会话加载失败");
        } finally {
          setLoading(false);
        }
      },
      [loadConversation, profiles],
    );

    useEffect(() => {
      const createFromShell = () => {
        if (sending) return;
        createConversation().catch((err) => {
          setError(err.message || "创建新会话失败");
        });
      };
      window.addEventListener(
        "hermes:new-unified-conversation",
        createFromShell,
      );
      return () => {
        window.removeEventListener(
          "hermes:new-unified-conversation",
          createFromShell,
        );
      };
    }, [createConversation, sending]);

    useEffect(() => {
      const resumeFromOfficialSession = async (event) => {
        const sessionId = String(event?.detail?.sessionId || "").trim();
        if (!sessionId || sending) return;
        window.sessionStorage.removeItem(PENDING_STORED_SESSION_KEY);
        setError("");
        try {
          const data = await collabApi("/single/conversations");
          let nextConversations = data.conversations || [];
          let conversation = nextConversations.find((item) =>
            Object.values(item.runtime_sessions || {}).includes(sessionId),
          );
          if (!conversation) {
            const adopted = await adoptOfficialStoredSession(
              sessionId,
              profiles,
            );
            conversation = adopted.conversation;
            nextConversations = [
              conversation,
              ...nextConversations.filter(
                (item) => item.id !== conversation.id,
              ),
            ];
          }
          setConversations(nextConversations);
          await selectConversation(conversation.id);
        } catch (err) {
          setError(err.message || "恢复历史会话失败");
        }
      };
      window.addEventListener(
        "hermes:resume-unified-session",
        resumeFromOfficialSession,
      );
      return () => {
        window.removeEventListener(
          "hermes:resume-unified-session",
          resumeFromOfficialSession,
        );
      };
    }, [profiles, selectConversation, sending]);

    const ensureConversation = async () => {
      if (activeId) return { id: activeId };
      return createConversation();
    };

    const uploadSelectedFiles = async (event) => {
      const files = Array.from(event.target.files || []);
      event.target.value = "";
      if (!files.length || uploading) return;
      setUploading(true);
      setError("");
      try {
        const conversation = await ensureConversation();
        const uploaded = [];
        for (const file of files) {
          const data = await collabApi(
            "/single/conversations/" +
              encodeURIComponent(conversation.id) +
              "/attachments",
            {
              method: "POST",
              headers: {
                "Content-Type": file.type || "application/octet-stream",
                "X-Filename": encodeURIComponent(file.name),
              },
              body: file,
            },
          );
          if (data.attachment) uploaded.push(data.attachment);
        }
        setPendingAttachments((current) => [...current, ...uploaded]);
      } catch (err) {
        setError(err.message || "附件上传失败");
      } finally {
        setUploading(false);
      }
    };

    const record = async (conversationId, message) => {
      await collabApi(
        "/single/conversations/" + encodeURIComponent(conversationId) + "/record",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(message),
        },
      );
    };

    const addMessage = (message) => {
      setMessages((current) => [...current, message]);
    };

    const patchMessage = (messageId, patcher) => {
      setMessages((current) =>
        current.map((message) =>
          message.id === messageId ? patcher(message) : message,
        ),
      );
    };

    const runProfile = async (
      conversationId,
      profile,
      prompt,
      continuedPrompt = prompt,
      sessionTitle = "",
    ) => {
      const streamId = `stream-${profile}-${Date.now()}-${Math.random()}`;
      const turnStartedAt = Date.now();
      const existingSessionId = runtimeSessionsRef.current[profile] || "";
      let accumulatedText = "";
      let activities = [];
      let activeReasoningId = "";
      let activitySequence = 0;
      const closeReasoning = () => {
        if (!activeReasoningId) return;
        const endedAt = Date.now();
        activities = activities.map((activity) =>
          activity.id === activeReasoningId
            ? {
                ...activity,
                status: "completed",
                ended_at: endedAt,
                duration_ms: Math.max(0, endedAt - activity.started_at),
              }
            : activity,
        );
        activeReasoningId = "";
      };
      const appendReasoning = (text, name = "模型思考") => {
        const value = structuredText(text);
        if (!value) return;
        if (!activeReasoningId) {
          activeReasoningId = `reasoning-${Date.now()}-${++activitySequence}`;
          activities = [
            ...activities,
            {
              id: activeReasoningId,
              kind: "reasoning",
              category: "reasoning",
              name,
              input: "",
              output: value,
              status: "running",
              started_at: Date.now(),
              ended_at: null,
            },
          ];
          return;
        }
        activities = activities.map((activity) => {
          if (activity.id !== activeReasoningId) return activity;
          if (value === activity.output || activity.output.includes(value)) return activity;
          return {
            ...activity,
            output: value.includes(activity.output) ? value : activity.output + value,
          };
        });
      };
      const patchActivity = (predicate, patch) => {
        let matched = false;
        activities = [...activities]
          .reverse()
          .map((activity) => {
            if (matched || !predicate(activity)) return activity;
            matched = true;
            return { ...activity, ...patch };
          })
          .reverse();
        return matched;
      };
      const addToolActivity = (payload, overrides = {}) => {
        const name = payload.name || overrides.name || "工具调用";
        const now = Date.now();
        const activity = {
          id: payload.tool_id || `tool-${now}-${++activitySequence}`,
          kind: overrides.kind || "tool",
          category: overrides.category || activityCategory(name),
          name,
          input: structuredText(payload.args_text || payload.context || overrides.input),
          output: structuredText(overrides.output),
          preview: structuredText(payload.preview || overrides.preview),
          error: "",
          status: overrides.status || "running",
          started_at: now,
          ended_at: null,
        };
        activities = [...activities, activity];
        return activity;
      };
      addMessage({
        id: streamId,
        role: "assistant",
        name: profile,
        content: "",
        status: "streaming",
        kind: "message",
        meta: { activities: [] },
      });
      let finalPayload = {};
      try {
        finalPayload = await streamProfileTurn(
          profile,
          existingSessionId ? continuedPrompt : prompt,
          async (event) => {
            const payload = event.payload || {};
            if (event.type === "session.ready") {
              const runtimeSessionId =
                payload.stored_session_id || payload.session_id || "";
              if (runtimeSessionId) {
                runtimeSessionsRef.current = {
                  ...runtimeSessionsRef.current,
                  [profile]: runtimeSessionId,
                };
                await collabApi(
                  "/single/conversations/" +
                    encodeURIComponent(conversationId) +
                    "/runtime-session",
                  {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      profile,
                      session_id: runtimeSessionId,
                      status: "running",
                    }),
                  },
                );
              }
              return;
            }
            patchMessage(streamId, (message) => {
              const meta = { ...(message.meta || {}) };
              if (event.type === "connection.waiting") {
                meta.connection = {
                  status: "waiting",
                  text: "设备离线，等待网络恢复；已提交任务会继续运行",
                };
                return { ...message, meta };
              }
              if (event.type === "connection.reconnecting") {
                meta.connection = {
                  status: "reconnecting",
                  text: `网络波动，正在恢复连接（${payload.attempt}/${payload.max_attempts}）…`,
                };
                return { ...message, meta };
              }
              if (event.type === "connection.restored") {
                meta.connection = {
                  status: "restored",
                  text: "连接已恢复，任务继续执行",
                };
                return { ...message, meta };
              }
              if (event.type === "message.delta") {
                closeReasoning();
                accumulatedText += payload.text || "";
                meta.activities = activities;
                return { ...message, content: accumulatedText, meta };
              }
              if (
                event.type === "reasoning.delta" ||
                event.type === "thinking.delta" ||
                event.type === "reasoning.available"
              ) {
                appendReasoning(payload.text || "");
              }
              if (event.type === "tool.start") {
                closeReasoning();
                addToolActivity(payload);
              }
              if (event.type === "tool.progress" || event.type === "tool.generating") {
                const name = payload.name || "工具调用";
                const preview = structuredText(
                  payload.preview || (event.type === "tool.generating" ? `正在生成 ${name}` : ""),
                );
                const matched = patchActivity(
                  (activity) => activity.kind === "tool" && activity.status === "running" && activity.name === name,
                  { preview },
                );
                if (!matched) addToolActivity(payload, { preview });
              }
              if (event.type === "tool.complete") {
                const endedAt = Date.now();
                const output = structuredText(
                  payload.result_text || payload.summary || payload.inline_diff,
                );
                const matched = patchActivity(
                  (activity) =>
                    activity.id === payload.tool_id ||
                    (activity.kind === "tool" && activity.status === "running" && activity.name === payload.name),
                  {
                    output,
                    preview: structuredText(payload.summary) || output,
                    error: structuredText(payload.error),
                    status: payload.error ? "failed" : "completed",
                    ended_at: endedAt,
                    duration_ms: Number.isFinite(payload.duration_s)
                      ? payload.duration_s * 1000
                      : null,
                  },
                );
                if (!matched) {
                  addToolActivity(payload, {
                    output,
                    preview: payload.summary,
                    status: payload.error ? "failed" : "completed",
                  });
                }
              }
              if (event.type === "browser.progress") {
                const preview = structuredText(payload.message);
                const matched = patchActivity(
                  (activity) => activity.category === "web" && activity.status === "running",
                  { preview, error: payload.level === "error" ? preview : "" },
                );
                if (!matched) {
                  addToolActivity(
                    { name: "browser", preview },
                    { category: "browser", preview },
                  );
                }
              }
              if (event.type === "subagent.start" || event.type === "subagent.spawn_requested") {
                closeReasoning();
                addToolActivity(
                  {
                    tool_id: payload.subagent_id,
                    name: payload.model ? `子 Agent · ${payload.model}` : "子 Agent",
                    context: payload.goal,
                  },
                  { kind: "subagent", category: "subagent", preview: payload.goal },
                );
              }
              if (event.type === "subagent.thinking") {
                closeReasoning();
                appendReasoning(payload.text || "", "子 Agent 思考");
              }
              if (event.type === "subagent.tool") {
                closeReasoning();
                addToolActivity(
                  {
                    name: payload.tool_name || "子 Agent 工具",
                    context: payload.tool_preview || payload.text,
                  },
                  { category: activityCategory(payload.tool_name), preview: payload.tool_preview || payload.text },
                );
              }
              if (event.type === "subagent.progress") {
                patchActivity(
                  (activity) => activity.kind === "subagent" && activity.status === "running",
                  { preview: structuredText(payload.text) },
                );
              }
              if (event.type === "subagent.complete") {
                const endedAt = Date.now();
                patchActivity(
                  (activity) =>
                    activity.kind === "subagent" &&
                    (!payload.subagent_id || activity.id === payload.subagent_id),
                  {
                    output: structuredText(payload.summary || payload.text),
                    preview: structuredText(payload.summary || payload.text),
                    status: /fail|error/i.test(payload.status || "") ? "failed" : "completed",
                    ended_at: endedAt,
                    duration_ms: Number.isFinite(payload.duration_seconds)
                      ? payload.duration_seconds * 1000
                      : null,
                  },
                );
              }
              meta.activities = activities;
              return { ...message, meta };
            });
          },
          existingSessionId,
          sessionTitle,
        );
        if (
          finalPayload.stored_session_id &&
          finalPayload.stored_session_id !== existingSessionId
        ) {
          runtimeSessionsRef.current = {
            ...runtimeSessionsRef.current,
            [profile]: finalPayload.stored_session_id,
          };
        }
        closeReasoning();
        const finalText = finalPayload.text || "";
        let outputAttachments = [];
        try {
          const artifactData = await collabApi(
            "/single/conversations/" +
              encodeURIComponent(conversationId) +
              "/attachments",
          );
          outputAttachments = (artifactData.attachments || []).filter(
            (attachment) =>
              attachment.bucket === "outputs" &&
              Number(attachment.updated_at || 0) >= turnStartedAt - 1000,
          );
        } catch {
          outputAttachments = [];
        }
        patchMessage(streamId, (message) => ({
          ...message,
          content: finalText || accumulatedText || message.content,
          status: finalPayload.status === "error" ? "failed" : "completed",
          meta: {
            ...(message.meta || {}),
            activities,
            attachments: outputAttachments,
            connection: null,
          },
        }));
        await record(conversationId, {
          role: "assistant",
          name: profile,
          content: finalText || accumulatedText || "执行完成",
          status: finalPayload.status === "error" ? "failed" : "completed",
          kind: "message",
          meta: {
            activities,
            attachments: outputAttachments,
            runtime_session_id:
              finalPayload.stored_session_id ||
              runtimeSessionsRef.current[profile] ||
              "",
          },
        });
      } catch (err) {
        if (err.submitted && err.stored_session_id) {
          runtimeSessionsRef.current = {
            ...runtimeSessionsRef.current,
            [profile]: err.stored_session_id,
          };
          setHostedRunning(true);
          await loadConversation(conversationId).catch(() => {});
          return;
        }
        patchMessage(streamId, (message) => ({
          ...message,
          content: `执行失败：${err.message || err}`,
          status: "failed",
        }));
        await record(conversationId, {
          role: "assistant",
          name: profile,
          content: `执行失败：${err.message || err}`,
          status: "failed",
          kind: "message",
        });
      }
    };

    const send = async () => {
      const value = content.trim();
      const attachmentsForTurn = [...pendingAttachments];
      if (
        (!value && !attachmentsForTurn.length) ||
        sending ||
        hostedRunning ||
        uploading
      ) return;
      setComposerExpanded(false);
      setContent("");
      setSending(true);
      setError("");
      try {
        const conversation = await ensureConversation();
        const conversationId = conversation.id;
        const workspaceData = await collabApi(
          "/single/conversations/" +
            encodeURIComponent(conversationId) +
            "/attachments",
        );
        const attachmentContext = attachmentsForTurn.length
          ? [
              "用户为本轮上传了以下附件：",
              ...attachmentsForTurn.map(
                (attachment) =>
                  `- ${attachment.name}: ${attachment.path}`,
              ),
            ].join("\n")
          : "";
        const deliveryContext = [
          `本会话交付文件目录：${workspaceData.output_dir}`,
          "如果用户要求 PPT、Word、PDF、表格、图片、压缩包或其他文件，必须把最终文件保存到该目录。",
          "如果实际工作在远程 PC Worker 上完成，请在回复前把交付文件复制回这个 DBB3 目录。",
        ].join("\n");
        const userContent = value || "请处理我上传的附件。";
        const continuousPrompt = buildContinuousPrompt(
          selectedProfile || "default",
          userContent,
          messages,
        );
        const promptWithFiles = [
          continuousPrompt,
          attachmentContext,
          deliveryContext,
        ]
          .filter(Boolean)
          .join("\n\n");
        const userMessage = {
          id: `user-${Date.now()}`,
          role: "user",
          name: "你",
          content: userContent,
          status: "completed",
          kind: "message",
          meta: { attachments: attachmentsForTurn },
          created_at: Date.now(),
        };
        addMessage(userMessage);
        await record(conversationId, userMessage);
        setPendingAttachments([]);

        const routeMessageId = `route-${Date.now()}`;
        addMessage({
          id: routeMessageId,
          role: "system",
          name: "正在判断任务类型",
          content: "",
          status: "streaming",
          kind: "route",
          meta: { mode: "pending" },
          created_at: Date.now(),
        });
        const route = await collabApi("/route", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: userContent, mode: routeMode }),
        });
        if (route.mode === "chat") route.profiles = [selectedProfile || "default"];
        const routeMessage = {
          id: routeMessageId,
          role: "system",
          name: route.label,
          content: route.reason,
          status: "completed",
          kind: "route",
          meta: {
            mode: route.mode,
            confidence: route.confidence,
            source: route.source,
            profiles: route.profiles || [],
          },
          created_at: Date.now(),
        };
        patchMessage(routeMessageId, () => routeMessage);
        await record(conversationId, routeMessage);

        if (route.mode === "work") {
          const workflowId = `workflow-${Date.now()}`;
          addMessage({
            id: workflowId,
            role: "system",
            name: "正在创建工作流",
            content: "DBB3 正在创建根任务并调用官方拆分器。",
            status: "streaming",
            kind: "workflow",
            meta: {},
          });
          const created = await kanbanApi("/tasks", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              title: route.title,
              body: userContent,
              triage: true,
              workspace_kind: "scratch",
              goal_mode: true,
            }),
          });
          const task = created.task;
          let decomposition = null;
          if (task?.id) {
            patchMessage(workflowId, (message) => ({
              ...message,
              name: "根任务已创建",
              content: "正在分析依赖并拆分执行步骤。",
              meta: { task_id: task.id },
            }));
            decomposition = await kanbanApi(
              "/tasks/" + encodeURIComponent(task.id) + "/decompose",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ author: "unified-webui" }),
              },
            );
          }
          const workflowText = decomposition?.fanout
            ? `已拆分为 ${decomposition.child_ids.length} 个子任务，Dispatcher 将按 Profile 能力执行。`
            : "根任务已进入官方 Kanban，Dispatcher 将继续处理。";
          patchMessage(workflowId, (message) => ({
            ...message,
            name: "工作流已启动",
            content: workflowText,
            status: "completed",
            meta: { task_id: task?.id, child_ids: decomposition?.child_ids || [] },
          }));
          await record(conversationId, {
            role: "system",
            name: "工作流已启动",
            content: workflowText,
            status: "completed",
            kind: "workflow",
            meta: { task_id: task?.id, child_ids: decomposition?.child_ids || [] },
          });
          await Promise.all(
            (route.profiles || ["default", "reviewer"]).map((profile) => {
              const workPrompt = [
                  "你正在 DBB3 统一智能会话的工作任务中。",
                  `你的 Profile：${profile}`,
                  task?.id ? `官方 Kanban 根任务：${task.id}` : "",
                  "请实时分析该任务。经理负责计划和协调，执行端说明执行方案，reviewer 负责风险与验收。",
                  "不要创建第二控制面；实际任务状态以官方 Kanban 为准。",
                  `用户任务：${userContent}`,
                  attachmentContext,
                  deliveryContext,
                ].filter(Boolean).join("\n");
              return runProfile(
                conversationId,
                profile,
                workPrompt,
                workPrompt,
                route.title,
              );
            }),
          );
        } else {
          await runProfile(
            conversationId,
            route.profiles[0],
            promptWithFiles,
            [
              userContent,
              attachmentContext,
              deliveryContext,
            ]
              .filter(Boolean)
              .join("\n\n"),
          );
        }
        const [indexData, officialData] = await Promise.all([
          collabApi("/single/conversations"),
          SDK.fetchJSON("/api/sessions?limit=50&offset=0&order=recent"),
        ]);
        setConversations(
          mergeConversationIndex(
            indexData.conversations || [],
            officialData.sessions || [],
          ),
        );
      } catch (err) {
        setError(err.message || "统一会话执行失败");
      } finally {
        setSending(false);
      }
    };

    return h(
      "section",
      { className: "hc-single-chat" },
      h(
        "aside",
        { className: "hc-single-sidebar" },
        h(
          "div",
          { className: "hc-single-brand" },
          h("span", { className: "hc-room-icon" }, "H"),
          h("div", null, h("strong", null, "智能会话"), h("small", null, "DBB3 CONTROL PLANE")),
        ),
        h(
          "button",
          {
            className: "hc-primary hc-new-chat",
            disabled: sending,
            onClick: createConversation,
          },
          "＋ 新建会话",
        ),
        h("div", { className: "hc-single-history-label" }, "最近会话"),
        h(
          "div",
          { className: "hc-single-history" },
          conversations.map((item) =>
            h(
              "button",
              {
                key: item.id,
                className: "hc-single-history-item" + (item.id === activeId ? " is-active" : ""),
                onClick: () => selectConversation(item.id),
              },
              h("strong", null, item.title || "新对话"),
              h("small", null, `${item.message_count || 0} 条记录`),
            ),
          ),
        ),
      ),
      h(
        "div",
        { className: "hc-single-main" },
        h(
          "header",
          { className: "hc-single-header" },
          h(
            "div",
            { className: "hc-single-heading" },
            h(
              "button",
              {
                className: "hc-nav-toggle",
                type: "button",
                onClick: openNavigation,
                "aria-label": "打开导航",
              },
              "☰",
            ),
            h(
              "span",
              { className: "hc-header-avatar", "aria-hidden": "true" },
              h("img", {
                className: "hc-official-avatar",
                src: "/hermes-official.png",
                alt: "",
              }),
            ),
            h(
              "div",
              null,
              h("strong", null, "Hermes Agent"),
              h("small", null, "当前窗口持续使用同一个会话"),
            ),
          ),
          h(
            "div",
            { className: "hc-header-controls" },
            h(
              "select",
              {
                className: "hc-route-select",
                value: routeMode,
                onChange: (event) => setRouteMode(event.target.value),
                "aria-label": "任务识别方式",
              },
              h("option", { value: "auto" }, "自动判断"),
              h("option", { value: "chat" }, "普通对话"),
              h("option", { value: "work" }, "工作任务"),
            ),
            h(
              "button",
              {
                className: "hc-model-tools",
                type: "button",
                onClick: openModelTools,
              },
              "模型与工具",
            ),
            h(
              "span",
              {
                className: "hc-live-dot" + (sending ? " is-busy" : ""),
                title: sending ? "Hermes 正在回复" : "Hermes 在线",
              },
            ),
          ),
        ),
        h(
          "div",
          {
            className: "hc-single-stream",
            ref: streamRef,
            onScroll: (event) => {
              const node = event.currentTarget;
              pinnedToBottomRef.current =
                node.scrollHeight - node.scrollTop - node.clientHeight < 72;
            },
          },
          loading
            ? h("div", { className: "hc-thinking" }, "正在加载会话...")
            : !messages.length
              ? h(
                  "div",
                  { className: "hc-single-welcome" },
                  h("span", { className: "hc-single-orb" }, "H"),
                  h("h2", null, "直接告诉 Hermes 你想做什么"),
                  h("p", null, "闲聊自动走单 Profile；需要执行的任务自动进入多 Profile 协作与官方工作流。"),
                )
              : messages.map((message) =>
                  h(UnifiedMessage, { key: message.id, message }),
                ),
        ),
        error ? h("div", { className: "hc-error hc-single-error" }, error) : null,
        h(
          "div",
          { className: "hc-single-composer" },
          h(AttachmentList, { attachments: pendingAttachments }),
          h("input", {
            ref: fileInputRef,
            className: "hc-file-input",
            type: "file",
            multiple: true,
            accept: "image/*,.pdf,.ppt,.pptx,.doc,.docx,.xls,.xlsx,.csv,.txt,.md,.zip",
            onChange: uploadSelectedFiles,
          }),
          h(
            "div",
            {
              className:
                "hc-single-input-shell" +
                (composerOverflow ? " has-overflow" : ""),
            },
            h(
              "button",
              {
                className: "hc-attach-button",
                type: "button",
                disabled: uploading || sending,
                onClick: () => fileInputRef.current?.click(),
                "aria-label": "上传图片或文件",
                title: "上传图片或文件",
              },
              uploading ? "…" : "＋",
            ),
            h("textarea", {
              ref: composerInputRef,
              value: content,
              rows: 1,
              placeholder: "输入消息",
              onChange: (event) => {
                setContent(event.target.value);
                resizeComposer(event.currentTarget);
              },
              onKeyDown: (event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  send();
                }
              },
            }),
            composerOverflow
              ? h(
                  "button",
                  {
                    className: "hc-composer-expand",
                    type: "button",
                    onClick: () => setComposerExpanded(true),
                    "aria-label": "展开编辑消息",
                    title: "展开编辑消息",
                  },
                  "↗",
                )
              : null,
            h(
              "button",
              {
                className: "hc-single-send",
                disabled:
                  sending ||
                  hostedRunning ||
                  uploading ||
                  (!content.trim() && !pendingAttachments.length),
                onClick: send,
                "aria-label": "发送消息",
              },
              sending || hostedRunning ? "…" : "↑",
            ),
          ),
        ),
        composerExpanded
          ? h(
              "section",
              {
                className: "hc-expanded-composer",
                role: "dialog",
                "aria-modal": "true",
                "aria-label": "编辑消息",
              },
              h(
                "header",
                null,
                h("strong", null, "编辑消息"),
                h(
                  "button",
                  {
                    type: "button",
                    onClick: () => setComposerExpanded(false),
                    "aria-label": "收起编辑器",
                  },
                  "↙",
                ),
              ),
              h("textarea", {
                ref: expandedInputRef,
                value: content,
                placeholder: "输入消息",
                onChange: (event) => setContent(event.target.value),
                onKeyDown: (event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault();
                    send();
                  }
                },
              }),
              h(AttachmentList, { attachments: pendingAttachments }),
              h(
                "footer",
                null,
                h(
                  "button",
                  {
                    className: "hc-expanded-attach",
                    type: "button",
                    disabled: uploading || sending,
                    onClick: () => fileInputRef.current?.click(),
                    "aria-label": "上传图片或文件",
                  },
                  uploading ? "…" : "＋",
                ),
                h(
                  "button",
                  {
                    className: "hc-expanded-send",
                    type: "button",
                    disabled:
                      sending ||
                      hostedRunning ||
                      uploading ||
                      (!content.trim() && !pendingAttachments.length),
                    onClick: send,
                    "aria-label": "发送消息",
                  },
                  sending || hostedRunning ? "…" : "↑",
                ),
              ),
            )
          : null,
      ),
    );
  }

  function ChatTopSlot() {
    return h(
      "div",
      { className: "hc-chat-top" },
      h(SingleChat),
    );
  }

  function EmptyState({ title, body }) {
    return h(
      "div",
      { className: "hc-empty" },
      h("div", { className: "hc-empty-orbit" }, h("span"), h("span"), h("span")),
      h("h3", null, title),
      h("p", null, body),
    );
  }

  function ProfilePill({ profile, checked, onChange }) {
    return h(
      "label",
      { className: "hc-profile-pill" + (checked ? " is-selected" : "") },
      h("input", {
        type: "checkbox",
        checked,
        onChange: (event) => onChange(event.target.checked),
      }),
      h("span", { className: "hc-avatar" }, profile.name.slice(0, 2).toUpperCase()),
      h(
        "span",
        { className: "hc-profile-copy" },
        h("strong", null, profile.name),
        h("small", null, profile.description || profile.model || "Hermes Profile"),
      ),
    );
  }

  function NewRoom({ profiles, onCreated }) {
    const [name, setName] = useState("");
    const [selected, setSelected] = useState([]);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");

    const toggle = (profile, checked) => {
      setSelected((current) =>
        checked
          ? Array.from(new Set([...current, profile]))
          : current.filter((item) => item !== profile),
      );
    };

    const create = async () => {
      if (!selected.length) {
        setError("至少选择一个 Hermes Profile");
        return;
      }
      setSaving(true);
      setError("");
      try {
        const data = await collabApi("/rooms", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name || "新群聊", profiles: selected }),
        });
        setName("");
        setSelected([]);
        onCreated(data.room);
      } catch (err) {
        setError(err.message || "创建群聊失败");
      } finally {
        setSaving(false);
      }
    };

    return h(
      "section",
      { className: "hc-new-room hc-panel" },
      h("div", { className: "hc-section-kicker" }, "NEW ROOM"),
      h("h2", null, "创建多 Profile 群聊"),
      h("p", null, "每个成员直接使用现有 Hermes Profile 的模型、Skill、MCP 与记忆。"),
      h("input", {
        className: "hc-input",
        value: name,
        placeholder: "群聊名称",
        onChange: (event) => setName(event.target.value),
      }),
      h(
        "div",
        { className: "hc-profile-grid" },
        profiles.map((profile) =>
          h(ProfilePill, {
            key: profile.name,
            profile,
            checked: selected.includes(profile.name),
            onChange: (checked) => toggle(profile.name, checked),
          }),
        ),
      ),
      error ? h("div", { className: "hc-error" }, error) : null,
      h(
        "button",
        { className: "hc-primary", disabled: saving, onClick: create },
        saving ? "正在创建..." : "创建群聊",
      ),
    );
  }

  function Message({ message }) {
    const failed = message.status === "failed";
    return h(
      "article",
      {
        className:
          "hc-message " +
          (message.role === "user" ? "is-user" : "is-agent") +
          (failed ? " is-failed" : ""),
      },
      h(
        "header",
        null,
        h("span", { className: "hc-avatar" }, (message.name || "?").slice(0, 2)),
        h("strong", null, message.name || "成员"),
        h(
          "time",
          null,
          message.created_at
            ? new Date(message.created_at).toLocaleTimeString("zh-CN", {
                hour: "2-digit",
                minute: "2-digit",
              })
            : "",
        ),
      ),
      h("div", { className: "hc-message-body" }, message.content),
    );
  }

  function RoomView({ roomId, onBack }) {
    const [room, setRoom] = useState(null);
    const [content, setContent] = useState("");
    const [sending, setSending] = useState(false);
    const [error, setError] = useState("");

    const load = useCallback(async () => {
      const data = await collabApi("/rooms/" + encodeURIComponent(roomId));
      setRoom(data.room);
    }, [roomId]);

    useEffect(() => {
      load().catch((err) => setError(err.message || "群聊加载失败"));
    }, [load]);

    const send = async () => {
      if (!content.trim() || sending) return;
      const value = content.trim();
      setContent("");
      setSending(true);
      setError("");
      setRoom((current) =>
        current
          ? {
              ...current,
              messages: [
                ...(current.messages || []),
                {
                  id: "optimistic-" + Date.now(),
                  role: "user",
                  name: "用户",
                  content: value,
                  created_at: Date.now(),
                },
              ],
            }
          : current,
      );
      try {
        await collabApi(
          "/rooms/" + encodeURIComponent(roomId) + "/messages",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: value }),
          },
        );
        await load();
      } catch (err) {
        setError(err.message || "群聊执行失败");
        await load().catch(() => {});
      } finally {
        setSending(false);
      }
    };

    if (!room) {
      return h(
        "div",
        { className: "hc-panel" },
        h("button", { className: "hc-ghost", onClick: onBack }, "← 返回"),
        h("p", null, error || "正在加载群聊..."),
      );
    }

    return h(
      "section",
      { className: "hc-room" },
      h(
        "div",
        { className: "hc-room-header hc-panel" },
        h("button", { className: "hc-ghost", onClick: onBack }, "← 群聊列表"),
        h(
          "div",
          null,
          h("div", { className: "hc-section-kicker" }, "LIVE COLLABORATION"),
          h("h2", null, room.name),
          h("p", null, (room.profiles || []).join(" · ")),
        ),
        h("span", { className: "hc-live" }, h("i"), sending ? "执行中" : "实时"),
      ),
      h(
        "div",
        { className: "hc-message-stream" },
        !(room.messages || []).length
          ? h(EmptyState, {
              title: "发送第一条协作消息",
              body: "选中的 Profiles 会依次分析并在这里展示回复。",
            })
          : room.messages.map((message) =>
              h(Message, { key: message.id, message }),
            ),
        sending
          ? h(
              "div",
              { className: "hc-thinking" },
              h("span"),
              h("span"),
              h("span"),
              "Hermes Profiles 正在协作",
            )
          : null,
      ),
      error ? h("div", { className: "hc-error" }, error) : null,
      h(
        "div",
        { className: "hc-composer hc-panel" },
        h("textarea", {
          value: content,
          placeholder: "向群聊下发任务或继续讨论...",
          onChange: (event) => setContent(event.target.value),
          onKeyDown: (event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              send();
            }
          },
        }),
        h(
          "button",
          { className: "hc-primary", disabled: sending, onClick: send },
          sending ? "执行中..." : "发送",
        ),
      ),
    );
  }

  function GroupMode() {
    const [profiles, setProfiles] = useState([]);
    const [rooms, setRooms] = useState([]);
    const [activeRoom, setActiveRoom] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState("");

    const load = useCallback(async () => {
      setLoading(true);
      try {
        const [profileData, roomData] = await Promise.all([
          collabApi("/profiles"),
          collabApi("/rooms"),
        ]);
        setProfiles(profileData.profiles || []);
        setRooms(roomData.rooms || []);
      } catch (err) {
        setError(err.message || "协作数据加载失败");
      } finally {
        setLoading(false);
      }
    }, []);

    useEffect(() => {
      load();
    }, [load]);

    if (activeRoom) {
      return h(RoomView, {
        roomId: activeRoom,
        onBack: () => {
          setActiveRoom(null);
          load();
        },
      });
    }

    return h(
      "div",
      { className: "hc-grid" },
      h(
        "section",
        { className: "hc-panel hc-room-list" },
        h("div", { className: "hc-section-kicker" }, "GROUP SESSIONS"),
        h("h2", null, "群聊会话"),
        loading ? h("p", null, "正在加载...") : null,
        error ? h("div", { className: "hc-error" }, error) : null,
        !loading && !rooms.length
          ? h(EmptyState, {
              title: "还没有群聊",
              body: "从右侧选择 DBB3 与本地电脑 Profiles 创建协作空间。",
            })
          : h(
              "div",
              { className: "hc-room-cards" },
              rooms.map((room) =>
                h(
                  "button",
                  {
                    key: room.id,
                    className: "hc-room-card",
                    onClick: () => setActiveRoom(room.id),
                  },
                  h("span", { className: "hc-room-icon" }, "H"),
                  h(
                    "span",
                    { className: "hc-room-copy" },
                    h("strong", null, room.name),
                    h("small", null, (room.profiles || []).join(" · ")),
                    h(
                      "em",
                      null,
                      room.message_count
                        ? `${room.message_count} 条消息`
                        : "等待第一条消息",
                    ),
                  ),
                  h("span", { className: "hc-chevron" }, "→"),
                ),
              ),
            ),
      ),
      h(NewRoom, {
        profiles,
        onCreated: (room) => {
          setRooms((current) => [room, ...current]);
          setActiveRoom(room.id);
        },
      }),
    );
  }

  function flattenTasks(board) {
    if (Array.isArray(board.tasks)) return board.tasks;
    const columns = Array.isArray(board.columns) ? board.columns : [];
    return columns.flatMap((column) =>
      Array.isArray(column.tasks)
        ? column.tasks.map((task) => ({
            ...task,
            status: task.status || column.status || column.id,
          }))
        : [],
    );
  }

  function WorkflowCard({ task, children }) {
    const status = task.status || "todo";
    return h(
      "article",
      { className: "hc-workflow-card status-" + status },
      h(
        "header",
        null,
        h("span", { className: "hc-status-dot" }),
        h("strong", null, task.title),
        h("span", { className: "hc-status-label" }, status),
      ),
      h("p", null, task.body || task.latest_summary || "暂无摘要"),
      h(
        "div",
        { className: "hc-workflow-meta" },
        h("span", null, task.assignee || "未分配"),
        h("span", null, task.id),
      ),
      children && children.length
        ? h(
            "div",
            { className: "hc-child-flow" },
            children.map((child) =>
              h(
                "div",
                { key: child.id, className: "hc-child-node status-" + child.status },
                h("i"),
                h("strong", null, child.title),
                h("small", null, child.assignee || child.status),
              ),
            ),
          )
        : null,
    );
  }

  function WorkflowMode() {
    const [tasks, setTasks] = useState([]);
    const [title, setTitle] = useState("");
    const [body, setBody] = useState("");
    const [loading, setLoading] = useState(true);
    const [creating, setCreating] = useState(false);
    const [error, setError] = useState("");

    const load = useCallback(async () => {
      setLoading(true);
      try {
        const data = await kanbanApi("/board");
        setTasks(flattenTasks(data));
      } catch (err) {
        setError(err.message || "工作流加载失败");
      } finally {
        setLoading(false);
      }
    }, []);

    useEffect(() => {
      load();
    }, [load]);

    const taskById = useMemo(
      () => Object.fromEntries(tasks.map((task) => [task.id, task])),
      [tasks],
    );
    const roots = useMemo(
      () =>
        tasks.filter((task) => {
          const parents = task.parents || task.parent_ids || [];
          return !parents.length || !parents.some((id) => taskById[id]);
        }),
      [tasks, taskById],
    );
    const childrenFor = (rootId) =>
      tasks.filter((task) => {
        const parents = task.parents || task.parent_ids || [];
        return parents.includes(rootId);
      });

    const createWorkflow = async () => {
      if (!title.trim() || creating) return;
      setCreating(true);
      setError("");
      try {
        const created = await kanbanApi("/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: title.trim(),
            body: body.trim() || title.trim(),
            triage: true,
            workspace_kind: "scratch",
            goal_mode: true,
          }),
        });
        const task = created.task;
        if (task && task.id) {
          await kanbanApi("/tasks/" + encodeURIComponent(task.id) + "/decompose", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ author: "official-webui" }),
          });
        }
        setTitle("");
        setBody("");
        await load();
      } catch (err) {
        setError(err.message || "创建工作流失败");
      } finally {
        setCreating(false);
      }
    };

    return h(
      "div",
      { className: "hc-workflow-layout" },
      h(
        "section",
        { className: "hc-panel hc-workflow-create" },
        h("div", { className: "hc-section-kicker" }, "NEW WORKFLOW"),
        h("h2", null, "创建 Hermes 工作流"),
        h(
          "p",
          null,
          "由 DBB3 官方 Hermes 拆分根任务，并按 Profile 能力派发到 DBB3 或本地电脑。",
        ),
        h("input", {
          className: "hc-input",
          value: title,
          placeholder: "总任务标题",
          onChange: (event) => setTitle(event.target.value),
        }),
        h("textarea", {
          className: "hc-input hc-textarea",
          value: body,
          placeholder: "补充目标、约束和验收标准",
          onChange: (event) => setBody(event.target.value),
        }),
        h(
          "button",
          {
            className: "hc-primary",
            disabled: creating,
            onClick: createWorkflow,
          },
          creating ? "正在拆分任务..." : "创建并自动拆分",
        ),
        error ? h("div", { className: "hc-error" }, error) : null,
      ),
      h(
        "section",
        { className: "hc-workflow-board" },
        h(
          "div",
          { className: "hc-board-heading" },
          h(
            "div",
            null,
            h("div", { className: "hc-section-kicker" }, "LIVE EXECUTION"),
            h("h2", null, "实时工作流"),
          ),
          h("button", { className: "hc-ghost", onClick: load }, "刷新"),
        ),
        loading
          ? h("p", null, "正在读取官方 Kanban...")
          : !roots.length
            ? h(EmptyState, {
                title: "暂无工作流",
                body: "创建总任务后，拆分结果和执行状态会实时显示在这里。",
              })
            : h(
                "div",
                { className: "hc-workflow-list" },
                roots.map((task) =>
                  h(WorkflowCard, {
                    key: task.id,
                    task,
                    children: childrenFor(task.id),
                  }),
                ),
              ),
      ),
    );
  }

  function CollaborationPage() {
    const [mode, setMode] = useState(routeMode());

    useEffect(() => {
      const onPopState = () => setMode(routeMode());
      window.addEventListener("popstate", onPopState);
      return () => window.removeEventListener("popstate", onPopState);
    }, []);

    return h(
      "div",
      { className: "hc-shell" },
      h(
        "header",
        { className: "hc-hero" },
        h(
          "div",
          null,
          h("div", { className: "hc-section-kicker" }, "HERMES OFFICIAL COLLABORATION"),
          h("h1", null, mode === "group" ? "多智能体群聊" : "任务工作流"),
          h(
            "p",
            null,
            "Studio 风格界面，底层仍是 DBB3 官方 Hermes、官方 Profiles 与官方 Kanban。",
          ),
        ),
        h(ModeSwitch, { active: mode }),
      ),
      mode === "group" ? h(GroupMode) : h(WorkflowMode),
    );
  }

  registry.register("collaboration", CollaborationPage);
  registry.registerSlot("collaboration", "chat:top", ChatTopSlot);
})();

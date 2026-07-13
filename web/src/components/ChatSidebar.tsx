/**
 * ChatSidebar — structured-events panel that sits next to the xterm.js
 * terminal in the dashboard Chat tab.
 *
 * Two WebSockets, one per concern:
 *
 *   1. **JSON-RPC sidecar** (`GatewayClient` → /api/ws) — a lightweight
 *      session used only for connection state (the "live" badge) and
 *      credential warnings. Independent of the PTY pane's session by
 *      design. The model badge does NOT come from here: it reads the
 *      effective config model over REST (`/api/model/info`), and the model
 *      picker writes config over REST (`/api/model/set`) then offers a
 *      dashboard reload so the running chat adopts the new model.
 *
 *   2. **Event subscriber** (/api/events?channel=…) — passive, receives
 *      every dispatcher emit from the PTY-side `tui_gateway.entry` that
 *      the dashboard fanned out.  The sidebar uses it for `session.info`
 *      (live chat title) and `dashboard.new_session_requested`.  The
 *      `channel` id ties this listener to the same chat tab's PTY child —
 *      see `ChatPage.tsx` for where the id is generated.
 *
 * Best-effort throughout: WS failures show in the badge / banner, the
 * terminal pane keeps working unimpaired.
 */

import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card } from "@nous-research/ui/ui/components/card";

import { ModelPickerDialog } from "@/components/ModelPickerDialog";
import { ModelReloadConfirm } from "@/components/ModelReloadConfirm";
import { ReasoningPicker } from "@/components/ReasoningPicker";
import { useI18n } from "@/i18n";
import { GatewayClient, type ConnectionState } from "@/lib/gatewayClient";
import { api, buildWsUrl } from "@/lib/api";
import { titleFromSessionInfoPayload } from "@/lib/chat-title";

import { cn } from "@/lib/utils";
import { AlertCircle, ChevronDown, MessageSquarePlus } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

interface SessionInfo {
  cwd?: string;
  model?: string;
  provider?: string;
  credential_warning?: string;
  title?: string;
}

interface RpcEnvelope {
  method?: string;
  params?: { type?: string; payload?: unknown };
}

type EventFeedState = "connecting" | "open" | "waiting" | "error";

const STATE_LABEL: Record<ConnectionState, string> = {
  idle: "idle",
  connecting: "connecting",
  open: "live",
  closed: "closed",
  error: "error",
};

const STATE_TONE: Record<
  ConnectionState,
  "secondary" | "warning" | "success" | "destructive"
> = {
  idle: "secondary",
  connecting: "warning",
  open: "success",
  closed: "secondary",
  error: "destructive",
};

interface ChatSidebarProps {
  channel: string;
  /** Chat profile from the dashboard switcher / URL scope. */
  profile?: string;
  className?: string;
  onDashboardNewSessionRequest?: () => void;
  onSessionTitleChange?: (title: string | null) => void;
}

export function ChatSidebar({
  channel,
  profile,
  className,
  onDashboardNewSessionRequest,
  onSessionTitleChange,
}: ChatSidebarProps) {
  const { locale } = useI18n();
  const isChinese = locale.startsWith("zh");
  const stateLabel: Record<ConnectionState, string> = isChinese
    ? {
        idle: "空闲",
        connecting: "连接中",
        open: "在线",
        closed: "已断开",
        error: "错误",
      }
    : STATE_LABEL;
  // `version` bumps on reconnect; gw is derived so we never call setState
  // for it inside an effect (React 19's set-state-in-effect rule). The
  // counter is the dependency on purpose — it's not read in the memo body,
  // it's the signal that says "rebuild the client".
  const [version, setVersion] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const gw = useMemo(() => new GatewayClient(), [version]);

  const [state, setState] = useState<ConnectionState>("idle");
  const [info, setInfo] = useState<SessionInfo>({});
  const [modelOpen, setModelOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [eventFeedState, setEventFeedState] =
    useState<EventFeedState>("connecting");
  const [eventFeedDetail, setEventFeedDetail] = useState(
    isChinese ? "正在连接" : "Connecting",
  );
  // The badge shows config.yaml's main model (`model.default`) via
  // `/api/model/info` — the same value the Models page writes and a new chat
  // session boots from. We deliberately don't use the sidecar's `session.info`
  // model: that's a one-time snapshot of the throwaway sidecar agent taken when
  // its session is created, and it never updates when the model is changed
  // elsewhere, so the badge would go stale. Pass the chat profile explicitly so
  // this card stays scoped to the PTY even if the global dashboard switcher
  // changes while the chat is open.
  const [effectiveModel, setEffectiveModel] = useState("");
  // Whether the effective model supports reasoning effort — gates the
  // ReasoningPicker. Read from the same `/api/model/info` capabilities the
  // (currently unused) ModelInfoCard surfaces, so the dashboard exposes a
  // control to *set* the level, not just a read-only "Reasoning" badge.
  const [supportsReasoning, setSupportsReasoning] = useState(false);
  // Bumped on model change/save so ReasoningPicker re-reads the saved effort
  // (config is profile-scoped the same way the model badge is).
  const [modelRefreshKey, setModelRefreshKey] = useState(0);
  // Set after the picker saves a model and the user declines the reload: config
  // is updated but the running session keeps its model until rebuilt.
  const [modelNotice, setModelNotice] = useState<string | null>(null);
  // Short name of a just-saved model awaiting confirm to reload (a fresh chat
  // session is how the running chat adopts it; we confirm before discarding it).
  const [pendingReloadModel, setPendingReloadModel] = useState<string | null>(
    null,
  );

  const refreshEffectiveModel = useCallback(() => {
    void api
      .getModelInfo(profile)
      .then((r) => {
        if (r?.model) setEffectiveModel(String(r.model));
        setSupportsReasoning(!!r?.capabilities?.supports_reasoning);
        // Bump so ReasoningPicker re-reads the saved effort for the new model.
        setModelRefreshKey((k) => k + 1);
      })
      .catch(() => {
        // Best-effort: keep the last known label rather than blanking it.
      });
  }, [profile]);

  // Profile or PTY channel change tears down both WebSockets. Bump `version`
  // (same path as the manual Reconnect button) so the gateway client is
  // recreated and the events feed resubscribes — otherwise the old events
  // socket's close handler can leave a stale error banner after a switch.
  const scopeKey = `${channel}\0${profile ?? ""}`;
  const prevScopeKey = useRef<string | null>(null);
  useEffect(() => {
    if (prevScopeKey.current === null) {
      prevScopeKey.current = scopeKey;
      return;
    }
    if (prevScopeKey.current === scopeKey) return;
    prevScopeKey.current = scopeKey;
    setError(null);
    setVersion((v) => v + 1);
  }, [scopeKey]);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setInfo({});
      setError(null);
    });
    const offState = gw.onState((nextState) => {
      setState(nextState);
      if (nextState === "open") setError(null);
    });

    const offSessionInfo = gw.on<SessionInfo>("session.info", (ev) => {
      if (ev.payload) {
        setInfo((prev) => ({ ...prev, ...ev.payload }));
      }
    });

    const offError = gw.on<{ message?: string }>("error", (ev) => {
      const message = ev.payload?.message;

      if (message) {
        setError(message);
      }
    });

    // Create the sidecar session so the gateway surfaces session-scoped
    // signals (connection state, credential warnings). It's independent of the
    // PTY pane's session by design. The model picker no longer rides this
    // session — it writes config.yaml over REST — so we don't track its id.
    gw.connect()
      .then(() => {
        if (cancelled) {
          return;
        }
        // close_on_disconnect: the gateway reaps this sidecar session (and its
        // slash_worker subprocess) when the WS drops, instead of leaking it.
        return gw.request<{ session_id: string }>("session.create", {
          close_on_disconnect: true,
          source: "tool",
          ...(profile ? { profile } : {}),
        });
      })
      .catch((e: Error) => {
        if (!cancelled) {
          setError(e.message);
        }
      });

    return () => {
      cancelled = true;
      offState();
      offSessionInfo();
      offError();
      gw.close();
    };
    // `profile` is read from render; scope changes bump `version` → new `gw`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gw]);

  useEffect(() => {
    if (state === "open") return;
    if (state === "idle" || state === "connecting") return;
    if (navigator.onLine === false) return;
    const timer = window.setTimeout(
      () => setVersion((current) => current + 1),
      state === "error" ? 1500 : 3000,
    );
    return () => window.clearTimeout(timer);
  }, [state]);

  // Event subscriber WebSocket — receives the rebroadcast of every
  // dispatcher emit from the PTY child's gateway.  See /api/pub +
  // /api/events in hermes_cli/web_server.py for the broadcast hop.
  //
  // The passive tool-event feed owns its retry lifecycle. Reconnects mint a
  // fresh gated-mode ticket, pause while offline, and wake immediately when
  // iOS returns the WebView to the foreground.
  useEffect(() => {
    if (!channel) return;
    let stopped = false;
    let ws: WebSocket | null = null;
    let retryTimer: number | null = null;
    let retryAttempt = 0;

    const clearRetry = () => {
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      retryTimer = null;
    };

    const show = (nextState: EventFeedState, zh: string, en: string) => {
      if (stopped) return;
      setEventFeedState(nextState);
      setEventFeedDetail(isChinese ? zh : en);
    };

    const scheduleReconnect = () => {
      if (stopped) return;
      clearRetry();
      if (navigator.onLine === false) {
        show("waiting", "网络断开，恢复后自动连接", "Offline; reconnecting automatically");
        return;
      }
      retryAttempt += 1;
      const delay = Math.min(30000, 750 * 2 ** Math.min(retryAttempt - 1, 5));
      show(
        "connecting",
        `${Math.max(1, Math.ceil(delay / 1000))} 秒后自动重连`,
        `Reconnecting automatically in ${Math.max(1, Math.ceil(delay / 1000))}s`,
      );
      retryTimer = window.setTimeout(connect, delay);
    };

    const handleMessage = (ev: MessageEvent) => {
        let frame: RpcEnvelope;
        try {
          frame = JSON.parse(ev.data);
        } catch {
          return;
        }

        if (frame.method !== "event" || !frame.params) {
          return;
        }

        const { type, payload } = frame.params;

        if (type === "session.info") {
          const title = titleFromSessionInfoPayload(payload);
          if (title !== undefined) {
            onSessionTitleChange?.(title);
          }
        } else if (type === "dashboard.new_session_requested") {
          onDashboardNewSessionRequest?.();
        }
    };

    async function connect() {
      clearRetry();
      if (stopped) return;
      if (navigator.onLine === false) {
        scheduleReconnect();
        return;
      }
      show("connecting", "正在连接", "Connecting");
      try {
        const url = await buildWsUrl("/api/events", { channel });
        if (stopped) return;
        const socket = new WebSocket(url);
        ws = socket;
        socket.addEventListener("open", () => {
          if (stopped || ws !== socket) return;
          retryAttempt = 0;
          show("open", "已连接，工具调用实时同步", "Connected; tool calls are live");
        });
        socket.addEventListener("message", handleMessage);
        socket.addEventListener("close", (event) => {
          if (stopped || ws !== socket || event.code === 1000) return;
          ws = null;
          if (event.code === 4401 || event.code === 4403) {
            show("error", "连接凭证已失效，正在自动刷新", "Refreshing event credentials");
          } else {
            show("error", "连接中断，正在自动恢复", "Connection lost; recovering automatically");
          }
          scheduleReconnect();
        });
        socket.addEventListener("error", () => {
          if (!stopped && ws === socket) {
            show("error", "工具事件流暂时不可用", "Tool events are temporarily unavailable");
          }
        });
      } catch {
        show("error", "连接失败，正在自动恢复", "Connection failed; recovering automatically");
        scheduleReconnect();
      }
    }

    const wake = () => {
      if (stopped || ws?.readyState === WebSocket.OPEN) return;
      retryAttempt = 0;
      void connect();
    };
    const handleOffline = () => {
      clearRetry();
      show("waiting", "网络断开，恢复后自动连接", "Offline; reconnecting automatically");
      ws?.close(1000);
      ws = null;
    };
    const handleVisibility = () => {
      if (document.visibilityState === "visible") wake();
    };

    window.addEventListener("online", wake);
    window.addEventListener("offline", handleOffline);
    window.addEventListener("pageshow", wake);
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", handleVisibility);
    void connect();

    return () => {
      stopped = true;
      clearRetry();
      window.removeEventListener("online", wake);
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("pageshow", wake);
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", handleVisibility);
      ws?.close(1000);
    };
  }, [channel, isChinese, onDashboardNewSessionRequest, onSessionTitleChange, version]);

  // Seed the badge on mount and re-read it whenever the sockets are rebuilt
  // (a profile/channel switch bumps `version`).
  useEffect(() => {
    refreshEffectiveModel();
  }, [refreshEffectiveModel, version]);

  // The picker writes config.yaml over REST and reloads — it doesn't ride the
  // sidecar gateway session, so it's available whenever the sidebar is mounted.
  const modelName = effectiveModel || info.model || "—";
  const modelLabel = modelName.split("/").slice(-1)[0] ?? "—";
  const banner = error ?? info.credential_warning ?? null;

  return (
    <aside
      className={cn(
        "flex h-full w-full min-w-0 shrink-0 flex-col gap-3 overflow-y-auto overflow-x-hidden pr-1",
        className,
      )}
    >
      <Button
        outlined
        className="w-full justify-start"
        prefix={<MessageSquarePlus />}
        onClick={onDashboardNewSessionRequest}
      >
        {isChinese ? "新建对话" : "New chat"}
      </Button>

      <Card className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-2">
        <div className="min-w-0">
          <div className="text-display text-xs tracking-wider text-text-tertiary">
            {isChinese ? "模型" : "model"}
          </div>

          <Button
            ghost
            size="sm"
            onClick={() => setModelOpen(true)}
            className={cn(
              "w-full min-w-0 justify-start px-0 py-0",
              "normal-case tracking-normal text-sm font-medium",
              "hover:underline disabled:no-underline",
            )}
            title={
              modelName === "—"
                ? isChinese
                  ? "切换模型"
                  : "switch model"
                : modelName
            }
          >
            <span className="grid w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-1">
              <span className="min-w-0 truncate text-left">{modelLabel}</span>

              <ChevronDown className="size-3.5 shrink-0 text-text-secondary" />
            </span>
          </Button>
        </div>

        <Badge tone={STATE_TONE[state]} className="shrink-0">
          {stateLabel[state]}
        </Badge>
      </Card>

      <Card className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-2">
        <div className="min-w-0">
          <div className="text-display text-xs tracking-wider text-text-tertiary">
            {isChinese ? "工具事件流" : "Tool events"}
          </div>
          <div className="mt-0.5 truncate text-xs text-text-secondary" title={eventFeedDetail}>
            {eventFeedDetail}
          </div>
        </div>
        <Badge
          tone={
            eventFeedState === "open"
              ? "success"
              : eventFeedState === "error"
                ? "destructive"
                : "warning"
          }
          className="shrink-0"
        >
          {eventFeedState === "open"
            ? isChinese ? "在线" : "Live"
            : eventFeedState === "waiting"
              ? isChinese ? "等待网络" : "Waiting"
              : eventFeedState === "error"
                ? isChinese ? "恢复中" : "Recovering"
                : isChinese ? "连接中" : "Connecting"}
        </Badge>
      </Card>

      {supportsReasoning && (
        <Card className="py-0">
          <ReasoningPicker
            currentModel={modelName}
            profile={profile}
            refreshKey={modelRefreshKey}
            onChanged={(effort) =>
              setModelNotice(
                isChinese
                  ? `推理强度已设为 ${effort}。执行 /new 或刷新页面后应用到当前对话。`
                  : `Reasoning effort set to ${effort}. Run /new or refresh the page to apply it to this chat.`,
              )
            }
          />
        </Card>
      )}

      {modelNotice && (
        <Card className="flex items-start gap-2 border-warning/40 bg-warning/5 px-3 py-2 text-xs">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />

          <div className="wrap-break-word min-w-0 flex-1 text-text-secondary">
            {modelNotice}
          </div>
        </Card>
      )}

      {banner && (
        <Card className="flex items-start gap-2 border-destructive/40 bg-destructive/5 px-3 py-2 text-xs">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />

          <div className="wrap-break-word min-w-0 flex-1 text-destructive">{banner}</div>
        </Card>
      )}

      {modelOpen && (
        <ModelPickerDialog
          // Same path the Models page uses (REST /api/model/set), not the
          // sidecar config.set RPC, which didn't reliably land in the
          // config.yaml the agent boots from. Always persisted (alwaysGlobal).
          loader={() => api.getModelOptions(profile)}
          alwaysGlobal
          onApply={async ({ provider, model, confirmExpensiveModel }) => {
            setModelNotice(null);
            setPendingReloadModel(null);
            const result = await api.setModelAssignment(
              {
                confirm_expensive_model: confirmExpensiveModel,
                scope: "main",
                provider,
                model,
              },
              profile,
            );
            // confirm_required => the dialog shows the expensive-model prompt
            // and calls back; don't announce until the user confirms.
            if (!result.confirm_required) {
              refreshEffectiveModel();
              // Ask before reloading: applying the model starts a fresh chat.
              setPendingReloadModel(model.split("/").slice(-1)[0]);
            }
            return result;
          }}
          onClose={() => {
            setModelOpen(false);
            refreshEffectiveModel();
          }}
        />
      )}

      <ModelReloadConfirm
        model={pendingReloadModel}
        onCancel={() => {
          const m = pendingReloadModel;
          setPendingReloadModel(null);
          setModelNotice(
            isChinese
              ? `模型已切换为 ${m}。执行 /new 或刷新页面后应用到当前对话。`
              : `Model set to ${m}. Run /new or refresh the page to apply it to this chat.`,
          );
        }}
      />
    </aside>
  );
}

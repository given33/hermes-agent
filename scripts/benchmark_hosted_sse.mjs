import { spawn } from 'node:child_process';

const baseUrl = process.env.HERMES_BENCHMARK_URL || 'https://daxueshenmai.top';
const username = process.env.HERMES_BENCHMARK_USERNAME || '';
const password = process.env.HERMES_BENCHMARK_PASSWORD || '';
const timeoutMs = Number(process.env.HERMES_BENCHMARK_TIMEOUT_MS || 90_000);
const requestTimeoutMs = Number(process.env.HERMES_BENCHMARK_REQUEST_TIMEOUT_MS || 15_000);
const skipSse = process.env.HERMES_BENCHMARK_SKIP_SSE === '1';
const prompt = process.env.HERMES_BENCHMARK_PROMPT || '请用一句话解释什么是 SSE。';
const keepConversation = process.env.HERMES_BENCHMARK_KEEP === '1';
const preEnqueueWaitMs = Number(process.env.HERMES_BENCHMARK_PRE_ENQUEUE_WAIT_MS || 150);

if (!username || !password) {
  throw new Error('HERMES_BENCHMARK_USERNAME and HERMES_BENCHMARK_PASSWORD are required');
}

const startedAt = performance.now();
const elapsed = () => ((performance.now() - startedAt) / 1000).toFixed(3);
let accessToken = '';
let refreshToken = '';
let conversationId = '';

async function jsonRequest(path, options = {}) {
  const requestStartedAt = performance.now();
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    signal: options.signal || AbortSignal.timeout(requestTimeoutMs),
    headers: {
      'content-type': 'application/json',
      ...(accessToken ? { authorization: `Bearer ${accessToken}` } : {}),
      ...(options.headers || {}),
    },
  });
  const raw = await response.text();
  if (!response.ok) {
    throw new Error(`${path} ${response.status}: ${raw.slice(0, 240)}`);
  }
  return {
    data: raw ? JSON.parse(raw) : {},
    duration: (performance.now() - requestStartedAt) / 1000,
  };
}

function log(...parts) {
  console.log(elapsed(), ...parts);
}

async function run() {
  const login = await jsonRequest('/auth/mobile/token', {
    method: 'POST',
    body: JSON.stringify({
      username,
      password,
      device: {
        device_id: `codex-sse-${Date.now()}`,
        name: 'Codex SSE timing',
        platform: 'ios',
      },
    }),
  });
  accessToken = login.data.access_token;
  refreshToken = login.data.refresh_token;
  const accountGeneration = login.data.account.account_generation;
  log('LOGIN', login.duration.toFixed(3));

  const requestedConversationId = `chat_codex_sse_${Date.now()}`;
  const created = await jsonRequest('/api/plugins/collaboration/single/conversations', {
    method: 'POST',
    body: JSON.stringify({
      profile: 'default',
      client_id: requestedConversationId,
      title: 'SSE timing',
    }),
  });
  conversationId = created.data.conversation.id;
  log('CREATE', created.duration.toFixed(3), conversationId);

  const streamController = new AbortController();
  let resolveTerminal;
  const terminal = new Promise((resolve) => {
    resolveTerminal = resolve;
  });
  const seenEventTypes = new Set();
  let lastSnapshotKey = '';
  const decoder = new TextDecoder();
  let streamBuffer = '';
  const consumeStreamChunk = (chunk) => {
    streamBuffer += decoder.decode(chunk, { stream: true });
    while (true) {
      const boundary = /\r?\n\r?\n/.exec(streamBuffer);
      if (!boundary) break;
      const frame = streamBuffer.slice(0, boundary.index);
      streamBuffer = streamBuffer.slice(boundary.index + boundary[0].length);
      const data = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n');
      if (!data) continue;
      const envelope = JSON.parse(data);
      const conversation = envelope.conversation;
      if (conversation && typeof conversation === 'object') {
        const turns = conversation.hosted_turns || {};
        const latestTurn = Object.values(turns)
          .filter((item) => item && typeof item === 'object')
          .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))[0];
        const chatRole = latestTurn?.role_events?.chat;
        const latestMessage = Array.isArray(conversation.messages)
          ? conversation.messages[conversation.messages.length - 1]
          : null;
        const snapshotKey = JSON.stringify({
          turn: latestTurn?.id || latestTurn?.turn_id || '',
          status: latestTurn?.status || '',
          stage: latestTurn?.stage || '',
          roleStatus: chatRole?.status || '',
          firstTokenAt: chatRole?.first_token_at || 0,
          content: String(latestMessage?.content || '').slice(0, 120),
          messageStatus: latestMessage?.status || '',
        });
        if (snapshotKey !== lastSnapshotKey) {
          lastSnapshotKey = snapshotKey;
          log(
            'SNAPSHOT',
            `status=${latestTurn?.status || ''}`,
            `role=${chatRole?.status || ''}`,
            `first_token=${chatRole?.first_token_at || 0}`,
            `message=${latestMessage?.status || ''}`,
            latestMessage?.content ? JSON.stringify(String(latestMessage.content).slice(0, 60)) : '',
          );
        }
      }
      for (const event of envelope.events || []) {
        if (!seenEventTypes.has(event.event_type)) {
          seenEventTypes.add(event.event_type);
          const delta = event.payload?.delta ?? event.payload?.text;
          const eventTime = Number(event.occurred_at || event.created_at || 0);
          const eventAge = eventTime ? ((Date.now() - eventTime) / 1000).toFixed(3) : '';
          log(
            'EVENT',
            event.event_type,
            delta ? JSON.stringify(String(delta).slice(0, 60)) : '',
            event.entity_id ? `entity=${event.entity_id}` : '',
            eventAge ? `age=${eventAge}s` : '',
          );
        }
        if (['turn.completed', 'turn.failed', 'turn.cancelled'].includes(event.event_type)) {
          log('TERMINAL_DETAIL', JSON.stringify(event.payload || {}).slice(0, 1200));
          resolveTerminal(event.event_type);
        }
      }
    }
  };
  log(skipSse ? 'SSE_SKIPPED' : 'SSE_OPENING');
  const stream = skipSse ? Promise.resolve() : new Promise((resolve) => {
    const executable = process.platform === 'win32' ? 'curl.exe' : 'curl';
    const streamUrl = `${baseUrl}/api/plugins/collaboration/single/conversations/`
      + `${encodeURIComponent(conversationId)}/hosted-events?cursor=0`
      + `&expected_account_generation=${encodeURIComponent(accountGeneration)}`;
    const child = spawn(executable, [
      '--no-buffer',
      '--silent',
      '--show-error',
      '--header',
      'Accept: text/event-stream',
      '--header',
      `Authorization: Bearer ${accessToken}`,
      streamUrl,
    ], { windowsHide: true });
    let stderr = '';
    child.stdout.on('data', (chunk) => consumeStreamChunk(chunk));
    child.stderr.on('data', (chunk) => { stderr += String(chunk); });
    child.once('spawn', () => log('SSE_PROCESS_READY'));
    child.once('close', (code) => {
      if (!streamController.signal.aborted && code !== 0) {
        log('SSE_ERROR', stderr.trim() || `curl exited ${code}`);
      }
      resolve();
    });
    streamController.signal.addEventListener('abort', () => {
      child.kill();
    }, { once: true });
  });

  await new Promise((resolve) => setTimeout(resolve, preEnqueueWaitMs));
  const now = Date.now();
  const messageId = `user_${now}`;
  const turnId = `turn_${now}`;
  log('ENQUEUE_START');
  const enqueued = await jsonRequest(
    `/api/plugins/collaboration/single/conversations/${encodeURIComponent(conversationId)}/enqueue`,
    {
      method: 'POST',
      body: JSON.stringify({
        request_id: messageId,
        turn_id: turnId,
        message: {
          id: messageId,
          role: 'user',
          name: 'You',
          content: prompt,
          kind: 'message',
          status: 'completed',
          created_at: now,
        },
        recent_messages: [],
        profiles: [],
        attachment_ids: [],
        attachment_context: '',
        delivery_context: '',
      }),
    },
  );
  log('ENQUEUE', enqueued.duration.toFixed(3), enqueued.data.accepted);

  const outcome = skipSse ? 'enqueue-only' : await Promise.race([
      terminal,
      new Promise((resolve) => setTimeout(() => resolve('timeout'), timeoutMs)),
    ]);
  log('OUTCOME', outcome);
  streamController.abort();
  await stream;
}

try {
  await run();
} catch (error) {
  log('FATAL', error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  try {
    if (!keepConversation && conversationId && accessToken) {
      await fetch(
        `${baseUrl}/api/plugins/collaboration/single/conversations/`
          + encodeURIComponent(conversationId),
        {
          method: 'DELETE',
          headers: { authorization: `Bearer ${accessToken}` },
          signal: AbortSignal.timeout(requestTimeoutMs),
        },
      );
      log('CLEAN_CONVERSATION');
    }
  } catch (error) {
    log('CLEAN_CONVERSATION_ERROR', error instanceof Error ? error.message : String(error));
  }
  try {
    if (accessToken) {
      await fetch(`${baseUrl}/auth/mobile/logout`, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${accessToken}`,
          'content-type': 'application/json',
        },
        body: JSON.stringify({ refresh_token: refreshToken }),
        signal: AbortSignal.timeout(requestTimeoutMs),
      });
      log('CLEAN_SESSION');
    }
  } catch (error) {
    log('CLEAN_SESSION_ERROR', error instanceof Error ? error.message : String(error));
  }
}

export const PENDING_UNIFIED_SESSION_KEY =
  "hermes.unified.pendingStoredSession";

type SessionResumeStorage = Pick<Storage, "setItem">;

export function queueUnifiedSessionResume(
  storage: SessionResumeStorage,
  sessionId: string,
): boolean {
  const normalized = sessionId.trim();
  if (!normalized) return false;
  storage.setItem(PENDING_UNIFIED_SESSION_KEY, normalized);
  return true;
}

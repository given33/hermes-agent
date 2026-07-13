import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Mondwest font only — use on layout shells; do not force normal-case here or `text-display` chrome (Segmented, badges) stops uppercasing. */
export const themedFont = "font-mondwest";

/** Mondwest body copy — sentence-case themed text (not uppercase chrome). */
export const themedBody = "font-mondwest normal-case";

/** Mondwest brand chrome — uppercase section headers and nav labels. */
export const themedChrome = "font-mondwest text-display";

function localizedRelativeTime(
  deltaSeconds: number,
  locale?: string,
): string | null {
  if (!locale?.toLowerCase().startsWith("zh")) return null;

  if (deltaSeconds < 60) return "刚刚";

  const formatter = new Intl.RelativeTimeFormat(locale, {
    numeric: "always",
    style: "long",
  });
  if (deltaSeconds < 3600) {
    return formatter.format(-Math.floor(deltaSeconds / 60), "minute");
  }
  if (deltaSeconds < 86400) {
    return formatter.format(-Math.floor(deltaSeconds / 3600), "hour");
  }
  return formatter.format(-Math.floor(deltaSeconds / 86400), "day");
}

/** Relative time from a Unix epoch timestamp (seconds). */
export function timeAgo(ts: number, locale?: string): string {
  const delta = Date.now() / 1000 - ts;
  const localized = localizedRelativeTime(delta, locale);
  if (localized) return localized;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  if (delta < 172800) return "yesterday";
  return `${Math.floor(delta / 86400)}d ago`;
}

/** Relative time from an ISO-8601 timestamp string. */
export function isoTimeAgo(iso: string, locale?: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0 || Number.isNaN(delta)) {
    return locale?.toLowerCase().startsWith("zh") ? "未知" : "unknown";
  }
  const localized = localizedRelativeTime(delta, locale);
  if (localized) return localized;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

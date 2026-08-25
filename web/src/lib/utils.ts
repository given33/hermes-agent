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

type TimeLocale = "en" | "zh";

const TIME_LABELS: Record<TimeLocale, Record<"now" | "minutes" | "hours" | "yesterday" | "days" | "unknown", (value?: number) => string>> = {
  en: {
    now: () => "just now",
    minutes: (value = 0) => `${value}m ago`,
    hours: (value = 0) => `${value}h ago`,
    yesterday: () => "yesterday",
    days: (value = 0) => `${value}d ago`,
    unknown: () => "unknown",
  },
  zh: {
    now: () => "刚刚",
    minutes: (value = 0) => `${value}分钟前`,
    hours: (value = 0) => `${value}小时前`,
    yesterday: () => "昨天",
    days: (value = 0) => `${value}天前`,
    unknown: () => "未知",
  },
};

function timeLabels(locale?: string): (typeof TIME_LABELS)[TimeLocale] {
  return TIME_LABELS[locale === "zh" ? "zh" : "en"];
}

/** Relative time from a Unix epoch timestamp (seconds). */
export function timeAgo(ts: number, locale?: string): string {
  const delta = Date.now() / 1000 - ts;
  const labels = timeLabels(locale);
  if (Number.isNaN(delta) || delta < 0) return labels.unknown();
  if (delta < 60) return labels.now();
  if (delta < 3600) return labels.minutes(Math.floor(delta / 60));
  if (delta < 86400) return labels.hours(Math.floor(delta / 3600));
  if (delta < 172800) return labels.yesterday();
  return labels.days(Math.floor(delta / 86400));
}

/** Relative time from an ISO-8601 timestamp string. */
export function isoTimeAgo(iso: string, locale?: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  return timeAgo(Date.now() / 1000 - delta, locale);
}

import { useEffect } from "react";

import { useI18n } from "@/i18n";
import { translateDashboardText } from "@/i18n/dashboard-zh-compat";

const SKIP_SELECTOR = [
  "pre",
  "code",
  "kbd",
  "samp",
  ".xterm",
  ".xterm-screen",
  ".hc-message-body",
  "[data-no-auto-translate]",
].join(",");

const originalText = new WeakMap<Text, string>();
const translatedText = new WeakMap<Text, string>();
const originalAttributes = new WeakMap<Element, Map<string, string>>();

function translateTextNode(node: Text) {
  const parent = node.parentElement;
  if (!parent || parent.closest(SKIP_SELECTOR)) return;
  const current = node.data;
  const previousTranslation = translatedText.get(node);
  if (previousTranslation !== current) originalText.set(node, current);
  const original = originalText.get(node) ?? current;
  const trimmed = original.trim();
  if (!trimmed) return;
  const translated = translateDashboardText(trimmed, parent.textContent ?? "");
  if (translated === trimmed) return;
  const next = original.replace(trimmed, translated);
  translatedText.set(node, next);
  if (node.data !== next) node.data = next;
}

function translateAttribute(element: Element, name: string) {
  const current = element.getAttribute(name);
  if (!current) return;
  let attributes = originalAttributes.get(element);
  if (!attributes) {
    attributes = new Map();
    originalAttributes.set(element, attributes);
  }
  if (!attributes.has(name)) attributes.set(name, current);
  const original = attributes.get(name) ?? current;
  const translated = translateDashboardText(original);
  if (translated !== original && current !== translated) {
    element.setAttribute(name, translated);
  }
}

function translateTree(root: Node) {
  if (root.nodeType === Node.TEXT_NODE) {
    translateTextNode(root as Text);
    return;
  }
  if (!(root instanceof Element) && root !== document.body) return;
  const element = root instanceof Element ? root : null;
  if (element?.matches(SKIP_SELECTOR)) return;
  if (element) {
    translateAttribute(element, "placeholder");
    translateAttribute(element, "title");
    translateAttribute(element, "aria-label");
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    translateTextNode(node as Text);
    node = walker.nextNode();
  }
  if (root instanceof Element || root === document.body) {
    const container = root as ParentNode;
    container
      .querySelectorAll("[placeholder],[title],[aria-label]")
      .forEach((child) => {
        translateAttribute(child, "placeholder");
        translateAttribute(child, "title");
        translateAttribute(child, "aria-label");
      });
  }
}

function restoreTree() {
  document.querySelectorAll("*").forEach((element) => {
    const attributes = originalAttributes.get(element);
    attributes?.forEach((value, name) => element.setAttribute(name, value));
  });
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    const original = originalText.get(node as Text);
    if (original !== undefined) (node as Text).data = original;
    node = walker.nextNode();
  }
}

export function DashboardChineseCompat() {
  const { locale } = useI18n();

  useEffect(() => {
    if (!locale.toLowerCase().startsWith("zh")) {
      restoreTree();
      return;
    }

    translateTree(document.body);
    const observer = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => {
        if (mutation.type === "characterData") {
          translateTextNode(mutation.target as Text);
          return;
        }
        if (mutation.type === "attributes") {
          translateAttribute(
            mutation.target as Element,
            mutation.attributeName ?? "",
          );
          return;
        }
        mutation.addedNodes.forEach(translateTree);
      });
    });
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ["placeholder", "title", "aria-label"],
      characterData: true,
      childList: true,
      subtree: true,
    });
    return () => observer.disconnect();
  }, [locale]);

  return null;
}

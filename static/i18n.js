"use strict";
// English / Tamil. The Tamil text lives in /static/i18n/ta.json (shared with the server), keyed by the
// English text. Two ways in:
//   * t("{n} new transactions", {n: 3}) for sentences the page builds;
//   * everything else (menus, headings, buttons, category names) is translated where it appears: a
//     MutationObserver swaps any text that exactly matches a dictionary entry, including text added later.
// Payee names, narrations and people's names are never touched.

const I18N = { lang: "en", locale: "en-IN", ta: {} };
const I18N_SKIP = "script, style, textarea, .payee, .narr, .mono, .person, #who-name, [data-noi18n]";
const I18N_ATTRS = ["placeholder", "aria-label", "title"];
const LANG_KEY = "et.lang";

function t(en, params = {}) {
  const template = I18N.lang === "ta" ? (I18N.ta[en] ?? en) : en;
  return template.replace(/\{(\w+)\}/g, (m, k) => (k in params ? params[k] : m));
}
// A label or category name on its own.
const tc = (name) => (I18N.lang === "ta" ? (I18N.ta[name] ?? name) : name);

function storedLang() {
  try { return localStorage.getItem(LANG_KEY) || "en"; } catch { return "en"; }
}

async function setLanguage(lang) {
  I18N.lang = lang === "ta" ? "ta" : "en";
  I18N.locale = I18N.lang === "ta" ? "ta-IN" : "en-IN";
  document.documentElement.lang = I18N.lang;
  try { localStorage.setItem(LANG_KEY, I18N.lang); } catch { /* private mode */ }
  if (I18N.lang !== "ta") return;
  try {
    I18N.ta = await (await fetch(`/static/i18n/ta.json?v=${document.documentElement.dataset.v || ""}`)).json();
  } catch { I18N.ta = {}; return; }
  translateTree(document.body);
  new MutationObserver((changes) => {
    for (const c of changes) {
      if (c.type === "characterData") translateTree(c.target);
      else c.addedNodes.forEach(translateTree);
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
}

function translateText(value) {
  // Keep surrounding spaces, and the "↳ " that marks a sub-category in drop-downs.
  const m = value.match(/^([\s ]*)(↳\s*)?([\s\S]*?)(\s*)$/);
  const hit = m && m[3] && I18N.ta[m[3]];
  return hit ? m[1] + (m[2] || "") + hit + m[4] : null;
}

function translateTree(node) {
  if (I18N.lang !== "ta" || !node) return;
  if (node.nodeType === Node.TEXT_NODE) {
    const parent = node.parentElement;
    if (!parent || parent.closest(I18N_SKIP)) return;
    const v = translateText(node.nodeValue);
    if (v !== null && v !== node.nodeValue) node.nodeValue = v;
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE || node.closest(I18N_SKIP)) return;
  for (const a of I18N_ATTRS) {
    const v = node.getAttribute(a);
    if (v && I18N.ta[v]) node.setAttribute(a, I18N.ta[v]);
  }
  node.childNodes.forEach(translateTree);
}

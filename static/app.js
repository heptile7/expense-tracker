"use strict";

// ── Helpers ──────────────────────────────────────────────────────────────────
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const state = { meta: null, txnOffset: 0, txnMerchant: null, txnMerchantLabel: "", rulesSource: "user", dash: null };
const PAGE = 100;

const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-IN", { notation: "compact", maximumFractionDigits: 1 });
const money = (v, whole = false) => (whole ? inr0 : inr).format(v || 0);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
const monthLabel = (m) => { const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleDateString("en-IN", { month: "long", year: "numeric" }); };
const monthShort = (m) => { const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleDateString("en-IN", { month: "short" }); };
const plural = (n, word) => `${n.toLocaleString("en-IN")} ${word}${n === 1 ? "" : "s"}`;

async function api(method, url, body, isForm = false) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join("; ") : data?.detail;
    const err = new Error(data?.error || detail || res.statusText);
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
const fail = (err) => toast(err.message || String(err), 6000);

function showTip(html, x, y) {
  const tip = $("#tooltip");
  tip.innerHTML = html; tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
  tip.style.left = `${Math.max(8, left)}px`; tip.style.top = `${Math.max(8, top)}px`;
}
const hideTip = () => { $("#tooltip").hidden = true; };

function ask(dialog) {
  return new Promise((resolve) => {
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue || "cancel"), { once: true });
    dialog.showModal();
  });
}

// ── Meta & selects ───────────────────────────────────────────────────────────
const KIND_LABEL = { expense: "Expenses", income: "Income", transfer: "Transfers (not counted)" };

function categoryOptions(selectedId, { includeBlank = false, blankLabel = "" } = {}) {
  const cats = state.meta?.categories || [];
  let html = includeBlank ? `<option value="">${esc(blankLabel)}</option>` : "";
  for (const kind of ["expense", "income", "transfer"]) {
    const group = cats.filter((c) => c.kind === kind && (c.is_active || c.id === selectedId));
    if (!group.length) continue;
    html += `<optgroup label="${KIND_LABEL[kind]}">`;
    for (const c of group) html += `<option value="${c.id}"${c.id === selectedId ? " selected" : ""}>${esc(c.name)}</option>`;
    html += "</optgroup>";
  }
  return html;
}

function fillSelect(sel, options, keepFirst = true) {
  const current = sel.value;
  const first = keepFirst ? sel.options[0]?.outerHTML || "" : "";
  sel.innerHTML = first + options;
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

async function loadMeta() {
  state.meta = await api("GET", "/api/meta");
  const { accounts, months } = state.meta;
  const acctOpts = accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  const monthOpts = months.map((m) => `<option value="${m}">${monthLabel(m)}</option>`).join("");
  ["#dash-account", "#f-account", "#imp-account"].forEach((s) => fillSelect($(s), acctOpts));
  fillSelect($("#f-month"), monthOpts);
  fillSelect($("#dash-month"), monthOpts, false);
  fillSelect($("#f-category"), categoryOptions(null));
  $("#rf-cat").innerHTML = categoryOptions(null);
}

async function refreshBadge() {
  try {
    const { total } = await api("GET", "/api/transactions?needs_review=true&limit=1");
    const b = $("#review-badge");
    b.textContent = total; b.hidden = !total;
  } catch { /* ignore */ }
}

// ── Tabs ─────────────────────────────────────────────────────────────────────
const LOADERS = {
  dashboard: () => loadDashboard(),
  transactions: () => loadTxns(true),
  review: () => loadReview(),
  import: () => loadStatements(),
  rules: () => loadRules(),
  categories: () => loadCategories(),
  settings: () => Promise.all([loadAccounts(), loadGmail(), loadUsage()]),
};

function showTab(name) {
  if (!LOADERS[name]) name = "dashboard";
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  hideTip();
  LOADERS[name]().catch(fail);
}
const currentTab = () => location.hash.slice(1) || "dashboard";
const reloadCurrent = () => LOADERS[currentTab()]?.().catch(fail);

// ── Dashboard ────────────────────────────────────────────────────────────────
async function loadDashboard() {
  const params = new URLSearchParams();
  if ($("#dash-month").value) params.set("month", $("#dash-month").value);
  if ($("#dash-account").value) params.set("account_id", $("#dash-account").value);
  const d = await api("GET", `/api/dashboard?${params}`);
  state.dash = d;
  const empty = !d.months.length;
  $("#dash-empty").hidden = !empty;
  $("#dash-body").hidden = empty;
  if (empty) return;
  if (!$("#dash-month").value) $("#dash-month").value = d.month;

  $("#t-spent").textContent = money(d.total_expense, true);
  $("#t-spent-sub").textContent = monthLabel(d.month);
  $("#t-income").textContent = money(d.total_income, true);
  $("#t-net").textContent = `${d.net < 0 ? "−" : ""}${money(Math.abs(d.net), true)}`;
  $("#t-transfers").textContent = money(d.transfers_out, true);

  const catIds = Object.fromEntries((state.meta?.categories || []).map((c) => [c.name, c.id]));
  renderHBars($("#chart-categories"), d.by_category, d.total_expense, (row) => {
    openTxns({ month: d.month, category_id: catIds[row.label] || "" });
  });
  renderHBars($("#chart-merchants"), d.top_merchants, d.total_expense, (row) => {
    openTxns({ month: d.month, q: row.label });
  });
  renderTrend($("#chart-trend"), d.trend);
  const review = d.needs_review ? ` ${plural(d.needs_review, "transaction")} need a look in Review.` : "";
  const claude = state.meta?.claude_key_set
    ? `Claude (${state.meta.model}) has categorized new payees in ${plural(d.llm_calls, "call")}, costing $${d.llm_cost_usd.toFixed(4)} so far.`
    : "Claude isn't set up yet (add ANTHROPIC_API_KEY to .env), so new payees stay Uncategorized until you set them.";
  $("#dash-foot").textContent = `Spending is net of refunds booked to the same category. ${claude}${review}`;
}

function renderHBars(el, rows, total, onClick) {
  if (!rows.length) { el.innerHTML = `<div class="none">Nothing spent this month</div>`; return; }
  const max = Math.max(...rows.map((r) => r.amount));
  el.innerHTML = rows.map((r, i) => `
    <button class="hbar" data-i="${i}" aria-label="${esc(r.label)}: ${money(r.amount)}">
      <span class="hbar-label">${esc(r.label)}</span>
      <span class="hbar-track"><span class="hbar-fill" style="width: calc((100% - 92px) * ${(r.amount / max).toFixed(4)})"></span>
      <span class="hbar-value">${money(r.amount, true)}</span></span>
    </button>`).join("");
  $$(".hbar", el).forEach((b) => {
    const r = rows[+b.dataset.i];
    b.addEventListener("click", () => onClick(r));
    b.addEventListener("mousemove", (e) => showTip(
      `<div class="tt-title">${esc(r.label)}</div>
       <div class="tt-row">Spent<b>${money(r.amount)}</b></div>
       <div class="tt-row">Share of month<b>${total ? Math.round((r.amount / total) * 100) : 0}%</b></div>
       <div class="tt-row">Transactions<b>${r.count}</b></div>`, e.clientX, e.clientY));
    b.addEventListener("mouseleave", hideTip);
  });
}

function niceMax(v) {
  if (v <= 0) return 1000;
  const p = 10 ** Math.floor(Math.log10(v));
  const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}

function roundedTop(x, y, w, h, r = 4) {
  if (h <= 0) return "";
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function renderTrend(el, trend) {
  const W = Math.max(el.clientWidth, 300), H = 240;
  const m = { l: 52, r: 8, t: 12, b: 28 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = niceMax(Math.max(...trend.map((t) => Math.max(t.expense, t.income))));
  const y = (v) => m.t + ih - (v / max) * ih;
  const band = iw / trend.length;
  const colW = Math.min(24, Math.max(4, (band - 10) / 2 - 1));
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Spent and income per month for the last 12 months">`;
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i, yy = y(v);
    svg += `<line class="${i ? "gridline" : "baseline"}" x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}"/>`;
    svg += `<text class="tick" x="${m.l - 8}" y="${yy + 4}" text-anchor="end">${v === 0 ? "0" : compact.format(v)}</text>`;
  }
  trend.forEach((t, i) => {
    const cx = m.l + band * i + band / 2;
    svg += `<path class="col-1" d="${roundedTop(cx - colW - 1, y(t.expense), colW, m.t + ih - y(t.expense))}"/>`;
    svg += `<path class="col-2" d="${roundedTop(cx + 1, y(t.income), colW, m.t + ih - y(t.income))}"/>`;
    const showLabel = trend.length <= 12 && (band > 34 || i % 2 === 0);
    if (showLabel) svg += `<text class="tick" x="${cx}" y="${H - 8}" text-anchor="middle">${monthShort(t.month)}</text>`;
    svg += `<rect class="hit" data-i="${i}" x="${m.l + band * i}" y="${m.t}" width="${band}" height="${ih}" rx="4"/>`;
  });
  el.innerHTML = svg + "</svg>";
  $$(".hit", el).forEach((r) => {
    const t = trend[+r.dataset.i];
    r.addEventListener("mousemove", (e) => showTip(
      `<div class="tt-title">${monthLabel(t.month)}</div>
       <div class="tt-row"><span class="legend"><span class="key s1"></span></span>Spent<b>${money(t.expense, true)}</b></div>
       <div class="tt-row"><span class="legend"><span class="key s2"></span></span>Income<b>${money(t.income, true)}</b></div>`, e.clientX, e.clientY));
    r.addEventListener("mouseleave", hideTip);
    r.addEventListener("click", () => { $("#dash-month").value = t.month; if ($("#dash-month").value === t.month) loadDashboard().catch(fail); });
  });
}

// ── Transactions ─────────────────────────────────────────────────────────────
const HOW = {
  manual: () => "You, one-off",
  rule: () => "Your rule",
  seed: () => "Built-in rule",
  llm: (t) => `Claude · ${Math.round((t.confidence || 0) * 100)}%`,
  transfer: () => "Transfer detection",
  fallback: () => "Not yet",
};

function txnRow(t, withCheckbox = false) {
  const sign = t.direction === "debit" ? "−" : "+";
  return `<tr data-id="${t.id}">
    ${withCheckbox ? `<td class="cb"><input type="checkbox" class="rv-cb" value="${t.id}" aria-label="Select"></td>` : ""}
    <td class="date">${fmtDate(t.txn_date)}</td>
    <td><div class="payee" data-open="${t.id}" tabindex="0">${esc(t.counterparty || "Unknown")}</div>
        <div class="narr" title="${esc(t.narration_raw)}">${esc(t.narration_raw)}</div></td>
    <td class="num ${t.direction === "credit" ? "amt-credit" : ""}">${sign}${money(t.amount)}<div class="acct">${esc(t.account_name)}</div></td>
    <td><select class="cat-select" data-id="${t.id}" data-current="${t.category_id}" aria-label="Category">${categoryOptions(t.category_id)}</select></td>
    <td class="how">${t.needs_review ? '<span class="dot" title="Needs review"></span>' : ""}${(HOW[t.category_source] || (() => ""))(t)}</td>
  </tr>`;
}

function txnParams() {
  const p = new URLSearchParams();
  const v = (id) => $(id).value;
  if (v("#f-q")) p.set("q", v("#f-q"));
  if (v("#f-month")) p.set("month", v("#f-month"));
  if (v("#f-account")) p.set("account_id", v("#f-account"));
  if (v("#f-category")) p.set("category_id", v("#f-category"));
  if (v("#f-source")) p.set("source", v("#f-source"));
  if ($("#f-review").checked) p.set("needs_review", "true");
  if (state.txnMerchant) p.set("merchant_key", state.txnMerchant);
  return p;
}

async function loadTxns(reset = true) {
  if (reset) state.txnOffset = 0;
  const p = txnParams();
  p.set("limit", PAGE); p.set("offset", state.txnOffset);
  const data = await api("GET", `/api/transactions?${p}`);
  const body = $("#txn-body");
  const html = data.items.map((t) => txnRow(t)).join("");
  body.innerHTML = reset ? html : body.innerHTML + html;
  if (!data.total) body.innerHTML = `<tr><td colspan="5" class="empty">No transactions match.</td></tr>`;
  state.txnOffset += data.items.length;
  $("#txn-more").hidden = state.txnOffset >= data.total;
  const chip = state.txnMerchant ? ` · payee <span class="pill">${esc(state.txnMerchantLabel)} <button class="link" id="clear-merchant" aria-label="Clear payee filter">✕</button></span>` : "";
  $("#txn-summary").innerHTML = `${plural(data.total, "transaction")}${chip}`;
  $("#clear-merchant")?.addEventListener("click", () => { state.txnMerchant = null; loadTxns(true).catch(fail); });
}

function openTxns(filters = {}) {
  $("#f-q").value = filters.q || "";
  $("#f-month").value = filters.month || "";
  $("#f-account").value = filters.account_id || "";
  $("#f-category").value = filters.category_id || "";
  $("#f-source").value = filters.source || "";
  $("#f-review").checked = !!filters.needs_review;
  state.txnMerchant = filters.merchant_key || null;
  state.txnMerchantLabel = filters.merchant_label || "";
  showTab("transactions");
}

async function onCategoryChange(sel) {
  const id = +sel.dataset.id, prev = sel.dataset.current, catId = +sel.value;
  const catName = sel.options[sel.selectedIndex].text;
  try {
    const t = await api("GET", `/api/transactions/${id}`);
    const payee = t.counterparty || "this payee";
    $("#dc-text").textContent = `${payee} → ${catName}`;
    $("#dc-merchant").textContent = t.similar_count
      ? `All ${t.similar_count + 1} from ${payee}, and future ones`
      : `This one and future payments to ${payee}`;
    const scope = await ask($("#dlg-category"));
    if (scope !== "merchant" && scope !== "single") { sel.value = prev; return; }
    const res = await api("PATCH", `/api/transactions/${id}`, { category_id: catId, scope });
    const c = res.category;
    toast(scope === "merchant"
      ? `${c.counterparty}: ${plural(c.updated, "transaction")} set to ${c.category}. Future ones will follow.`
      : `Changed just this transaction to ${c.category}.`);
    sel.dataset.current = catId;
    refreshBadge();
    reloadCurrent();
  } catch (err) { sel.value = prev; fail(err); }
}

async function openDetails(id) {
  const t = await api("GET", `/api/transactions/${id}`);
  $("#dt-title").textContent = t.counterparty || "Transaction";
  const rows = [
    ["Date", fmtDate(t.txn_date)],
    ["Account", esc(t.account_name)],
    ["Amount", `${t.direction === "debit" ? "−" : "+"}${money(t.amount)} (${t.direction})`],
    ["Balance after", t.balance != null ? money(t.balance) : "—"],
    ["Narration", `<span class="mono">${esc(t.narration_raw)}</span>`],
    ["Channel", esc(t.channel || "—")],
    ["UPI ID", esc(t.vpa || "—")],
    ["Payee key", `<span class="mono">${esc(t.merchant_key)}</span>`],
    ["Category", `${esc(t.category)} · ${(HOW[t.category_source] || (() => ""))(t)}`],
    ["Same payee", plural(t.similar_count, "other transaction")],
  ];
  $("#dt-details").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  $("#dt-note").value = t.note || "";
  $("#dt-similar").hidden = !t.similar_count;
  const action = await ask($("#dlg-txn"));
  if (action === "save") {
    await api("PATCH", `/api/transactions/${id}`, { note: $("#dt-note").value });
    toast("Note saved");
  } else if (action === "similar") {
    openTxns({ merchant_key: t.merchant_key, merchant_label: t.counterparty });
  }
}

// ── Review ───────────────────────────────────────────────────────────────────
async function loadReview() {
  const data = await api("GET", "/api/transactions?needs_review=true&sort=confidence&limit=500");
  $("#rv-body").innerHTML = data.items.length
    ? data.items.map((t) => txnRow(t, true)).join("")
    : `<tr><td colspan="6" class="empty">Nothing to review. Everything has a confident category.</td></tr>`;
  $("#rv-all").checked = false;
  updateAcceptButton();
}
const updateAcceptButton = () => {
  const n = $$(".rv-cb:checked").length;
  $("#rv-accept").disabled = !n;
  $("#rv-accept").textContent = n ? `Accept ${n} selected` : "Accept selected";
};

// ── Import ───────────────────────────────────────────────────────────────────
function accountSelect(selected) {
  return `<select class="ri-account"><option value="">Choose account…</option>${(state.meta?.accounts || [])
    .map((a) => `<option value="${a.id}"${a.id === selected ? " selected" : ""}>${esc(a.name)}</option>`).join("")}</select>`;
}

async function processFile(file, card, opts = {}) {
  card.className = "result";
  card.innerHTML = `<div class="title">${esc(file.name)}</div><div class="meta">Importing… new payees are sent to Claude for categorizing</div>`;
  const fd = new FormData();
  fd.append("file", file);
  const acct = opts.account_id ?? $("#imp-account").value;
  if (acct) fd.append("account_id", acct);
  if (opts.password) { fd.append("password", opts.password); fd.append("remember_password", opts.remember ? "true" : "false"); }
  try {
    const r = await api("POST", "/api/import", fd, true);
    renderImportResult(card, file, r);
  } catch (err) {
    card.className = "result error";
    const head = `<div class="title">${esc(file.name)}</div><div class="meta">${esc(err.message)}</div>`;
    if (err.status === 409 && err.data?.needs_password) {
      card.innerHTML = head + `<div class="form-row">
        <input type="password" class="ri-pw" placeholder="PDF password" autocomplete="off" aria-label="PDF password">
        <label class="check"><input type="checkbox" class="ri-remember" checked> Remember for this account</label>
        <button class="primary ri-go">Unlock and import</button></div>`;
      const go = () => processFile(file, card, { password: $(".ri-pw", card).value, remember: $(".ri-remember", card).checked, account_id: acct });
      $(".ri-go", card).addEventListener("click", go);
      $(".ri-pw", card).addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      $(".ri-pw", card).focus();
    } else if (err.status === 409 && err.data?.needs_account) {
      const hasAccounts = (state.meta?.accounts || []).length;
      card.innerHTML = head + (hasAccounts
        ? `<div class="form-row">${accountSelect()}<button class="primary ri-go">Import</button></div>`
        : `<div class="form-row"><button class="secondary" data-goto="settings">Add an account first</button></div>`);
      $(".ri-go", card)?.addEventListener("click", () => {
        const id = $(".ri-account", card).value;
        if (id) processFile(file, card, { ...opts, account_id: id });
      });
    } else {
      card.innerHTML = head;
    }
  }
}

function renderImportResult(card, file, r) {
  if (r.duplicate_file) {
    card.innerHTML = `<div class="title">${esc(file.name)}</div><div class="meta">Already imported into ${esc(r.account_name)}. Nothing changed.</div>`;
    return;
  }
  const s = r.categorize?.by_source || {};
  const parts = [];
  if (s.rule) parts.push(`${s.rule} by your rules`);
  if (s.seed) parts.push(`${s.seed} by built-in rules`);
  if (s.llm) parts.push(`${s.llm} by Claude`);
  if (r.transfers) parts.push(`${r.transfers} transfers between your accounts`);
  if (s.fallback) parts.push(`${s.fallback} left uncategorized`);
  const period = r.period_start ? `${fmtDate(r.period_start)} – ${fmtDate(r.period_end)}` : "";
  const claude = r.categorize?.llm_merchants
    ? `Claude looked at ${plural(r.categorize.llm_merchants, "new payee")} ($${(r.categorize.llm_cost_usd || 0).toFixed(4)}).` : "";
  const warn = r.categorize?.llm_error ? `<div class="meta" style="color: var(--critical)">${esc(r.categorize.llm_error)}</div>` : "";
  card.innerHTML = `<div class="title">${esc(file.name)} → ${esc(r.account_name)}</div>
    <div class="meta">${plural(r.n_new, "new transaction")}${r.n_duplicates ? `, ${r.n_duplicates} already present` : ""} · ${period}</div>
    <div class="meta">${parts.join(" · ")}${parts.length ? ". " : ""}${claude}</div>${warn}`;
}

async function handleFiles(files) {
  const box = $("#imp-results");
  const jobs = [...files].map((file) => {
    const card = document.createElement("div");
    box.prepend(card);
    return [file, card];
  });
  for (const [file, card] of jobs) await processFile(file, card);
  await loadMeta();
  loadStatements().catch(fail);
  refreshBadge();
}

async function loadStatements() {
  const rows = await api("GET", "/api/statements");
  $("#stmt-body").innerHTML = rows.length ? rows.map((s) => `<tr>
      <td>${esc(s.filename)}</td><td>${esc(s.account_name)}</td>
      <td class="date">${s.period_start ? `${fmtDate(s.period_start)} – ${fmtDate(s.period_end)}` : "—"}</td>
      <td class="num">${s.n_rows ?? ""}</td><td class="num">${s.n_new ?? ""}</td><td>${esc(s.source)}</td>
      <td class="num"><button class="danger stmt-del" data-id="${s.id}" data-name="${esc(s.filename)}" data-n="${s.n_new}">Undo import</button></td></tr>`).join("")
    : `<tr><td colspan="7" class="empty">No statements imported yet.</td></tr>`;
}

// ── Rules ────────────────────────────────────────────────────────────────────
const MATCH_LABEL = { exact: "Payee", contains: "Contains", regex: "Regex" };
const DIR_LABEL = { any: "Debits and credits", debit: "Debits", credit: "Credits" };

function rulePattern(r) {
  if (r.match_type === "exact") {
    const [d, ...rest] = r.pattern.split(":");
    return `<span class="pill">${d === "D" ? "paid to" : "received from"}</span> ${esc(rest.join(":"))}`;
  }
  return `<span class="mono">${esc(r.pattern)}</span>`;
}

async function loadRules() {
  const p = new URLSearchParams({ source: state.rulesSource });
  if ($("#rules-q").value) p.set("q", $("#rules-q").value);
  const rows = await api("GET", `/api/rules?${p}`);
  const empty = { user: "No rules yet. Correct a transaction's category or add a rule above.", llm: "Claude hasn't categorized anything yet.", seed: "No built-in rules." };
  $("#rules-body").innerHTML = rows.length ? rows.map((r) => `<tr>
      <td>${rulePattern(r)}${r.source === "llm" && r.confidence != null ? ` <span class="pill">${Math.round(r.confidence * 100)}%</span>` : ""}</td>
      <td>${MATCH_LABEL[r.match_type]}</td>
      <td>${r.match_type === "exact" ? "—" : DIR_LABEL[r.direction]}</td>
      <td><select class="rule-cat" data-id="${r.id}" aria-label="Category">${categoryOptions(r.category_id)}</select></td>
      <td class="num">${r.txn_count}</td>
      <td class="num"><button class="danger rule-del" data-id="${r.id}">Delete</button></td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">${empty[state.rulesSource]}</td></tr>`;
}

// ── Categories ───────────────────────────────────────────────────────────────
async function loadCategories() {
  const cats = await api("GET", "/api/categories");
  state.meta.categories = cats;
  const mergeOpts = (c) => `<option value="">—</option>` + cats.filter((o) => o.id !== c.id).map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
  $("#cat-body").innerHTML = cats.map((c) => `<tr data-id="${c.id}">
      <td><input class="cat-field" data-field="name" value="${esc(c.name)}" ${c.is_system ? "disabled" : ""} aria-label="Name"></td>
      <td><select class="cat-field" data-field="kind" aria-label="Kind">${["expense", "income", "transfer"].map((k) => `<option value="${k}"${k === c.kind ? " selected" : ""}>${k === "transfer" ? "Transfer" : k[0].toUpperCase() + k.slice(1)}</option>`).join("")}</select></td>
      <td><input class="cat-field" data-field="description" value="${esc(c.description || "")}" aria-label="Description"></td>
      <td class="num">${c.txn_count}</td>
      <td><input type="checkbox" class="cat-field" data-field="is_active" ${c.is_active ? "checked" : ""} ${c.is_system ? "disabled" : ""} aria-label="Shown"></td>
      <td>${c.is_system ? "" : `<select class="cat-merge" data-id="${c.id}" data-name="${esc(c.name)}" aria-label="Merge into">${mergeOpts(c)}</select>`}</td></tr>`).join("");
}

// ── Settings ─────────────────────────────────────────────────────────────────
async function loadAccounts() {
  const rows = await api("GET", "/api/accounts");
  const opt = (vals, cur) => vals.map(([v, l]) => `<option value="${v}"${v === cur ? " selected" : ""}>${l}</option>`).join("");
  $("#acct-body").innerHTML = rows.length ? rows.map((a) => `<tr data-id="${a.id}">
      <td><input class="acct-field" data-field="name" value="${esc(a.name)}" aria-label="Name"></td>
      <td><select class="acct-field" data-field="bank" aria-label="Bank">${opt([["HDFC", "HDFC"], ["CUB", "CUB"], ["OTHER", "Other"]], a.bank)}</select></td>
      <td><select class="acct-field" data-field="type" aria-label="Type">${opt([["savings", "Savings"], ["current", "Current"], ["credit_card", "Credit card"]], a.type)}</select></td>
      <td><input class="acct-field" data-field="last4" value="${esc(a.last4 || "")}" maxlength="4" style="width: 72px" aria-label="Last 4"></td>
      <td><input class="acct-field" data-field="holder_name" value="${esc(a.holder_name || "")}" aria-label="Holder name"></td>
      <td><input class="acct-field" data-field="gmail_query" value="${esc(a.gmail_query || "")}" placeholder="from:(bank) subject:(statement)" aria-label="Gmail search"></td>
      <td><button class="secondary acct-pw" data-id="${a.id}" data-name="${esc(a.name)}">${a.has_pdf_password ? "Saved · change" : "Set"}</button></td>
      <td class="num">${a.txn_count}</td>
      <td class="num"><button class="danger acct-del" data-id="${a.id}" data-name="${esc(a.name)}" data-n="${a.txn_count}">Delete</button></td></tr>`).join("")
    : `<tr><td colspan="9" class="empty">Add your HDFC and City Union Bank accounts below.</td></tr>`;
}

async function loadGmail() {
  const s = await api("GET", "/api/gmail/status");
  let text;
  if (!s.credentials_file) text = "Not set up yet: save your Google OAuth client file as data/credentials.json (steps in the README), then connect.";
  else if (s.connected) text = `Connected as ${esc(s.email)} (read-only).`;
  else text = "Ready to connect. Clicking Connect opens Google's consent page in your browser.";
  if (s.error && s.credentials_file) text += ` <span style="color: var(--critical)">${esc(s.error)}</span>`;
  $("#gmail-status").innerHTML = text;
  $("#gm-connect").disabled = !s.credentials_file || s.connected;
  $("#gm-sync").disabled = !s.connected;
  $("#gm-disconnect").hidden = !s.connected;
}

async function loadUsage() {
  const u = await api("GET", "/api/llm/usage");
  const keyWarn = state.meta && !state.meta.claude_key_set
    ? `<p class="hint" style="color: var(--critical)">ANTHROPIC_API_KEY isn't set in .env. New payees stay Uncategorized until it is (built-in and your own rules still work).</p>` : "";
  $("#llm-usage").innerHTML = `${keyWarn}<p>Model <span class="mono">${esc(u.model)}</span> · ${plural(u.calls, "call")} · ${plural(u.merchants, "payee")} categorized ·
    ${u.input_tokens.toLocaleString("en-IN")} input / ${u.output_tokens.toLocaleString("en-IN")} output tokens · <b>$${u.cost_usd.toFixed(4)}</b> total</p>
    <p class="hint">Only payee names, UPI IDs and narrations with every number masked are sent. Amounts, balances and account numbers stay on this machine.</p>`;
}

// ── Events ───────────────────────────────────────────────────────────────────
function bind() {
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => showTab(currentTab()));
  document.addEventListener("click", (e) => {
    const go = e.target.closest("[data-goto]");
    if (go) showTab(go.dataset.goto);
    const open = e.target.closest("[data-open]");
    if (open) openDetails(+open.dataset.open).catch(fail);
  });
  document.addEventListener("keydown", (e) => {
    const open = e.target.closest?.("[data-open]");
    if (open && e.key === "Enter") openDetails(+open.dataset.open).catch(fail);
  });
  document.addEventListener("change", (e) => {
    if (e.target.matches(".cat-select")) onCategoryChange(e.target);
    if (e.target.matches(".rv-cb")) updateAcceptButton();
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.dash && currentTab() === "dashboard") renderTrend($("#chart-trend"), state.dash.trend); }, 150);
  });

  // Dashboard
  $("#dash-month").addEventListener("change", () => loadDashboard().catch(fail));
  $("#dash-account").addEventListener("change", () => loadDashboard().catch(fail));

  // Transactions
  let qTimer;
  $("#f-q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => loadTxns(true).catch(fail), 250); });
  ["#f-month", "#f-account", "#f-category", "#f-source", "#f-review"].forEach((s) => $(s).addEventListener("change", () => loadTxns(true).catch(fail)));
  $("#txn-more").addEventListener("click", () => loadTxns(false).catch(fail));

  // Review
  $("#rv-all").addEventListener("change", (e) => { $$(".rv-cb").forEach((c) => { c.checked = e.target.checked; }); updateAcceptButton(); });
  $("#rv-accept").addEventListener("click", async () => {
    const ids = $$(".rv-cb:checked").map((c) => +c.value);
    try {
      const r = await api("POST", "/api/transactions/accept", { ids });
      toast(`Accepted ${plural(r.accepted, "transaction")}. Those payees now follow your rules.`);
      refreshBadge(); loadReview();
    } catch (err) { fail(err); }
  });
  $("#rv-retry").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("POST", "/api/recategorize", { scope: "uncategorized" });
      toast(r.llm_error ? `Claude: ${r.llm_error}` : `Claude categorized ${r.by_source.llm || 0} of ${r.total}. $${r.llm_cost_usd.toFixed(4)}`);
      refreshBadge(); loadReview();
    } catch (err) { fail(err); } finally { e.target.disabled = false; }
  });

  // Import
  const dz = $("#dropzone");
  $("#imp-file").addEventListener("change", (e) => { if (e.target.files.length) handleFiles(e.target.files).finally(() => { e.target.value = ""; }); });
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove("over")));
  dz.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files); });
  $("#stmt-body").addEventListener("click", async (e) => {
    const b = e.target.closest(".stmt-del");
    if (!b || !confirm(`Remove "${b.dataset.name}" and the ${b.dataset.n} transactions it added?`)) return;
    try { const r = await api("DELETE", `/api/statements/${b.dataset.id}`); toast(`Removed ${plural(r.deleted_transactions, "transaction")}`); await loadMeta(); loadStatements(); refreshBadge(); } catch (err) { fail(err); }
  });

  // Rules
  $$("#rules-source button").forEach((b) => b.addEventListener("click", () => {
    state.rulesSource = b.dataset.source;
    $$("#rules-source button").forEach((x) => x.classList.toggle("active", x === b));
    loadRules().catch(fail);
  }));
  let rTimer;
  $("#rules-q").addEventListener("input", () => { clearTimeout(rTimer); rTimer = setTimeout(() => loadRules().catch(fail), 250); });
  $("#rule-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("POST", "/api/rules", { pattern: $("#rf-pattern").value, match_type: $("#rf-type").value, direction: $("#rf-dir").value, category_id: +$("#rf-cat").value });
      toast(`Rule added. It now categorizes ${plural(r.matched_transactions, "transaction")}.`);
      $("#rf-pattern").value = "";
      state.rulesSource = "user";
      $$("#rules-source button").forEach((x) => x.classList.toggle("active", x.dataset.source === "user"));
      loadRules(); refreshBadge();
    } catch (err) { fail(err); }
  });
  $("#rules-body").addEventListener("change", async (e) => {
    if (!e.target.matches(".rule-cat")) return;
    try { await api("PATCH", `/api/rules/${e.target.dataset.id}`, { category_id: +e.target.value }); toast("Rule updated and applied"); loadRules(); refreshBadge(); } catch (err) { fail(err); }
  });
  $("#rules-body").addEventListener("click", async (e) => {
    const b = e.target.closest(".rule-del");
    if (!b || !confirm("Delete this rule? Its transactions will be categorized again by the next matching rule or Claude.")) return;
    try { const r = await api("DELETE", `/api/rules/${b.dataset.id}`); toast(`Rule deleted. ${plural(r.recategorized, "transaction")} recategorized.`); loadRules(); refreshBadge(); } catch (err) { fail(err); }
  });

  // Categories
  $("#cat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/api/categories", { name: $("#cf-name").value, kind: $("#cf-kind").value, description: $("#cf-desc").value });
      $("#cf-name").value = ""; $("#cf-desc").value = "";
      toast("Category added"); await loadMeta(); loadCategories();
    } catch (err) { fail(err); }
  });
  $("#cat-body").addEventListener("change", async (e) => {
    const el = e.target;
    const id = el.closest("tr")?.dataset.id;
    try {
      if (el.matches(".cat-field")) {
        const value = el.type === "checkbox" ? el.checked : el.value;
        await api("PATCH", `/api/categories/${id}`, { [el.dataset.field]: value });
        toast("Saved"); await loadMeta();
      } else if (el.matches(".cat-merge") && el.value) {
        const target = el.options[el.selectedIndex].text;
        if (!confirm(`Move everything in "${el.dataset.name}" to "${target}" and delete "${el.dataset.name}"?`)) { el.value = ""; return; }
        const r = await api("POST", `/api/categories/${id}/merge`, { target_id: +el.value });
        toast(`Merged: ${plural(r.moved_transactions, "transaction")} moved to ${r.into}`);
        await loadMeta(); loadCategories();
      }
    } catch (err) { fail(err); loadCategories(); }
  });

  // Accounts
  $("#acct-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("POST", "/api/accounts", { name: $("#af-name").value, bank: $("#af-bank").value, type: $("#af-type").value, last4: $("#af-last4").value, holder_name: $("#af-holder").value });
      ["#af-name", "#af-last4", "#af-holder"].forEach((s) => { $(s).value = ""; });
      toast("Account added"); await loadMeta(); loadAccounts();
    } catch (err) { fail(err); }
  });
  $("#acct-body").addEventListener("change", async (e) => {
    if (!e.target.matches(".acct-field")) return;
    const id = e.target.closest("tr").dataset.id;
    try { await api("PATCH", `/api/accounts/${id}`, { [e.target.dataset.field]: e.target.value }); toast("Saved"); await loadMeta(); } catch (err) { fail(err); loadAccounts(); }
  });
  $("#acct-body").addEventListener("click", async (e) => {
    const pw = e.target.closest(".acct-pw");
    if (pw) {
      $("#dp-text").textContent = `Used to open ${pw.dataset.name}'s PDF statements. Stored in Windows Credential Manager, not in the app's database.`;
      $("#dp-input").value = "";
      const action = await ask($("#dlg-password"));
      try {
        if (action === "save" && $("#dp-input").value) { await api("PUT", `/api/accounts/${pw.dataset.id}/pdf-password`, { password: $("#dp-input").value }); toast("Password saved"); }
        else if (action === "clear") { await api("PUT", `/api/accounts/${pw.dataset.id}/pdf-password`, { password: "" }); toast("Saved password removed"); }
        loadAccounts();
      } catch (err) { fail(err); }
      return;
    }
    const del = e.target.closest(".acct-del");
    if (del && confirm(`Delete ${del.dataset.name} and its ${del.dataset.n} transactions? This can't be undone.`)) {
      try { await api("DELETE", `/api/accounts/${del.dataset.id}`); toast("Account deleted"); await loadMeta(); loadAccounts(); } catch (err) { fail(err); }
    }
  });

  // Gmail
  $("#gm-connect").addEventListener("click", async (e) => {
    e.target.disabled = true;
    toast("Complete the Google sign-in in the browser window that just opened…", 10000);
    try { await api("POST", "/api/gmail/connect"); toast("Gmail connected"); } catch (err) { fail(err); }
    loadGmail().catch(fail);
  });
  $("#gm-disconnect").addEventListener("click", async () => { await api("POST", "/api/gmail/disconnect").catch(fail); loadGmail().catch(fail); });
  $("#gm-sync").addEventListener("click", async (e) => {
    e.target.disabled = true;
    $("#gm-result").innerHTML = `<div class="result"><div class="meta">Searching Gmail and importing…</div></div>`;
    try {
      const r = await api("POST", "/api/gmail/sync", {});
      $("#gm-result").innerHTML = r.accounts.map((a) => `<div class="result${a.problems.length ? " error" : ""}">
        <div class="title">${esc(a.account)}</div>
        <div class="meta" style="color: var(--text-2)">${plural(a.messages, "new email")} · ${plural(a.imported, "statement")} imported · ${plural(a.new_transactions, "new transaction")}${a.skipped ? ` · ${a.skipped} seen before` : ""}</div>
        ${a.problems.map((p) => `<div class="meta">${esc(p)}</div>`).join("")}</div>`).join("");
      await loadMeta(); refreshBadge();
    } catch (err) { $("#gm-result").innerHTML = ""; fail(err); } finally { e.target.disabled = false; }
  });
}

(async function init() {
  bind();
  try { await loadMeta(); } catch (err) { fail(err); }
  refreshBadge();
  showTab(currentTab());
})();

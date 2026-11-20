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
// ₹10.15 lakh for big totals, ₹23,300 below a lakh.
const lakh = (v) => (Math.abs(v) >= 100000 ? `${v < 0 ? "−" : ""}₹${(Math.abs(v) / 100000).toFixed(2).replace(/\.?0+$/, "")} lakh` : money(v, true));

async function api(method, url, body, isForm = false) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && data?.signin) {
    showGate().catch(() => {});  // signed out elsewhere, or the session expired
  }
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
    for (const c of group) html += `<option value="${c.id}"${c.id === selectedId ? " selected" : ""}>${c.parent_id ? "\u2003↳ " : ""}${esc(c.name)}</option>`;
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
  plan: () => loadPlan(),
  transactions: () => loadTxns(true),
  review: () => loadReview(),
  import: () => Promise.all([loadStatements(), loadNetbanking()]),
  rules: () => loadRules(),
  categories: () => loadCategories(),
  settings: () => Promise.all([loadPeople(), loadAccounts(), loadGmail(), loadUsage()]),
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
state.period = "month";
const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const signed = (v) => `${v < 0 ? "−" : ""}${money(Math.abs(v), true)}`;

async function loadDashboard() {
  const params = new URLSearchParams({ period: state.period });
  // Until you pick a month, let the server choose (the last full month, not one still in progress).
  if (state.dashMonthPicked && $("#dash-month").value) params.set("month", $("#dash-month").value);
  if ($("#dash-account").value) params.set("account_id", $("#dash-account").value);
  const [d, w] = await Promise.all([api("GET", `/api/overview?${params}`), api("GET", `/api/where?${params}`)]);
  state.dash = d;
  $("#dash-empty").hidden = d.has_data;
  $("#dash-body").hidden = !d.has_data;
  if (!d.has_data) return;
  $("#dash-month").value = d.period.end.slice(0, 7);
  const p = d.period;
  const soFar = p.in_progress ? " so far" : "";
  $("#t-spent").textContent = money(d.spent, true);
  $("#t-spent-sub").textContent = `${p.label}${soFar} · ${money(d.daily_average, true)} a day` +
    (d.projected_spend ? ` · on pace for ${money(d.projected_spend, true)}` : "");
  $("#t-income").textContent = money(d.income, true);
  $("#t-income-sub").textContent = d.income_sources.slice(0, 2).map((x) => x.name).join(", ");
  $("#t-saved").textContent = signed(d.saved);
  $("#t-saved-sub").textContent = d.savings_rate == null ? "No income recorded"
    : d.savings_rate < 0 ? (p.in_progress ? "Spending is ahead of income so far" : "Spent more than came in")
    : `${pct(d.savings_rate)} of income`;
  $("#t-invested").textContent = money(d.invested, true);
  $("#t-invested-sub").textContent = d.invest_rate == null || d.invest_rate > 1 ? "Not counted as spending" : `${pct(d.invest_rate)} of income · not counted as spending`;
  renderWhere(w);
  renderSplit(d);
  renderFacts(d.facts);
  renderCards(d.cards || []);
  renderCategoryTable(d);
  renderRecurring(d);
  renderMerchants(d.top_merchants);
  renderPeople(d);
  renderUnusual(d.unusual);
  renderTrend($("#chart-trend"), d.trend);
  const chain = state.meta?.categorizer?.chain || [];
  $("#dash-foot").textContent = `Spending is net of refunds booked to the same category; transfers between your accounts and card bill payments aren't counted. ` +
    `New payees are categorized by: ${chain.join(" → ")}.${d.needs_review ? ` ${plural(d.needs_review, "transaction")} need a look in Review.` : ""}`;
}

// ── Where the money went ─────────────────────────────────────────────────────
const WHERE_TOP = 10;

function whereRange(w) {
  return { start_date: w.period.start, end_date: w.period.end, range_label: w.period.label, account_id: $("#dash-account").value };
}

function categoryIdByName(name) {
  return (state.meta?.categories || []).find((c) => c.name === name)?.id;
}

function whereOpen(w, row) {
  if (row.kind === "card_bills") return showTab("import");
  const id = row.kind === "category" ? row.category_id
    : row.kind === "investments" ? categoryIdByName("Investments")
    : row.kind === "own_accounts" ? categoryIdByName("Self Transfer") : categoryIdByName("Credit Card Payment");
  openTxns({ ...whereRange(w), category_id: id, sort: "amount_desc" });
}

function renderWhere(w) {
  const p = w.period, several = p.months > 1;
  $("#where-period").textContent = p.label + (p.in_progress ? " so far" : "") +
    (p.months_in_period > 1 && Math.round(p.months) < p.months_in_period ? ` · statements cover ${Math.round(p.months)} of the ${p.months_in_period} months` : "");
  if (!w.money_out && !w.money_in) {
    $("#where-lead").textContent = "Nothing came in or went out in this period.";
    ["#where-rows", "#where-unknown", "#where-oneoffs", "#where-from"].forEach((s) => { $(s).innerHTML = ""; });
    return;
  }
  const perMonth = (v) => (several && v != null ? ` (${lakh(v)} a month)` : "");
  let lead = `<b>${lakh(w.money_out)}</b> went out${perMonth(w.out_per_month)} and <b>${lakh(w.money_in)}</b> came in.`;
  if (w.balance) {
    const ch = w.balance.change;
    lead += ` Your bank balance went ${ch < 0 ? "down" : "up"} by <b>${money(Math.abs(ch), true)}</b>, from ${money(w.balance.opening, true)} to ${money(w.balance.closing, true)}`;
    lead += w.check?.matches ? `: <span class="ok">every rupee is accounted for.</span>`
      : `. ${money(Math.abs(w.check.gap), true)} of that isn't accounted for: usually card spending not paid off yet, or a gap between statements.`;
  }
  $("#where-lead").innerHTML = lead;

  const max = Math.max(...w.rows.map((r) => r.amount), 1);
  const rowHtml = (r, i) => {
    const subs = [r.unexplained ? "not explained yet" : "", ...r.top_children.map((c) => `${c.name} ${money(c.amount, true)}`)]
      .filter(Boolean).join(" · ");
    const cls = r.unexplained ? "unknown" : r.kind === "investments" ? "savings" : "";
    return `<div class="where-row ${cls}" data-i="${i}"${i >= WHERE_TOP ? " hidden" : ""}>
      <button class="name" data-where="${i}" title="${esc(r.name)}"><span>${esc(r.name)}</span>
        ${subs ? `<span class="sub">${esc(subs)}</span>` : ""}</button>
      <div class="bar-cell"><div class="bar" style="width:${(r.amount / max) * 100}%"></div></div>
      <div class="num">${money(r.amount, true)}</div>
      <div class="num pm">${several && r.per_month != null ? money(r.per_month, true) : ""}</div>
      <div class="share">${pct(r.share)}</div></div>`;
  };
  $("#where-rows").innerHTML = `<div class="where-row head"><span>Went to</span><span class="bar-cell"></span><span class="num">Total</span>` +
    `<span class="num pm">${several ? "A month" : ""}</span><span class="share">Share</span></div>` + w.rows.map(rowHtml).join("") +
    (w.rows.length > WHERE_TOP ? `<button class="link where-toggle" id="where-all">Show ${w.rows.length - WHERE_TOP} more</button>` : "");
  $$("[data-where]", $("#where-rows")).forEach((b) => b.addEventListener("click", () => whereOpen(w, w.rows[+b.dataset.where])));
  $("#where-all")?.addEventListener("click", (e) => {
    $$(".where-row[hidden]", $("#where-rows")).forEach((r) => { r.hidden = false; });
    e.target.remove();
  });

  // Not explained yet
  const u = w.unexplained;
  const fixButton = (it, i) => `<button class="link fix" data-fix="${i}">${esc(it.fix)} →</button>`;
  $("#where-unknown").innerHTML = u.items.length
    ? `<h3><span>Not explained yet</span><span class="hint">${money(u.amount, true)} · ${pct(u.share)} of what went out</span></h3>
       <div class="where-meter" role="img" aria-label="${pct(u.share)} not explained"><i style="width:${Math.min(100, (u.share || 0) * 100)}%"></i></div>
       <ul class="where-list">${u.items.map((it, i) => `<li><span>${esc(it.text)}</span><span class="amt">${money(it.amount, true)}</span>
         ${it.items ? `<ul>${it.items.map((m) => `<li><span class="payee" data-open="${m.id}" tabindex="0">${esc(m.payee)}</span>
           <span class="amt">${money(m.amount, true)}</span><span class="sub">${fmtDate(m.date)} · ${esc(m.category)}: ${esc(m.why)}. Click to change it.</span></li>`).join("")}</ul>`
           : fixButton(it, i)}</li>`).join("")}</ul>`
    : `<h3><span>Not explained yet</span><span class="hint">nothing</span></h3><p class="hint">Everything that went out has a category.</p>`;
  $$("[data-fix]", $("#where-unknown")).forEach((b) => b.addEventListener("click", () => {
    const it = u.items[+b.dataset.fix];
    if (it.kind === "card_bills") showTab("import");
    else openTxns({ ...whereRange(w), category_id: it.category_id, sort: "amount_desc" });
  }));

  // One-off payments
  const regular = several && w.regular_per_month != null ? `${money(w.regular_per_month, true)} a month` : money(w.regular_out, true);
  $("#where-oneoffs").innerHTML = w.one_offs.length
    ? `<h3><span>One-off payments</span><span class="hint">${money(w.one_off_total, true)} · without them, ${regular} went out</span></h3>
       <ul class="where-list">${w.one_offs.map((o) => `<li><span class="payee" data-open="${o.id}" tabindex="0">${esc(o.payee)}</span>
         <span class="amt">${money(o.amount, true)}</span><span class="sub">${esc(o.category)} · ${fmtDate(o.date)} · ${esc(o.why)}</span></li>`).join("")}</ul>`
    : `<h3><span>One-off payments</span><span class="hint">none</span></h3><p class="hint">No large payments outside your usual pattern (₹10,000 or more).</p>`;

  $("#where-from").textContent = w.came_from.length
    ? `Came in: ${w.came_from.map((c) => `${c.name} ${lakh(c.amount)}`).join(" · ")}.` : "";
}

function renderSplit(d) {
  const inc = d.income || 0;
  const overspent = d.saved < 0 || inc <= 0;
  const total = overspent ? d.needs + d.wants : inc;
  const parts = [["needs", "Needs", d.needs], ["wants", "Wants", d.wants]];
  if (!overspent) parts.push(["saved", "Saved", d.saved]);
  const bar = $("#split-bar");
  bar.setAttribute("aria-label", parts.map(([, l, v]) => `${l} ${money(v, true)}`).join(", "));
  bar.innerHTML = parts.filter(([, , v]) => v > 0).map(([k, l, v]) => {
    const share = total ? v / total : 0;
    return `<div class="part ${k}" style="flex: ${v} 1 0" data-k="${k}">${share >= 0.12 ? `${l} ${Math.round(share * 100)}%` : ""}</div>`;
  }).join("");
  $$(".part", bar).forEach((el) => {
    const [, l, v] = parts.find(([k]) => k === el.dataset.k);
    el.addEventListener("mousemove", (e) => showTip(`<div class="tt-title">${l}</div><div class="tt-row">Amount<b>${money(v, true)}</b></div>` +
      (inc ? `<div class="tt-row">Share of income<b>${pct(v / inc)}</b></div>` : ""), e.clientX, e.clientY));
    el.addEventListener("mouseleave", hideTip);
  });
  // Shares of income normally; of spending when more went out than came in.
  const share = (v) => (total ? ` (${pct(v / total)}${overspent ? " of spending" : ""})` : "");
  $("#split-legend").innerHTML =
    `<span class="key s1"></span>Needs <b>${money(d.needs, true)}</b>${share(d.needs)}` +
    `<span class="key s2"></span>Wants <b>${money(d.wants, true)}</b>${share(d.wants)}` +
    (overspent && inc > 0
      ? `<span class="key over"></span>Overspent <b>${money(-d.saved, true)}</b> more than came in`
      : `<span class="key s3"></span>Saved <b>${money(Math.max(d.saved, 0), true)}</b>${share(d.saved)}`);
}

function renderFacts(facts) {
  const icon = { good: "✓", warn: "!", info: "i" };
  $("#facts").innerHTML = facts.length
    ? facts.map((f) => `<li class="${f.tone}"><span class="icon" aria-label="${f.tone}">${icon[f.tone]}</span><span>${esc(f.text)}</span></li>`).join("")
    : `<li class="info"><span class="icon">i</span><span>Nothing unusual. Import more months to compare against your usual spending.</span></li>`;
}

const CARD_STATUS = {
  paid: ["Paid", "good"], due: ["Due", ""], part_paid: ["Minimum paid", "medium"], overdue: ["Overdue", "high"],
  unknown: ["Amount due not found", ""], no_statement: ["No statement yet", ""],
};

function renderCards(cards) {
  $("#cards-card").hidden = !cards.length;
  if (!cards.length) return;
  const due = (c) => {
    if (!c.due_date) return "—";
    const when = c.status === "paid" ? "" : c.days_left > 1 ? `in ${c.days_left} days` : c.days_left === 1 ? "tomorrow"
      : c.days_left === 0 ? "today" : `${-c.days_left} days ago`;
    return `${fmtDate(c.due_date)}${when ? `<div class="sub">${when}</div>` : ""}`;
  };
  $("#cards").innerHTML = miniTable(cards, [
    { h: "Card", f: (c) => nameCell(c.name, c.statement_date ? `Statement ${fmtDate(c.statement_date)} · ${money(c.spent, true)} spent` : "Import a card statement to see its bill") },
    { h: "Bill", num: true, f: (c) => c.total_due == null ? "—" : `${money(c.total_due, true)}${c.min_due ? `<div class="sub">minimum ${money(c.min_due, true)}</div>` : ""}` },
    { h: "Paid since", num: true, f: (c) => (c.statement_date ? money(c.paid, true) : "—") },
    { h: "Due", num: true, f: due },
    { h: "Status", f: (c) => { const [label, cls] = CARD_STATUS[c.status] || [c.status, ""]; return `<span class="pill ${cls}">${label}</span>`; } },
  ], "");
}

function renderCategoryTable(d) {
  const el = $("#cat-table");
  if (!d.categories.length) { el.innerHTML = `<div class="none">No spending in this period</div>`; return; }
  const max = d.categories[0].amount;
  const month = d.period.kind === "month" ? d.period.start.slice(0, 7) : "";
  let html = `<div class="cat-row head"><span>Category</span><span class="bar-cell"></span><span class="amt">Spent</span><span class="chg">vs usual</span><span class="budget-cell">Budget</span></div>`;
  d.categories.forEach((c, i) => {
    const subs = c.children.filter((ch) => !ch.is_parent);
    const expandable = subs.length > 0;
    let chg = `<span class="chg">—</span>`;
    if (c.change_pct != null) {
      const cls = c.change_pct > 0.1 ? "up" : c.change_pct < -0.1 ? "down" : "";
      const arrow = c.change_pct > 0.005 ? "▲" : c.change_pct < -0.005 ? "▼" : "";
      chg = `<span class="chg ${cls}" title="Usual for this point: ${money(c.usual, true)}">${arrow} ${Math.abs(Math.round(c.change_pct * 100))}%</span>`;
    }
    const budget = c.budget
      ? `<div class="budget">${pct(c.budget_used)} of ${money(c.budget, true)}<div class="meter${c.budget_used > 1 ? " over" : ""}"><i style="width:${Math.min(100, c.budget_used * 100)}%"></i></div></div>`
      : `<div class="budget"></div>`;
    html += `<div class="cat-row top" data-i="${i}">
      <button class="name" data-toggle="${expandable ? i : ""}" data-cat="${c.id}" aria-expanded="false">
        <span class="caret">${expandable ? "▸" : ""}</span><span>${esc(c.name)}</span></button>
      <div class="bar-cell"><div class="bar" style="width:${(c.amount / max) * 100}%"></div></div>
      <div class="amt">${money(c.amount, true)}</div>${chg}<div class="budget-cell">${budget}</div></div>`;
    for (const ch of c.children) {
      if (!expandable) break;
      html += `<div class="cat-row child" data-parent="${i}" hidden>
        <button class="name" data-cat="${ch.id}" data-month="${month}"><span>${esc(ch.is_parent ? `${c.name} (other)` : ch.name)}</span></button>
        <div class="bar-cell"><div class="bar" style="width:${(ch.amount / max) * 100}%"></div></div>
        <div class="amt">${money(ch.amount, true)}</div><span class="chg">${plural(ch.count, "payment")}</span><span class="budget-cell"></span></div>`;
    }
  });
  el.innerHTML = html;
  $$(".cat-row.top .name", el).forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.toggle === "") { openTxns({ month, category_id: b.dataset.cat }); return; }
    const row = b.closest(".cat-row");
    const open = !row.classList.contains("open");
    row.classList.toggle("open", open);
    b.setAttribute("aria-expanded", open);
    $$(`.cat-row.child[data-parent="${b.dataset.toggle}"]`, el).forEach((r) => { r.hidden = !open; });
  }));
  $$(".cat-row.child .name", el).forEach((b) => b.addEventListener("click", () => openTxns({ month: b.dataset.month, category_id: b.dataset.cat })));
}

function miniTable(rows, cols, empty) {
  if (!rows.length) return `<div class="none">${empty}</div>`;
  return `<table class="mini"><thead><tr>${cols.map((c) => `<th class="${c.num ? "num" : ""}">${c.h}</th>`).join("")}</tr></thead><tbody>` +
    rows.map((r) => `<tr class="${r._cls || ""}">${cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.f(r)}</td>`).join("")}</tr>`).join("") +
    "</tbody></table>";
}
const nameCell = (name, sub) => `<div class="name" title="${esc(name)}">${esc(name)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}`;

function renderRecurring(d) {
  const rows = d.recurring.map((r) => ({ ...r, _cls: r.active ? "" : "inactive" }));
  $("#recurring-total").textContent = d.recurring_monthly
    ? `≈ ${money(d.recurring_monthly, true)} a month${d.recurring_share_of_income ? ` · ${pct(d.recurring_share_of_income)} of usual income` : ""}` : "";
  $("#recurring").innerHTML = miniTable(rows, [
    { h: "Payee", f: (r) => nameCell(r.payee, r.active ? r.category : `${r.category} · not seen recently`) },
    { h: "Per month", num: true, f: (r) => money(r.monthly, true) },
    { h: "Last paid", num: true, f: (r) => fmtDate(r.last_date) },
  ], "No regular payments spotted yet (needs about 3 months of statements).");
}

function renderMerchants(list) {
  $("#merchants").innerHTML = miniTable(list, [
    { h: "Merchant", f: (m) => nameCell(m.name, m.category) },
    { h: "Visits", num: true, f: (m) => m.count },
    { h: "Average", num: true, f: (m) => money(m.average, true) },
    { h: "Total", num: true, f: (m) => money(m.amount, true) },
  ], "No merchant spending in this period");
}

function renderPeople(d) {
  $("#people-total").textContent = d.people_total ? `${money(d.people_total, true)} in total` : "";
  $("#people").innerHTML = miniTable(d.people, [
    { h: "Person", f: (m) => nameCell(m.name) },
    { h: "Payments", num: true, f: (m) => m.count },
    { h: "Total", num: true, f: (m) => money(m.amount, true) },
  ], "No payments to individuals in this period");
}

function renderUnusual(list) {
  $("#unusual").innerHTML = miniTable(list, [
    { h: "Payment", f: (u) => `<div class="name payee" data-open="${u.id}" tabindex="0">${esc(u.payee || "Unknown")}</div><div class="sub">${esc(u.category)} · ${u.times_usual}× the usual payment</div>` },
    { h: "Date", num: true, f: (u) => fmtDate(u.date) },
    { h: "Amount", num: true, f: (u) => money(u.amount, true) },
  ], "Nothing out of the ordinary");
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
       <div class="tt-row"><span class="legend"><span class="key s2"></span></span>Income<b>${money(t.income, true)}</b></div>
       <div class="tt-row">Kept<b>${t.savings_rate == null ? "—" : pct(t.savings_rate)}</b></div>`, e.clientX, e.clientY));
    r.addEventListener("mouseleave", hideTip);
    r.addEventListener("click", () => {
      $("#dash-month").value = t.month;
      if ($("#dash-month").value !== t.month) return;
      state.dashMonthPicked = true;
      state.period = "month";
      $$("#dash-period button").forEach((b) => b.classList.toggle("active", b.dataset.p === "month"));
      loadDashboard().catch(fail);
    });
  });
}

// ── Transactions ─────────────────────────────────────────────────────────────
const HOW = {
  manual: () => "You, one-off",
  rule: () => "Your rule",
  seed: () => "Built-in rule",
  llm: (t) => `Claude · ${Math.round((t.confidence || 0) * 100)}%`,
  local: (t) => `Offline guess · ${Math.round((t.confidence || 0) * 100)}%`,
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
  if (state.txnRange) { p.set("start_date", state.txnRange.start); p.set("end_date", state.txnRange.end); }
  if (state.txnSort) p.set("sort", state.txnSort);
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
  const range = state.txnRange ? ` · <span class="pill">${esc(state.txnRange.label)}${state.txnSort === "amount_desc" ? ", largest first" : ""} <button class="link" id="clear-range" aria-label="Clear period filter">✕</button></span>` : "";
  $("#txn-summary").innerHTML = `${plural(data.total, "transaction")}${chip}${range}`;
  $("#clear-merchant")?.addEventListener("click", () => { state.txnMerchant = null; loadTxns(true).catch(fail); });
  $("#clear-range")?.addEventListener("click", () => { state.txnRange = null; state.txnSort = null; loadTxns(true).catch(fail); });
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
  state.txnRange = filters.start_date ? { start: filters.start_date, end: filters.end_date, label: filters.range_label || "" } : null;
  state.txnSort = filters.sort || null;
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

// ── Plan ─────────────────────────────────────────────────────────────────────
const HEALTH = { strong: "Doing well", okay: "Okay", needs_attention: "Needs attention" };

async function loadPlan() {
  renderPlan(await api("GET", "/api/plan"));
  await loadBudgets();
}

function compareBar(needs, wants, saved) {
  const parts = [["needs", needs], ["wants", wants], ["saved", Math.max(saved, 0)]];
  return `<div class="segbar">${parts.filter(([, v]) => v > 0).map(([k, v]) =>
    `<div class="part ${k}" style="flex: ${v} 1 0">${v >= 12 ? `${Math.round(v)}%` : ""}</div>`).join("")}</div>`;
}

function renderPlan(rec) {
  const body = $("#plan-body");
  $("#plan-disclaimer").textContent = rec?.disclaimer || "General budgeting guidance based only on your transactions; not professional financial advice.";
  if (!rec || !rec.plan) {
    $("#plan-meta").textContent = "";
    body.innerHTML = `<p class="none">No plan yet. Create one above; it takes about a minute.</p>`;
    return;
  }
  const p = rec.plan;
  const a = rec.averages || {};
  $("#plan-meta").textContent = `Made ${fmtDate(rec.created_at.slice(0, 10))} · ${rec.backend} · based on ${rec.based_on.map(monthShortYear).join(", ")}`;
  const fell = rec.errors?.length && rec.backend === "Offline rules"
    ? `<p class="hint">Claude wasn't available (${esc(rec.errors.join("; "))}), so this plan uses offline rules.</p>` : "";
  const now = a.income ? [a.needs / a.income * 100, a.wants / a.income * 100, a.saved / a.income * 100] : null;
  const total = p.actions.reduce((sum, x) => sum + (x.monthly_saving || 0), 0);
  body.innerHTML = `${fell}
    <div class="plan-head"><span class="health ${p.health.rating}">${HEALTH[p.health.rating] || p.health.rating}</span>
      <p class="plan-summary">${esc(p.summary)}</p></div>
    <p class="hint">${esc(p.health.comment)}</p>
    <div class="compare">
      ${now ? `<span>You now</span>${compareBar(...now)}` : ""}
      <span>Target</span>${compareBar(p.targets.needs_pct, p.targets.wants_pct, p.targets.savings_pct)}
    </div>
    <div class="legend split-legend"><span class="key s1"></span>Needs<span class="key s2"></span>Wants<span class="key s3"></span>Saved
      <span class="hint" style="margin-left:12px">${esc(p.targets.note)}</span></div>
    <div class="plan-section"><h3>What to do${total ? ` · could free up about ${money(total, true)} a month` : ""}</h3>
      <div class="actions">${p.actions.map((x) => `<div class="action">
        <div class="t">${esc(x.title)} <span class="pill ${x.priority}">${x.priority}</span></div>
        <div class="save">${x.monthly_saving ? `≈ ${money(x.monthly_saving, true)}/month` : ""}</div>
        <div class="d">${esc(x.detail)}</div></div>`).join("")}</div></div>
    <div class="plan-section"><h3>Suggested monthly budgets</h3>
      ${p.budgets.length ? `${miniTable(p.budgets, [
        { h: "Category", f: (b) => nameCell(b.category, b.reason) },
        { h: "Budget", num: true, f: (b) => money(b.monthly, true) },
      ], "")}<div class="toolbar" style="margin-top:10px"><button class="secondary" id="plan-apply">Use these as my budgets</button></div>` : `<p class="none">No budgets suggested.</p>`}
    </div>
    <div class="plan-section"><h3>Emergency fund</h3><p>${esc(p.emergency_fund)}</p></div>
    ${p.watch?.length ? `<div class="plan-section"><h3>Keep an eye on</h3><ul>${p.watch.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}`;
  $("#plan-apply")?.addEventListener("click", async () => {
    try {
      const r = await api("POST", `/api/plan/${rec.id}/apply-budgets`);
      toast(`Set ${plural(r.applied.length, "budget")}. The dashboard now tracks them.`);
      loadBudgets().catch(fail);
    } catch (err) { fail(err); }
  });
}

function monthShortYear(m) { const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleDateString("en-IN", { month: "short", year: "numeric" }); }

async function createPlan(useClaude) {
  const btns = [$("#plan-create"), $("#plan-offline")];
  btns.forEach((b) => { b.disabled = true; });
  $("#plan-body").innerHTML = `<p class="none">${useClaude ? "Asking Claude for a plan… this can take a minute." : "Building an offline plan…"}</p>`;
  try {
    renderPlan(await api("POST", "/api/plan", { use_claude: useClaude }));
  } catch (err) { fail(err); loadPlan().catch(fail); }
  finally { btns.forEach((b) => { b.disabled = false; }); }
}

async function loadBudgets() {
  const months = state.meta?.months || [];
  const thisMonth = new Date().toISOString().slice(0, 7);
  const anchor = months[0] === thisMonth ? months[1] : months[0];
  const [budgets, avg, cur] = await Promise.all([
    api("GET", "/api/budgets"),
    anchor ? api("GET", `/api/overview?period=3m&month=${anchor}`) : Promise.resolve(null),
    months[0] ? api("GET", `/api/overview?period=month&month=${months[0]}`) : Promise.resolve(null),
  ]);
  const byId = Object.fromEntries(budgets.map((b) => [b.category_id, b.monthly]));
  const amt = (o, id) => (o?.categories.find((c) => c.id === id)?.amount || 0);
  const tops = (state.meta?.categories || []).filter((c) => !c.parent_id && c.kind === "expense" && c.is_active && c.name !== "Uncategorized");
  const rows = tops.map((c) => ({ ...c, avg: amt(avg, c.id) / 3, now: amt(cur, c.id) })).sort((x, y) => y.avg - x.avg);
  $("#budget-body").innerHTML = rows.map((c) => `<tr>
    <td>${esc(c.name)}</td><td class="how">${esc(GROUP_LABEL[c.grp] || c.grp || "")}</td>
    <td class="num">${c.avg ? money(c.avg, true) : "—"}</td>
    <td class="num">${c.now ? money(c.now, true) : "—"}</td>
    <td><input type="number" min="0" step="100" class="budget-input" data-id="${c.id}" value="${byId[c.id] ?? ""}" placeholder="No budget" aria-label="Budget for ${esc(c.name)}" style="max-width:140px"></td></tr>`).join("");
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
// kind: "credit_card" lists only card accounts, "bank" only bank accounts, null everything.
const accountsOfKind = (kind) => (state.meta?.accounts || []).filter((a) => !kind || (a.type === "credit_card") === (kind === "credit_card"));

function accountSelect(selected, kind = null) {
  return `<select class="ri-account"><option value="">Choose ${kind === "credit_card" ? "card" : "account"}…</option>${accountsOfKind(kind)
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
      const kind = err.data.kind || null;
      const hasAccounts = accountsOfKind(kind).length;
      card.innerHTML = head + (hasAccounts
        ? `<div class="form-row">${accountSelect(null, kind)}<button class="primary ri-go">Import</button></div>`
        : `<div class="form-row"><button class="secondary" data-goto="settings">${kind === "credit_card" ? "Add a credit card account first" : "Add an account first"}</button></div>`);
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
  if (s.local) parts.push(`${s.local} guessed offline`);
  if (r.transfers) parts.push(r.kind === "credit_card" ? `${plural(r.transfers, "bill payment")} recognised` : `${r.transfers} transfers between your accounts`);
  if (s.fallback) parts.push(`${s.fallback} left uncategorized`);
  const period = r.period_start ? `${fmtDate(r.period_start)} – ${fmtDate(r.period_end)}` : "";
  const c = r.categorize || {};
  const claude = c.llm_calls
    ? `${esc(c.llm_backend || "Claude")} looked at ${plural(c.llm_merchants, "new payee")}${c.llm_cost_usd ? ` ($${c.llm_cost_usd.toFixed(4)})` : ""}.` : "";
  const warn = c.llm_error
    ? `<div class="meta" style="color: var(--muted)">Claude wasn't available (${esc(c.llm_error)}), so new payees were guessed offline.</div>` : "";
  const b = r.card;
  const bill = b ? `<div class="meta">Credit card statement${b.total_due != null ? ` · bill ${money(b.total_due, true)}` : ""}` +
    `${b.min_due ? ` (minimum ${money(b.min_due, true)})` : ""}${b.due_date ? `, due ${fmtDate(b.due_date)}` : ""}.` +
    `${r.account_created ? ` Added it as a new account, “${esc(r.account_name)}”; rename it in Settings if you like.` : ""}</div>` : "";
  card.innerHTML = `<div class="title">${esc(file.name)} → ${esc(r.account_name)}</div>
    <div class="meta">${plural(r.n_new, "new transaction")}${r.n_duplicates ? `, ${r.n_duplicates} already present` : ""} · ${period}</div>
    ${bill}<div class="meta">${parts.join(" · ")}${parts.length ? ". " : ""}${claude}</div>${warn}`;
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

// ── Net banking ──────────────────────────────────────────────────────────────
const nb = { banks: [], timer: null, importsSeen: 0 };

function nbAccount() {
  return (state.meta?.accounts || []).find((a) => String(a.id) === $("#nb-account").value);
}
function nbBank(acct = nbAccount()) {
  return nb.banks.find((b) => b.bank === (acct && ["HDFC", "CUB"].includes(acct.bank) ? acct.bank : "OTHER"));
}

async function loadNetbanking() {
  nb.banks = await api("GET", "/api/netbanking/banks");
  const accts = state.meta?.accounts || [];
  const current = $("#nb-account").value;
  $("#nb-account").innerHTML = accts.length
    ? accts.map((a) => `<option value="${a.id}">${esc(a.name)} (${esc(a.bank)})</option>`).join("")
    : `<option value="">Add an account in Settings first</option>`;
  if ([...$("#nb-account").options].some((o) => o.value === current)) $("#nb-account").value = current;
  $("#nb-open").disabled = !accts.length;
  renderNbBank();
  const s = await api("GET", "/api/netbanking/session");
  renderNbSession(s);
  if (s.state && !["none", "closed", "error"].includes(s.state)) startNbPolling();
}

function renderNbBank() {
  const b = nbBank();
  const card = nbAccount()?.type === "credit_card";  // cards have their own statement page, so their own steps
  const what = b ? `${esc(b.name)}${card ? " (credit card statements)" : ""}` : "";
  $("#nb-url").value = b?.login_url || "";
  const r = card ? b?.card_route : b?.route;
  $("#nb-route").innerHTML = r
    ? `Saved steps for ${what} (recorded ${fmtDate(r.recorded_at.slice(0, 10))}): ${r.steps.map(esc).join(" → ")}
       <button class="link" id="nb-forget">Forget these steps</button>`
    : b ? `No saved steps for ${what} yet. Use “Record my steps” once after logging in.` : "";
  $("#nb-forget")?.addEventListener("click", async () => {
    if (!confirm(`Forget the saved steps for ${b.name}${card ? " credit card statements" : ""}?`)) return;
    nb.banks = await api("DELETE", `/api/netbanking/banks/${b.bank}/route?kind=${card ? "credit_card" : "bank"}`).catch(fail) || nb.banks;
    renderNbBank();
  });
}

function renderNbSession(s) {
  const box = $("#nb-session");
  if (!s || s.state === "none") { box.hidden = true; return; }
  box.hidden = false;
  const live = !["closed", "error"].includes(s.state);
  $("#nb-message").textContent = s.message || "";
  $("#nb-message").style.color = s.state === "error" ? "var(--critical)" : "";
  $("#nb-progress").textContent = s.progress || "";
  $("#nb-fetch-row").hidden = !(live && s.has_route && s.state !== "recording");
  $("#nb-fetch").disabled = s.state === "replaying";
  $("#nb-record").hidden = !live;
  $("#nb-record").disabled = s.state === "replaying";
  $("#nb-record").textContent = s.recording ? "Stop recording" : (s.has_route ? "Record my steps again" : "Record my steps");
  $("#nb-close").hidden = !live;
  $("#nb-close").textContent = s.logout_failed ? "Close anyway" : "Log out and close";
  $("#nb-close").dataset.force = s.logout_failed ? "1" : "";
  $("#nb-open").disabled = live;
  $("#nb-steps").innerHTML = (s.recording ? s.recorded_steps : []).map((t) => `<li>${esc(t)}</li>`).join("");
  $("#nb-imports").innerHTML = (s.imports || []).map((i) => i.ok
    ? `<div class="result"><div class="title">${esc(i.file)} → ${esc(i.account_name)}</div>
       <div class="meta">${i.duplicate_file ? "Already imported before." : `${plural(i.n_new, "new transaction")}${i.n_duplicates ? `, ${i.n_duplicates} already present` : ""}${i.period_start ? ` · ${fmtDate(i.period_start)} – ${fmtDate(i.period_end)}` : ""}`}</div></div>`
    : `<div class="result error"><div class="title">${esc(i.file)}</div><div class="meta">${esc(i.error)}</div></div>`).join("");
  const n = (s.imports || []).filter((i) => i.ok).length;
  if (n > nb.importsSeen) {
    nb.importsSeen = n;
    loadMeta().then(() => { loadStatements().catch(fail); refreshBadge(); }).catch(fail);
  }
  if (!live && nb.timer) {
    stopNbPolling();
    api("GET", "/api/netbanking/banks").then((b) => { nb.banks = b; renderNbBank(); }).catch(fail);
  }
}

function startNbPolling() {
  stopNbPolling();
  nb.timer = setInterval(async () => {
    try { renderNbSession(await api("GET", "/api/netbanking/session")); } catch (err) { stopNbPolling(); fail(err); }
  }, 1500);
}
function stopNbPolling() { clearInterval(nb.timer); nb.timer = null; }

async function nbAction(action, body = {}) {
  try { renderNbSession(await api("POST", `/api/netbanking/session/${action}`, body)); } catch (err) { fail(err); }
  if (action === "record_stop") { nb.banks = await api("GET", "/api/netbanking/banks"); renderNbBank(); }
}

function nbPeriod() {
  const v = $("#nb-period").value;
  if (v === "since") return {};
  const iso = (d) => d.toISOString().slice(0, 10);
  if (v === "custom") return { start: $("#nb-from").value, end: $("#nb-to").value };
  const end = new Date(), start = new Date(Date.now() - (+v) * 86400000);
  return { start: iso(start), end: iso(end) };
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
const GROUP_LABEL = { needs: "Needs", wants: "Wants", savings: "Savings", income: "Income", transfer: "Transfer" };

async function loadCategories() {
  const cats = await api("GET", "/api/categories");
  state.meta.categories = cats;
  const tops = cats.filter((c) => !c.parent_id);
  const hasKids = new Set(cats.filter((c) => c.parent_id).map((c) => c.parent_id));
  $("#cf-parent").innerHTML = `<option value="">Main category</option>` + tops.filter((t) => t.kind !== "transfer" || t.grp === "savings")
    .map((t) => `<option value="${t.id}">Sub-category of ${esc(t.name)}</option>`).join("");
  const mergeOpts = (c) => `<option value="">—</option>` + cats.filter((o) => o.id !== c.id).map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
  const parentOpts = (c) => `<option value="">—</option>` + tops.filter((t) => t.id !== c.id && t.kind === c.kind)
    .map((t) => `<option value="${t.id}"${t.id === c.parent_id ? " selected" : ""}>${esc(t.name)}</option>`).join("");
  const opt = (vals, cur) => vals.map((v) => `<option value="${v}"${v === cur ? " selected" : ""}>${GROUP_LABEL[v] || v[0].toUpperCase() + v.slice(1)}</option>`).join("");
  $("#cat-body").innerHTML = cats.map((c) => `<tr data-id="${c.id}">
      <td><input class="cat-field" data-field="name" value="${esc(c.name)}" ${c.is_system ? "disabled" : ""} aria-label="Name" style="${c.parent_id ? "margin-left:20px;width:calc(100% - 20px)" : ""}"></td>
      <td><select class="cat-field" data-field="parent_id" aria-label="Sub-category of" ${c.is_system || hasKids.has(c.id) ? "disabled" : ""}>${parentOpts(c)}</select></td>
      <td><select class="cat-field" data-field="kind" aria-label="Kind" ${c.parent_id ? "disabled" : ""}>${opt(["expense", "income", "transfer"], c.kind)}</select></td>
      <td><select class="cat-field" data-field="grp" aria-label="Group" ${c.parent_id ? "disabled title=\"Follows its main category\"" : ""}>${opt(["needs", "wants", "savings", "income", "transfer"], c.grp)}</select></td>
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
  const [s, u] = await Promise.all([api("GET", "/api/settings/categorizer?check=true"), api("GET", "/api/llm/usage")]);
  $("#cat-mode").value = s.mode;
  const sub = $("#cat-mode option[value=claude_code]");
  sub.disabled = !s.subscription_allowed;
  sub.textContent = s.subscription_allowed ? "Claude via your subscription (Claude Code)" : "Claude subscription (only for the person who set up the app)";
  const cli = s.claude_cli || {};
  const cliText = !cli.installed ? "Claude Code CLI not found"
    : cli.logged_in ? `Claude Code CLI signed in (${esc(cli.auth_method === "claude.ai" ? "your Claude subscription" : cli.auth_method || "signed in")})`
    : "Claude Code CLI installed but not signed in: run <span class=\"mono\">claude</span> once and log in";
  $("#cat-status").innerHTML = `
    <div>${s.api_key_set ? "✓" : "✗"} Claude API key ${s.api_key_set ? "set" : "not set (needs credits on console.anthropic.com)"}</div>
    <div>${cli.installed && cli.logged_in ? "✓" : "✗"} ${cliText}</div>
    <div>✓ Offline categorizer (always on as the last step)</div>
    <p class="hint">Order used now: ${s.chain.map(esc).join(" → ")}. ${esc(s.modes[s.mode])}.</p>`;
  $("#llm-usage").innerHTML = `<p>${plural(u.api_calls, "API call")} ($${u.cost_usd.toFixed(4)}) · ${plural(u.subscription_calls, "call")} through your subscription ·
    ${plural(u.local_guesses, "transaction")} currently guessed offline</p>
    <p class="hint">Claude only sees payee names, UPI IDs and narrations with every number masked. Amounts, balances and account numbers stay on this machine.
    Calls through your subscription run the official Claude Code CLI under your own sign-in and count toward your plan's usage limits; that's fine for your own use, but use an API key if you ever host this for other people.</p>`;
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
  $$("#dash-period button").forEach((b) => b.addEventListener("click", () => {
    state.period = b.dataset.p;
    $$("#dash-period button").forEach((x) => x.classList.toggle("active", x === b));
    loadDashboard().catch(fail);
  }));
  $("#dash-month").addEventListener("change", () => { state.dashMonthPicked = true; loadDashboard().catch(fail); });

  // Plan & budgets
  $("#plan-create").addEventListener("click", () => createPlan(true));
  $("#plan-offline").addEventListener("click", () => createPlan(false));
  $("#budget-body").addEventListener("change", async (e) => {
    if (!e.target.matches(".budget-input")) return;
    try {
      const r = await api("PUT", `/api/budgets/${e.target.dataset.id}`, { monthly: e.target.value });
      toast(r.monthly ? `${r.category}: budget set to ${money(r.monthly, true)} a month` : `${r.category}: budget removed`);
    } catch (err) { fail(err); }
  });
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
      const bits = [];
      if (r.by_source.llm) bits.push(`${r.by_source.llm} by Claude`);
      if (r.by_source.local) bits.push(`${r.by_source.local} guessed offline`);
      if (r.by_source.fallback) bits.push(`${r.by_source.fallback} still uncategorized`);
      toast(`${plural(r.total, "transaction")} re-checked: ${bits.join(", ") || "no change"}.${r.llm_error ? ` Claude unavailable: ${r.llm_error}` : ""}`, 7000);
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

  // Net banking
  $("#nb-account").addEventListener("change", renderNbBank);
  $("#nb-url").addEventListener("change", async (e) => {
    const b = nbBank();
    if (!b) return;
    try { nb.banks = await api("PUT", `/api/netbanking/banks/${b.bank}`, { login_url: e.target.value }); toast(`Login page saved for ${b.name}`); }
    catch (err) { fail(err); renderNbBank(); }
  });
  $("#nb-open").addEventListener("click", async () => {
    const acct = nbAccount();
    if (!acct) return;
    try {
      nb.importsSeen = 0;
      renderNbSession(await api("POST", "/api/netbanking/session", { account_id: acct.id }));
      startNbPolling();
    } catch (err) { fail(err); }
  });
  $("#nb-period").addEventListener("change", (e) => {
    const custom = e.target.value === "custom";
    $("#nb-from").hidden = !custom; $("#nb-to").hidden = !custom;
  });
  $("#nb-fetch").addEventListener("click", () => {
    const p = nbPeriod();
    if ($("#nb-period").value === "custom" && (!p.start || !p.end)) { toast("Pick both dates"); return; }
    nbAction("replay", p);
  });
  $("#nb-record").addEventListener("click", () => nbAction($("#nb-record").textContent === "Stop recording" ? "record_stop" : "record_start"));
  // Logs out of the bank, then closes. If the app couldn't find Log out, the button becomes “Close anyway”.
  $("#nb-close").addEventListener("click", () => nbAction("close", { force: $("#nb-close").dataset.force === "1" }));

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
      await api("POST", "/api/categories", { name: $("#cf-name").value, kind: $("#cf-kind").value, description: $("#cf-desc").value, parent_id: $("#cf-parent").value || null });
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

  // Categorizer choice
  $("#cat-mode").addEventListener("change", async (e) => {
    try {
      await api("PUT", "/api/settings/categorizer", { mode: e.target.value });
      toast("Saved. Applies to the next import or re-check.");
      await loadMeta(); loadUsage().catch(fail);
    } catch (err) { fail(err); }
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

// ── Sign-in ──────────────────────────────────────────────────────────────────
const gate = { person: null };

async function showGate(auth) {
  auth = auth || await api("GET", "/api/auth/state");
  document.body.classList.add("signed-out");
  $("#gate").hidden = false;
  $("#who").hidden = true;
  ["#gate-setup", "#gate-remote", "#gate-people", "#gate-pin"].forEach((s) => { $(s).hidden = true; });
  if (auth.setup_needed) {
    $(auth.can_setup ? "#gate-setup" : "#gate-remote").hidden = false;
    if (auth.can_setup) $("#setup-name").focus();
    return;
  }
  if (auth.people.length === 1) return askPin(auth.people[0]);
  $("#people-list").innerHTML = auth.people.map((p) => `<button class="person" data-id="${p.id}"><span class="initial">${esc(p.name[0].toUpperCase())}</span>${esc(p.name)}</button>`).join("");
  $$("#people-list .person").forEach((b) => b.addEventListener("click", () => askPin(auth.people.find((p) => String(p.id) === b.dataset.id))));
  $("#gate-people").hidden = false;
}

function askPin(person) {
  gate.person = person;
  $("#gate-people").hidden = true;
  $("#gate-pin").hidden = false;
  $("#pin-back").hidden = document.querySelectorAll("#people-list .person").length < 2;
  $("#pin-title").textContent = `Hello ${person.name}, enter your PIN`;
  $("#pin-input").value = ""; $("#pin-error").textContent = "";
  if (!matchMedia("(pointer: coarse)").matches) $("#pin-input").focus();  // phones use the keypad, no keyboard pop-up
}

async function signIn(e) {
  e.preventDefault();
  const pin = $("#pin-input").value;
  if (!pin) return;
  try {
    await api("POST", "/api/auth/signin", { profile_id: gate.person.id, pin });
    location.reload();
  } catch (err) {
    $("#pin-error").textContent = err.message;
    $("#pin-input").value = "";
  }
}

async function setUp(e) {
  e.preventDefault();
  const pin = $("#setup-pin").value;
  if (pin !== $("#setup-pin2").value) { $("#setup-error").textContent = "The two PINs don't match."; return; }
  try {
    await api("POST", "/api/auth/setup", { name: $("#setup-name").value, pin });
    location.reload();
  } catch (err) { $("#setup-error").textContent = err.message; }
}

function bindGate() {
  $("#pin-form").addEventListener("submit", signIn);
  $("#setup-form").addEventListener("submit", setUp);
  $("#pin-back").addEventListener("click", () => showGate().catch(fail));
  $$("#keypad [data-k]").forEach((b) => b.addEventListener("click", () => {
    const input = $("#pin-input");
    input.value = b.dataset.k === "del" ? input.value.slice(0, -1) : (input.value + b.dataset.k).slice(0, 8);
  }));
  $("#sign-out").addEventListener("click", async () => {
    await api("POST", "/api/auth/signout").catch(() => {});
    location.reload();
  });
}

// ── People (Settings) ────────────────────────────────────────────────────────
async function loadPeople() {
  const list = await api("GET", "/api/people");
  const owner = !!state.me?.is_owner;
  $("#person-add").hidden = !owner;
  $("#people-body").innerHTML = `<table class="plain"><tbody>${list.map((p) => `<tr>
      <td><b>${esc(p.name)}</b>${p.you ? ` <span class="pill">you</span>` : ""}${p.is_owner ? ` <span class="pill">set up the app</span>` : ""}</td>
      <td class="num">${owner || p.you ? `<button class="link" data-rename="${p.id}">Rename</button>` : ""}
        ${owner && !p.you ? ` · <button class="link" data-resetpin="${p.id}">Reset PIN</button>` : ""}</td></tr>`).join("")}</tbody></table>`;
  $$("[data-rename]").forEach((b) => b.addEventListener("click", async () => {
    const name = prompt("New name");
    if (!name) return;
    try { await api("PATCH", `/api/people/${b.dataset.rename}`, { name }); toast("Renamed"); loadPeople(); } catch (err) { fail(err); }
  }));
  $$("[data-resetpin]").forEach((b) => b.addEventListener("click", async () => {
    const pin = prompt("New PIN for them (4 to 8 digits). It signs them out on their devices.");
    if (!pin) return;
    try { await api("PATCH", `/api/people/${b.dataset.resetpin}`, { pin }); toast("PIN changed"); } catch (err) { fail(err); }
  }));
}

function bindPeople() {
  $("#person-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const p = await api("POST", "/api/people", { name: $("#pf-name").value, pin: $("#pf-pin").value });
      e.target.reset();
      toast(`Added ${p.name}. They sign in with their name and PIN.`);
      loadPeople();
    } catch (err) { fail(err); }
  });
  $("#mypin-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("PATCH", `/api/people/${state.me.id}`, { current_pin: $("#mp-current").value, pin: $("#mp-new").value });
      e.target.reset();
      toast("PIN changed. Your other devices are signed out.");
    } catch (err) { fail(err); }
  });
}

(async function init() {
  bind();
  bindGate();
  bindPeople();
  const auth = await api("GET", "/api/auth/state").catch(() => null);
  if (!auth?.me) { await showGate(auth || undefined).catch(fail); return; }
  state.me = auth.me;
  $("#who-name").textContent = auth.me.name;
  $("#who").hidden = false;
  try { await loadMeta(); } catch (err) { fail(err); }
  refreshBadge();
  showTab(currentTab());
})();

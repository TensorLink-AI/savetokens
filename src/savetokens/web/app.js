// savetokens browser view ("Retro Terminal"): reads /v1/dashboard and draws it. Read-only; no external code.
"use strict";

const TOOLS = { "claude-code": "Claude Code", codex: "Codex", hermes: "Hermes" };
const REFRESH_MS = 30000;
const $ = (id) => document.getElementById(id);

// ── credentials: a local key (savetokens web) or a server token (from a join code) ──
const store = {
  get(k, session) { try { return (session ? sessionStorage : localStorage).getItem(k); } catch { return null; } },
  set(k, v, session) { try { (session ? sessionStorage : localStorage).setItem(k, v); } catch { /* private mode */ } },
  del(k) { try { sessionStorage.removeItem(k); localStorage.removeItem(k); } catch { /* ignore */ } },
};
let memToken = null;   // when storage is unavailable
const token = () => store.get("st_key") || store.get("st_token") || memToken;

async function readFragment() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (["spend", "limits", "projects"].includes(h.get("tab"))) tab = h.get("tab");   // a link straight to a tab
  if (h.has("key") || h.has("code")) history.replaceState(null, "", location.pathname + location.search);
  if (h.has("key")) { memToken = h.get("key"); store.set("st_key", memToken); }
  if (h.has("code")) await join(h.get("code"));
}

async function join(code) {
  const r = await fetch("v1/join", { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify({ code }) });
  const body = await r.json().catch(() => ({}));
  if (!r.ok || !body.token) throw new Error(body.error || "That code didn't work.");
  memToken = body.token;
  store.set("st_token", body.token);
}

// ── formatting ──
const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const p2 = (n) => String(n).padStart(2, "0");
const hm = (ts) => { const d = new Date(ts * 1000); return `${p2(d.getHours())}:${p2(d.getMinutes())}`; };
function when(ts, now) {
  if (ts == null) return "–";
  const d = new Date(ts * 1000), n = new Date(now * 1000);
  if (d.toDateString() === n.toDateString()) return hm(ts);
  return Math.abs(ts - now) < 6 * 86400 ? `${DAY[d.getDay()]} ${hm(ts)}`
    : `${d.getDate()} ${d.toLocaleDateString([], { month: "short" })} ${hm(ts)}`;
}
function span(s) {
  s = Math.max(0, s);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 2 * 86400) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}
const pct = (v, d = 0) => (v == null ? "–" : `${v.toFixed(d)}%`);
const usd = (v) => (v == null ? "–" : v >= 100 ? `$${Math.round(v).toLocaleString("en-US")}` : `$${v.toFixed(2)}`);
const usd2 = (v) => (v == null ? "–" : "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const ago = (ts, now) => (ts ? `${span(now - ts)} ago` : "never");
function tok(v) {
  if (v == null) return "–";
  for (const [u, d] of [["B", 1e9], ["M", 1e6], ["k", 1e3]]) if (v >= d) return `${(v / d).toFixed(v / d >= 100 ? 0 : 1)}${u}`;
  return String(Math.round(v));
}
const rng = (q, f) => (q && f(q[0]) !== f(q[2]) ? `${f(q[0])}–${f(q[2])}` : q ? f(q[1]) : "–");
function niceTop(v) {
  const k = Math.pow(10, Math.floor(Math.log10(v)));
  return [1, 2, 2.5, 5, 10].map((m) => m * k).find((m) => m >= v);
}
const fmtNum = (v) => (v >= 10 ? String(Math.round(v)) : String(Number(v.toPrecision(3))));
const clamp = (v) => Math.max(0, Math.min(100, v));

// ── tiny DOM helper (text only: names come from users' data) ──
function el(tag, cls, ...kids) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}
function clear(node) { while (node.firstChild) node.firstChild.remove(); return node; }
// a table's rows go, its heading row (.row.head) stays
function clearRows(node) { for (const c of [...node.childNodes]) if (!(c.classList && c.classList.contains("head"))) c.remove(); return node; }
const empty = (text) => el("div", "empty", text);
const kv = (k, v) => el("span", "kv", el("span", "dim", k), el("span", null, v));
// a tile with a label cut into its border; with `pop`, a panel that opens on hover or focus
function tile(cls, legend, pop) {
  const t = el("div", "tile " + cls);
  if (legend != null) t.append(el("span", "legend", `┤ ${legend} ├`));
  if (pop) { t.classList.add("pop-host"); t.tabIndex = 0; t._pop = el("div", "pop-in"); t.append(el("div", "pop", t._pop)); }
  return t;
}

// ── view state ──
const PERIOD = { day: ["Today", "Next 24 hours", "by midnight", "24 h"], week: ["This week", "Next 7 days", "by Sunday night", "7 days"],
                 month: ["This month", "Next 30 days", "by month's end", "30 days"] };
let tab = store.get("st_tab") || "limits";
let period = store.get("st_period") || "month";
let live = store.get("st_live") !== "off";
let sel = null, allSessions = false;
const open = {}, alertOpen = {};
let last = null;

// ── header: forecast, coverage, quiet machines ──
const QUIET = 86400;   // machines sync every 10 minutes while they run
const quiet = (s) => (s.machines || []).filter((m) => m.last_sync && s.now - m.last_sync > QUIET);
const SOURCE = { ephemeris: "Ephemeris", baseline: "local baseline" };

function drawMeta(s) {
  $("pop-forecast").textContent = (SOURCE[s.source] || "none yet") + (s.forecast_made_at ? `, ${ago(s.forecast_made_at, s.now)}` : "");
  const m = (s.machines || []).length;
  $("pop-coverage").textContent = m ? `${m} machine${m === 1 ? "" : "s"} this week` : "this machine";
  const problems = [];
  const q = quiet(s).length;
  if (q) problems.push(`${q} machine${q === 1 ? "" : "s"} not synced for over a day: see MACHINES in Limits`);
  if (s.sync_error) problems.push("Server unreachable: showing this machine only");
  $("meta").textContent = problems.join(" · ");
  $("meta").hidden = !problems.length;
}

// plan or API: a badge that tells them apart at a glance, wherever either appears
function badge(kind) {
  const [cls, txt, tip] = { subscription: ["sub", "PLAN", "Subscription: usage limits, a fixed price"],
    api: ["api", "API", "Pay as you go: billed per token"], goal: ["goal", "BUDGET", "A budget you set"] }[kind]
    || ["goal", String(kind || "").toUpperCase(), ""];
  const b = el("span", `badge ${cls}`, txt); if (tip) b.title = tip; return b;
}

function drawHeadline(s) {
  const h = s.headline || { level: "none", text: "" };
  const level = { bad: "act", warn: "warn", ok: "ok" }[h.level] || "none";
  $("headline").className = `headsup ${level}`;
  $("headline-label").textContent = { act: "┤ ACT NOW ├", warn: "┤ HEADS-UP ├", ok: "┤ ON TRACK ├" }[level] || "┤ STATUS ├";
  // the headline names the plan or API budget it's about; the others are under "N on track"
  const items = h.items || [], p = clear($("headline-text"));
  const lead = (h.level === "bad" || h.level === "warn") && items.length ? items[0] : null;
  if (lead) p.append(badge(lead.kind), h.text);
  else p.append(items.length > 1 ? "On track: nothing is likely to run out early." : h.text || "Nothing to report yet.");
  const rest = lead ? items.slice(1) : items.length > 1 ? items : [];
  $("ontrack").hidden = !rest.length;
  if (!rest.length) return;
  const ok = rest.filter((x) => x.level === "ok" || x.level === "none").length;
  $("ontrack-count").textContent = ok === rest.length ? `${ok} on track` : `${rest.length} more`;
  const list = clear($("ontrack-list"));
  for (const x of rest) list.append(el("span", "ontrack-item", el("span", "t", badge(x.kind), x.tool), el("span", "dim", x.text)));
}

// ── spend ──
const MIXC = ["c0", "c1", "c2", "c3", "c4", "c5"];

function drawSpend(s) {
  const sp = s.spend, prov = $("providers"), kpis = clear($("kpis"));
  clearRows(prov);
  const seg = clear($("periods"));
  for (const [k, [name]] of Object.entries(PERIOD)) {
    const b = el("button", null, name.toUpperCase());
    b.type = "button"; b.setAttribute("aria-pressed", String(k === period));
    b.addEventListener("click", () => setPeriod(k));
    seg.append(b);
  }
  if (!sp || sp.error || !sp.periods) {
    kpis.append(empty(sp && sp.error ? `Couldn't work out spend: ${sp.error}` : "No usage yet."));
    return;
  }
  const src = SOURCE[sp.source];
  $("spend-src").textContent = src ? `forecast: ${src}` + (s.forecast_made_at ? `, made ${ago(s.forecast_made_at, s.now)}` : "") : "forecast on its way";
  const [name, nextName, by] = PERIOD[period], x = sp.periods[period];
  if (x) {
    const payg = x.usd || 0, fixed = x.fixed_usd || 0, pj = x.projected || {}, nx = x.next || {};
    const kpi = (label, value, sub, rows) => {
      const t = tile("kpi", label, true);
      t.append(el("div", "big", value), el("div", "sub", sub));
      for (const [k, v] of rows) t._pop.append(kv(k, v));
      kpis.append(t);
    };
    kpi(`${name.toUpperCase()} SO FAR`, usd2(x.cost), `${tok(x.tokens)} tokens`,
      [["pay as you go", usd2(payg)], ["subscriptions", usd2(fixed)], ["tokens", tok(x.tokens)], ["requests", (x.requests || 0).toLocaleString("en-US")]]);
    kpi(`LIKELY ${by.toUpperCase()}`, pj.cost ? usd(pj.cost[1]) : "–", pj.cost ? `range ${rng(pj.cost, usd)}` : "forecast on its way",
      [["likely", pj.cost ? usd(pj.cost[1]) : "–"], ["range", pj.cost ? rng(pj.cost, usd) : "–"], ["tokens", pj.tokens ? `${tok(pj.tokens[1])} (${rng(pj.tokens, tok)})` : "–"]]);
    kpi(nextName.toUpperCase(), nx.cost ? usd(nx.cost[1]) : "–", nx.cost ? `range ${rng(nx.cost, usd)}` : "forecast on its way",
      [["likely", nx.cost ? usd(nx.cost[1]) : "–"], ["of which subscriptions", usd(nx.fixed_usd)], ["tokens", nx.tokens ? `${tok(nx.tokens[1])} (${rng(nx.tokens, tok)})` : "–"]]);
    const saved = Math.max(0, (x.api_value || 0) - x.cost);
    kpi("API VALUE", usd(x.api_value), saved > 0.005 ? `plans save you ${usd(saved)}` : "what it'd cost pay-as-you-go",
      [["API value of all usage", usd2(x.api_value)], ["what you pay", usd2(x.cost)], ["saved by plans", usd2(saved)]]);
    $("saved").textContent = `${usd(saved)} ${name.toLowerCase()}`;
  }
  $("period-name").textContent = name.toUpperCase();
  const cells = (y) => [el("span", "r", tok(y.tokens)), el("span", "r spend", usd2(y.cost != null ? y.cost : y.usd)),
    el("span", "r", y.projected && y.projected.cost ? usd(y.projected.cost[1]) : y.projected_usd != null ? usd(y.projected_usd) : "–"),
    el("span", "r dim", y.api_value ? usd2(y.api_value) : "–")];
  const list = (sp.providers || []).filter((e) => e.periods[period]);
  for (const e of list) {
    const y = e.periods[period], id = e.label;
    const r = el("div", "row click" + (open[id] ? " sel" : ""),
      el("span", null, el("span", "mark-col", open[id] ? "▼" : "▶"), el("span", "name", e.label),
         e.plan ? badge("subscription") : null, e.plan ? el("span", "plan", `${usd(e.plan.usd)}/${e.plan.period}`) : null,
         y.usd > 0.005 || !e.plan ? badge("api") : null),
      ...cells(y));
    r.tabIndex = 0;
    const toggle = () => { open[id] = !open[id]; drawSpend(last); };
    r.addEventListener("click", toggle);
    r.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } });
    prov.append(r);
    if (open[id]) {
      const d = el("div", "detail");
      for (const m of (e.models || []).filter((m) => m.periods[period])) {
        const mr = el("div", "row", el("span", null, m.model || "unknown"), ...cells(m.periods[period]));
        if (m.share_tokens != null) mr.title = `${Math.round(m.share_tokens * 100)}% of ${e.label}'s tokens`;
        d.append(mr);
      }
      d.append(el("div", "sub", `likely range ${y.projected ? rng(y.projected.cost, usd) : "–"} · ${nextName.toLowerCase()} `
        + (y.next && y.next.cost ? `${usd(y.next.cost[1])} (${rng(y.next.cost, usd)})` : "–")));
      prov.append(d);
    }
  }
  if (x) prov.append(el("div", "row total", el("span", null, "All providers"), ...cells(x)));
  // what each provider is of the period's spend
  const bar = clear($("mixbar")), ml = clear($("mixlist")), tot = x && x.cost > 0 ? x.cost : 0;
  list.forEach((e, i) => {
    const y = e.periods[period], v = y.cost != null ? y.cost : y.usd || 0, share = tot ? (100 * v) / tot : 0;
    const segm = el("span", MIXC[i % MIXC.length]); segm.style.flex = String(Math.max(share, 1)); segm.title = `${e.label} ${Math.round(share)}%`;
    bar.append(segm);
    ml.append(el("div", "mixrow", el("span", "sw " + MIXC[i % MIXC.length]), el("span", "n", e.label, e.plan ? null : el("span", "dim", " · API")),
      el("span", null, usd2(v)), el("span", "r dim", `${Math.round(share)}%`)));
  });
  if (!list.length) ml.append(empty("No spend yet this period."));
  $("spend-note").textContent = "Spend is pay-as-you-go plus subscriptions, spread evenly over time, so two plans at the same price spend the same."
    + " API value is what the same usage would cost at pay-as-you-go prices: on a plan, that's what the plan saves you."
    + (Object.keys(sp.plans || {}).length ? "" : " On a subscription? Add its price: savetokens plan anthropic --usd 200 --per month");
}

// ── limits ──
function limitCard(o, now) {
  const api = o.kind === "api", goal = o.kind === "goal", money = api || (goal && o.unit === "usd");
  const crit = o.stage === "act" || o.stage === "last_call" || o.used >= 100, warn = !crit && !!o.eta;
  const tool = goal ? String(o.label || "").replace(/ budget$/, "") : (TOOLS[o.harness] || o.harness || "");
  const label = api ? `Budget, ${usd(o.budget_usd)} ${o.per || ""}` : goal ? `${money ? usd(o.amount) : tok(o.amount)} ${o.per || ""}`
    : `${String(o.label || "").replace(/^.* plan's /, "").replace(/^./, (c) => c.toUpperCase())}`;
  const card = tile("limit " + (o.kind || "") + (crit ? " crit" : warn ? " warn" : ""), null, true);
  card.prepend(el("span", "legend", "┤ ", tool, api ? " · API" : goal ? " · budget" : "", " ├"));
  card.append(el("span", "label", badge(o.kind), label, o.stage ? el("span", "stage", `· ${o.stage.replace("_", "-")}`) : null));
  card.append(el("span", "big", el("span", "num", pct(o.used)), el("span", "dim",
    api ? `${usd2(o.spent_usd)} spent` : goal ? `${money ? usd2(o.spent) : tok(o.spent)} spent` : "used")));
  const m = el("div", "meter");
  if (o.p10 != null && o.p90 != null) {
    const b = el("div", "band"); b.style.left = `${clamp(o.p10)}%`; b.style.width = `${clamp(o.p90) - clamp(o.p10)}%`; m.append(b);
  }
  const f = el("div", "fill"); f.style.width = `${clamp(o.used)}%`; m.append(f);
  if (o.p50 != null) { const t = el("div", "p50"); t.style.left = `calc(${clamp(o.p50)}% - 1px)`; m.append(t); }
  card.append(m);
  const end = api || goal ? "the period's end" : "reset";
  let main;
  if (o.used >= 100) main = `Used up until ${api || goal ? "the period ends" : "it resets"}`;
  else if (o.eta) main = `Runs out around ${when(o.eta, now)}, ${span(o.resets - o.eta)} early`;
  else if (o.p50 != null) main = `Likely ${pct(o.p50)} by ${end} · ${Math.round((o.p_hit || 0) * 100)}% risk`;
  else main = "Forecast on its way";
  card.append(el("span", "main", main));
  if (goal && o.counts) card.append(el("span", "dim small", `Counts ${o.counts}`));
  card._pop.append(kv(`likely at ${end}`, pct(o.p50)), kv("range", o.p10 != null ? `${pct(o.p10)}–${pct(o.p90)}` : "–"),
    kv("chance of running out", o.p_hit != null ? `${Math.round(o.p_hit * 100)}%` : "–"),
    kv(api || goal ? "period ends" : "resets", `${when(o.resets, now)} (in ${span(o.resets - now)})`),
    el("span", "faint sep", "bar: used now, then the likely range at reset; the white tick is the most likely point"));
  return card;
}

function drawLimits(s) {
  const box = clear($("limits")), list = (s.limits || []).filter((o) => o.kind !== "goal");
  if (!list.length) {
    box.append(empty("No limits known yet. Send a message in Claude Code or Codex with savetokens installed, or set an API budget: savetokens api hermes --budget 100"));
    return;
  }
  // plans first, then API budgets: each card's badge says which
  for (const o of [...list.filter((o) => o.kind !== "api"), ...list.filter((o) => o.kind === "api")]) box.append(limitCard(o, s.now));
}

let chartId = store.get("st_chart");
const SHORT = (c) => c.group === "providers" ? c.label.replace(" tokens (millions)", " tokens")
  : c.label.replace(" % of weekly limit", " plan limit").replace(" API $", " API spend");

function drawChart(s) {
  // every series the hub sends (each tool's limit, each provider's tokens); an older one sends just `demand`
  const list = (s.charts && s.charts.length) ? s.charts
    : s.demand ? [{ id: s.demand.pool || "demand", group: "limits", ...s.demand }] : [];
  const d = list.find((c) => c.id === chartId) || list.find((c) => s.demand && c.id === s.demand.pool) || list[0] || {};
  const pick = clear($("chart-pick"));
  if (list.length > 1) for (const c of list) {
    const b = el("button", null, SHORT(c));
    b.type = "button";
    b.setAttribute("aria-pressed", String(c.id === d.id));
    b.addEventListener("click", () => { chartId = c.id; store.set("st_chart", c.id); drawChart(last); });
    pick.append(b);
  }
  const plot = clear($("plot")), ticks = clear($("ticks")), axis = clear($("axis"));
  const past = d.past || [], next = d.next || [], lo = d.next_lo || [], hi = d.next_hi || [];
  $("demand-label").textContent = d.label ? `${d.label}, per hour. Solid: used. Outline: forecast. Hover any hour.` : "";
  const unit = d.unit === "$" ? "$" : d.unit === "M" ? "M" : "%";
  const fmt = (v, dp = 1) => unit === "$" ? `$${v.toFixed(dp + 1)}` : unit === "M" ? `${v.toFixed(v < 10 ? dp : 0)}M` : `${v.toFixed(dp)}%`;
  const sum = (a) => a.reduce((x, y) => x + (y || 0), 0);
  $("demand-sum").textContent = past.length || next.length ? `used ${fmt(sum(past))} in the last ${past.length} h, ~${fmt(sum(next))} to come` : "";
  if (!past.some((v) => v > 0) && !next.some((v) => v > 0)) { plot.append(empty("No usage yet.")); clear($("readout")); return; }
  const tickTxt = (v) => unit === "$" ? `$${fmtNum(v)}` : unit === "M" ? `${fmtNum(v)}M` : `${fmtNum(v)}%`;
  const top = niceTop(Math.max(0.0001, ...past, ...next, ...hi));
  for (const v of [top, top / 2, 0]) ticks.append(el("span", null, tickTxt(v)));
  plot.classList.toggle("dense", past.length + next.length > 72);
  const bars = [];
  past.forEach((v, i) => bars.push({ cls: "past", v, hour: d.start != null ? d.start + i * 3600 : null }));
  next.forEach((v, i) => bars.push({ cls: "next", v, lo: lo[i], hi: hi[i], hour: d.now_hour != null ? d.now_hour + i * 3600 : null, first: i === 0 }));
  const readout = (b) => {
    const r = clear($("readout"));
    if (!b) return;
    const label = b.first ? `now (${hm(b.hour)})` : b.hour != null ? when(b.hour, s.now) : "";
    r.append(label + " ", el("b", null, b.cls === "past" ? `${fmt(b.v)} used`
      : `likely ${fmt(b.v)}` + (b.lo != null && b.hi != null ? ` (${fmt(b.lo)}–${fmt(b.hi)})` : "")));
  };
  bars.forEach((b) => {
    const col = el("div", `col ${b.cls}` + (b.first ? " now" : ""));
    col.title = b.hour != null ? when(b.hour, s.now) : "";
    if (b.cls === "next" && b.hi != null) {
      const r = el("div", "rng"); r.style.bottom = `${(100 * (b.lo || 0)) / top}%`;
      r.style.height = `${(100 * Math.max(0, b.hi - (b.lo || 0))) / top}%`; col.append(r);
    }
    const v = el("div", "val"); v.style.height = `${(100 * b.v) / top}%`; col.append(v);
    col.addEventListener("mouseenter", () => { plot.classList.add("hovering"); col.classList.add("hover"); readout(b); });
    col.addEventListener("mouseleave", () => { plot.classList.remove("hovering"); col.classList.remove("hover");
                                               readout(bars[past.length] || bars[bars.length - 1]); });
    plot.append(col);
  });
  readout(bars[past.length] || bars[bars.length - 1]);
  const end = d.now_hour != null ? d.now_hour + (next.length - 1) * 3600 : null;
  for (const x of [d.start != null ? when(d.start, s.now) : "", "now", end != null ? when(end, s.now) : ""]) axis.append(el("span", null, x));
}

// ── sessions ──
const SESSIONS_SHOWN = 5;
function pauseText(x, now) {
  const w = x.if_stopped;
  if (!x.running) return "Not running: nothing to pause. Its usage still counts toward today's total.";
  if (!w) return "No forecast for its limit yet, so no estimate of what pausing it would change.";
  const lim = w.short || (w.limit === "five_hour" ? "5-hour" : "weekly");
  if (w.eta) return w.eta_if_stopped ? `Pausing it moves the run-out from ${when(w.eta, now)} to ${when(w.eta_if_stopped, now)}.`
    : `Pausing it gets you to the reset: you'd no longer run out at ${when(w.eta, now)}.`;
  return `Pausing it saves about ${w.adds.toFixed(1)} points of the ${lim} limit over the next ${w.hours || 5} hours.`;
}

function drawSessions(s) {
  const box = clearRows($("sessions")), rows = (s.sessions || []).filter((x) => x.session != null);
  const rest = (s.sessions || []).find((x) => x.session == null);
  const running = rows.filter((x) => x.running).length;
  $("sessions-aside").textContent = rows.length ? `${running} running` : "";
  const more = $("sessions-more");
  if (!rows.length) { box.append(empty("No sessions in the last 24 hours.")); more.hidden = true; return; }
  const top = Math.max(1, ...rows.map((x) => x.pct_week || 0));
  const shown = allSessions || rows.some((x, i) => i >= SESSIONS_SHOWN && x.session === sel) ? rows : rows.slice(0, SESSIONS_SHOWN);
  for (const x of shown) {
    const id = x.session, name = x.project || x.session;
    const tool = (TOOLS[x.harness] || x.harness || "") + (x.pool && x.pool.endsWith(":api") ? " · API" : "");
    const fill = el("i"); fill.style.width = `${(100 * (x.pct_week || 0)) / top}%`;
    const r = el("div", "row click" + (sel === id ? " sel" : ""), el("span", "dot" + (x.running ? " on" : "")),
      el("span", "name", name, x.project ? el("span", "sid", id) : null), el("span", "tool", tool),
      el("span", "todaybar", el("span", "track", fill), el("span", "v", pct(x.pct_week, 1))),
      el("span", "r", x.running && x.pace ? pct(x.pace, 1) : "–"));
    r.tabIndex = 0;
    const pick = () => { sel = sel === id ? null : id; drawSessions(last); };
    r.addEventListener("click", pick);
    r.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
    box.append(r);
    if (sel === id) {
      box.append(el("div", "sdetail", el("span", "cap", "IF YOU PAUSE IT · NEXT 5 H"), el("span", "what", pauseText(x, s.now)),
        el("span", "facts", `${x.model || "–"} · ${String(x.machine || "?").slice(0, 12)} · ${(x.requests || 0).toLocaleString("en-US")} requests`
          + ` · ${Math.round((x.subagents || 0) * 100)}% subagents · since ${when(x.first, s.now)}`
          + (x.running ? "" : ` · last ${ago(x.last, s.now)}`))));
    }
  }
  if (rest && allSessions) box.append(el("div", "row rest", el("span"), el("span", null, rest.project), el("span"),
    el("span", "todaybar", el("span", "track"), el("span", "v", pct(rest.pct_week, 1))), el("span")));
  const hidden = rows.length - SESSIONS_SHOWN;
  more.hidden = hidden <= 0 && !rest;
  more.textContent = allSessions ? "− show fewer" : `+ ${hidden > 0 ? `${hidden} more session${hidden === 1 ? "" : "s"}` : "the rest"}`;
}

// ── models, alerts, machines, hits ──
const HIT = (h) => ({ session: "5-hour limit", weekly: "Weekly limit" }[h.kind] || (h.model ? `${h.model} limit` : "A limit"));
function drawLists(s) {
  const models = clear($("models")), by = {};
  for (const m of s.models || []) (by[m.harness] = by[m.harness] || []).push(m);
  if (!Object.keys(by).length) models.append(empty("No usage this week."));
  for (const [h, list] of Object.entries(by)) {
    models.append(el("div", "group", TOOLS[h] || h));
    for (const m of list.slice(0, 5)) {
      const fill = el("i"); fill.style.width = `${Math.round(m.share * 100)}%`;
      const row = el("div", "model", el("span", "n", m.model || "unknown"), el("span", "track", fill), el("b", null, `${Math.round(m.share * 100)}%`));
      if (m.subagents >= 0.05) row.title = `${Math.round(m.subagents * 100)}% via subagents`;
      models.append(row);
    }
  }
  const mach = clear($("machines"));
  if (!(s.machines || []).length) mach.append(empty("No machines have synced this week."));
  for (const m of s.machines || []) {
    const off = m.last_sync && s.now - m.last_sync > QUIET;
    mach.append(el("div", "stack", el("span", "n", String(m.machine || "?").slice(0, 12), off ? el("span", "badge quiet", "QUIET") : null),
      el("span", "s", `${(m.requests || 0).toLocaleString("en-US")} requests · `
        + (m.last_sync ? `synced ${ago(m.last_sync, s.now)}` : m.last_seen ? `last seen ${ago(m.last_seen, s.now)}` : "no usage this week")),
      off ? el("span", "s warnc", "Not reaching this hub. Switched off, or pointed at an old address: add it again with ADD MACHINE.") : null));
  }
  const accts = s.accounts || [];
  if (accts.length > 1) mach.append(el("div", "s dim", `Claude accounts: ${accts.map((a) => (a.account || "?") + (a.active ? " (in use)" : "")).join(", ")}`));
  const al = clear($("alerts"));
  if (!(s.alerts || []).length) al.append(empty("No alerts this week."));
  (s.alerts || []).forEach((a, i) => {
    const row = el("div", "alert" + (alertOpen[i] ? " open" : ""), el("span", "when", when(a.ts, s.now)), el("span", "what", a.message),
      el("span", "sign", alertOpen[i] ? "−" : "+"));
    row.tabIndex = 0;
    const toggle = () => { alertOpen[i] = !alertOpen[i]; drawLists(last); };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } });
    al.append(row);
  });
  const hits = clear($("hits"));
  if (!(s.hits || []).length) hits.append(empty("You haven't run out in the last 30 days."));
  for (const h of s.hits || []) hits.append(el("div", "hit", el("span", null, HIT(h)), el("span", "dim", when(h.ts, s.now))));
}

function drawTrack(s) {
  const tr = Object.entries(s.track_record || {}).filter(([k]) => k !== "at").sort((a, b) => a[1].mae - b[1].mae);
  $("track").textContent = tr.length
    ? "Forecast track record: " + tr.map(([p, r]) => `${p} off by ${r.mae.toFixed(2)} points an hour over ${r.forecasts} forecasts`).join("; ") + "."
    : "No forecast track record yet.";
  $("track").hidden = false;
}

// ── projects ──
const BILL = { plan: "subscription", api: "api" };
const PROVIDERS = { anthropic: "Anthropic", openai: "OpenAI", openrouter: "OpenRouter", google: "Google" };
// where the tokens went: one segment per tool, provider and plan or API
function mix(t) {
  const tot = t.tokens || 1, bar = el("div", "mix");
  (t.by || []).forEach((b, i) => {
    const seg = el("span", `seg-${b.billing}` + (i % 2 ? " alt" : ""));
    seg.style.width = `${(100 * b.tokens) / tot}%`; bar.append(seg);
  });
  bar.title = (t.by || []).map(splitText(tot)).join("\n");
  return bar;
}
const splitText = (tot) => (b) => `${TOOLS[b.tool] || b.tool} ${b.billing === "api" ? "API" : "plan"}`
  + (b.billing === "api" || b.tool === "hermes" ? ` (${PROVIDERS[b.provider] || b.provider})` : "") + ` ${Math.round((100 * b.tokens) / tot)}%`;

function drawProjects(s) {
  const pv = s.projects, grid = clear($("projects")), cards = clear($("budget-cards"));
  if (!pv || pv.error) { grid.append(empty(pv ? `Couldn't add up projects: ${pv.error}` : "No projects yet.")); return; }
  $("projects-days").textContent = `PROJECTS · LAST ${pv.days} DAYS · hover a project for where its tokens went and its tasks`;
  const all = pv.list.reduce((a, p) => a + (p.tokens || 0), 0) || 1;
  for (const p of [...pv.list].sort((a, b) => (b.value_usd || 0) - (a.value_usd || 0))) {
    const name = p.name || `project ${p.id.slice(0, 8)}`;
    const c = tile("proj", null, true);
    c.prepend(el("span", "legend" + (p.name ? "" : " anon"), `┤ ${name} ├`));
    const days = p.last_days || [], hi = Math.max(1, ...days);
    const spark = el("div", "spark");
    days.forEach((v, i) => { const b = el("span", i === days.length - 1 ? "last" : null); b.style.height = `${Math.max(6, (100 * v) / hi)}%`; spark.append(b); });
    spark.title = `tokens a day, the last ${days.length} days`;
    c.append(el("div", "top-row", el("span", null, el("div", "big", usd2(p.value_usd)),
      el("div", "dim small", p.api_usd > 0.005 ? `at API prices · ${usd(p.api_usd)} paid` : "at API prices")), days.length ? spark : null));
    c.append(mix(p));
    const n = p.sessions;
    c.append(el("span", "kv", el("span", null, `${tok(p.tokens)} tokens` + (pv.more ? "" : ` · ${Math.round((100 * p.tokens) / all)}%`)),
      el("span", null, n == null ? "" : `${n} session${n === 1 ? "" : "s"}`)));
    const pop = c._pop;
    pop.append(el("span", "head", "WHERE ITS TOKENS WENT"));
    for (const b of p.by) pop.append(el("span", "kv", el("span", null, badge(BILL[b.billing]), `${TOOLS[b.tool] || b.tool} · ${PROVIDERS[b.provider] || b.provider}`),
      el("span", null, `${Math.round((100 * b.tokens) / (p.tokens || 1))}%`)));
    pop.append(kv("paid as API", p.api_usd > 0.005 ? usd2(p.api_usd) : "–"), kv("requests", (p.requests || 0).toLocaleString("en-US")));
    for (const o of pv.budgets.filter((o) => ((o.scope || {}).projects || []).includes(p.id)))
      pop.append(el("span", "kv", el("span", null, badge("goal"), o.label), el("span", null, `${pct(o.used)} used`)));
    if (p.tasks.length) {
      pop.append(el("span", "head", "BIGGEST TASKS"));
      for (const t of p.tasks.slice(0, 4)) pop.append(kv((t.name || `task ${t.id.slice(0, 8)}`) + (t.done ? " ✓" : ""), `${tok(t.tokens)} · ${usd(t.value_usd)}`));
      if (p.tasks.length > 4 || p.more_tasks) pop.append(el("span", "faint", `More: savetokens tasks --project ${p.name || "NAME"}`));
    }
    grid.append(c);
  }
  if (!pv.list.length) grid.append(empty("No usage with a project yet: it's counted from the folder each session runs in."));
  const notes = [];
  if (pv.more) notes.push(`${pv.more} smaller projects: savetokens projects`);
  if (pv.unnamed) notes.push("Names stay on your machines, so this hub shows ids. To see names here, run on each machine: savetokens projects --share-names on");
  $("projects-note").textContent = notes.join(" · ");
  for (const o of pv.budgets) cards.append(limitCard(o, s.now));
  if (!pv.budgets.length) cards.append(empty("No budgets yet. e.g. savetokens budget --project webapp --usd 150 --per month (API spend), add --count all to count plan usage too"));
}

// ── menus: setup and add a machine ──
function closeMenus(except) {
  for (const m of document.querySelectorAll("[data-menu]")) if (m !== except) {
    m.querySelector(".menu").hidden = true; m.querySelector(".chip").setAttribute("aria-expanded", "false");
  }
}
function menu(host, onOpen) {
  const btn = host.querySelector(".chip"), box = host.querySelector(".menu");
  btn.addEventListener("click", () => {
    const on = box.hidden;
    closeMenus(host);
    box.hidden = !on; btn.setAttribute("aria-expanded", String(on));
    if (on && onOpen) onOpen();
  });
}
document.addEventListener("mousedown", (e) => { if (!(e.target.closest && e.target.closest("[data-menu]"))) closeMenus(); });

function drawSetup(s) {
  const steps = s.setup || [], left = steps.filter((x) => !x.done), needed = left.filter((x) => !x.optional);
  $("setup").hidden = !left.length;
  if (!left.length) return;
  const done = steps.length - left.length;
  $("setup-count").textContent = `${done}/${steps.length}`;
  $("setup-done").textContent = `${done} of ${steps.length} done`;
  $("setup-title").textContent = needed.length ? "FINISH SETTING UP" : "SETUP";
  $("setup-bar").style.width = `${(100 * done) / (steps.length || 1)}%`;
  const list = clear($("setup-steps"));
  for (const x of left) {
    const copy = el("button", "copy", "COPY");
    copy.type = "button";
    copyButton(copy, () => x.command);
    list.append(el("li", x.optional ? "optional" : null,
      el("span", "st-title", x.title, x.optional ? el("span", "tag", " · optional") : null),
      el("span", "dim small", x.detail),
      x.command ? el("div", "cmd", el("code", null, x.command), copy) : null));
  }
}
menu($("setup"));

function copyButton(btn, text) {
  btn.onclick = () => navigator.clipboard?.writeText(text()).then(() => { btn.textContent = "COPIED"; setTimeout(() => { btn.textContent = "COPY"; }, 1500); });
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
                                body: JSON.stringify(body || {}) });
  const got = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(got.error || `HTTP ${r.status}`);
  return got;
}
menu($("adder"), async () => {
  if ($("adder").dataset.made) return;
  $("add-err").textContent = "";
  try {
    const got = await post("v1/pair", { machines: 10, hours: 24 });
    const url = got.url || location.origin;
    $("add-cmd").textContent = got.command || `curl -fsSL ${url}/join | sh -s ${got.code}`;
    $("add-join").textContent = `savetokens join ${url} ${got.code}`;
    $("adder").dataset.made = "1";
  } catch (e) { $("add-err").textContent = `Couldn't make a code (${e.message}).`; }
});
copyButton($("add-copy"), () => $("add-cmd").textContent);
copyButton($("add-join-copy"), () => $("add-join").textContent);
$("add-token").addEventListener("click", async () => {
  try {
    const got = await post("v1/token");
    $("add-env").textContent = `SAVETOKENS_SERVER=${got.url || location.origin}\nSAVETOKENS_TOKEN=${got.token}`;
    $("add-env-row").hidden = $("add-env-note").hidden = false;
    $("add-token").disabled = true;
  } catch (e) { $("add-err").textContent = `Couldn't make a token (${e.message}).`; }
});
copyButton($("add-env-copy"), () => $("add-env").textContent);

// ── tabs, period, live ──
function showTab(t) {
  tab = t;
  store.set("st_tab", t);
  $("spend").hidden = t !== "spend";
  $("limits-tab").hidden = t !== "limits";
  $("projects-tab").hidden = t !== "projects";
  for (const k of ["spend", "limits", "projects"]) $(`tab-${k}`).setAttribute("aria-selected", String(t === k));
}
function setPeriod(p) { period = p; store.set("st_period", p); if (last) drawSpend(last); }
function setLive(on) {
  live = on;
  store.set("st_live", on ? "on" : "off");
  $("live-dot").classList.toggle("paused", !on);
  $("pop-updates").textContent = on ? "live, every 30 seconds" : "paused";
  $("live").textContent = on ? "pause live updates" : "resume live updates";
  $("key-p").textContent = on ? "pause" : "resume";
  clearTimeout(timer);
  if (on) timer = setTimeout(load, REFRESH_MS);
}
$("tab-spend").addEventListener("click", () => showTab("spend"));
$("tab-limits").addEventListener("click", () => showTab("limits"));
$("tab-projects").addEventListener("click", () => showTab("projects"));
$("live").addEventListener("click", () => setLive(!live));
$("sessions-more").addEventListener("click", () => { allSessions = !allSessions; if (last) drawSessions(last); });

// ── the command bar ──
let msgTimer = null;
function say(text, ok = true) {
  const m = $("msg"); m.textContent = text; m.classList.toggle("bad", !ok);
  clearTimeout(msgTimer); msgTimer = setTimeout(() => { m.textContent = ""; }, 3000);
}
function openCmd() { $("cmd-form").hidden = false; $("keys").hidden = true; $("cmd").value = ""; $("cmd").focus(); }
function closeCmd() { $("cmd-form").hidden = true; $("keys").hidden = false; }
function run(raw) {
  const c = raw.trim().toLowerCase();
  closeCmd();
  if (!c) return;
  if (c === "spend" || c === "limits" || c === "projects") { showTab(c); return say(`Showing ${c}`); }
  if (["day", "today", "week", "month"].includes(c)) { setPeriod(c === "today" ? "day" : c); showTab("spend"); return say(`Period: ${PERIOD[period][0].toLowerCase()}`); }
  if (c === "refresh") { load(); return say("Refreshing"); }
  if (c === "pause" || c === "resume") { setLive(c === "resume"); return say(c === "pause" ? "Live updates paused" : "Live updates on"); }
  if (c.startsWith("sel ")) {
    const q = c.slice(4).trim(), x = (last && last.sessions || []).find((x) => x.session && (x.session.startsWith(q) || (x.project || "").toLowerCase() === q));
    if (x) { sel = x.session; showTab("limits"); drawSessions(last); return say(`Selected ${x.project || x.session}`); }
  }
  say(`Unknown command: ${c}`, false);
}
const KEYS = { "/": openCmd, "1": () => showTab("spend"), "2": () => showTab("limits"), "3": () => showTab("projects"), r: () => { load(); say("Refreshing"); }, p: () => setLive(!live) };
for (const b of document.querySelectorAll("#keys button")) b.addEventListener("click", () => KEYS[b.dataset.key]());
$("cmd-form").addEventListener("submit", (ev) => { ev.preventDefault(); run($("cmd").value); });
$("cmd").addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeCmd(); });
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenus();
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || e.metaKey || e.ctrlKey || e.altKey || $("view").hidden) return;
  const f = KEYS[e.key.toLowerCase()];
  if (f) { if (e.key === "/") e.preventDefault(); f(); }
});

// ── clock ──
function tick() { const d = new Date(); $("clock").textContent = `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`; }
setInterval(tick, 1000);
tick();

// ── drawing and loading ──
function draw(s) {
  for (const f of [drawMeta, drawSetup, drawHeadline, drawSpend, drawLimits, drawProjects, drawChart, drawSessions, drawLists, drawTrack]) {
    try { f(s); } catch (e) { console.error(f.name, e); }
  }
}

function show(which) {
  $("signin").hidden = which !== "signin";
  $("view").hidden = which !== "view";
  $("signout").hidden = !store.get("st_token");
  $("adder").hidden = which !== "view" || !store.get("st_token");   // a hub, not `savetokens web`
  if (which !== "view") $("setup").hidden = true;
  document.querySelector(".tabs").hidden = document.querySelector(".bar").hidden = which !== "view";
}

let timer = null;
async function load() {
  clearTimeout(timer);
  const t = token();
  if (!t) { show("signin"); return; }
  try {
    const r = await fetch("v1/dashboard", { headers: { Authorization: `Bearer ${t}` }, cache: "no-store" });
    if (r.status === 401) { store.del("st_key"); store.del("st_token"); memToken = null; show("signin"); return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    last = await r.json();
    show("view");
    draw(last);
  } catch (e) {
    $("meta").textContent = `Couldn't refresh (${e.message}); trying again shortly.`;
    $("meta").hidden = false;
  }
  if (live) timer = setTimeout(load, REFRESH_MS);
}

$("signin-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("signin-err").textContent = "";
  try { await join($("code").value.trim()); $("code").value = ""; await load(); }
  catch (e) { $("signin-err").textContent = e.message; }
});
$("signout").addEventListener("click", () => { store.del("st_token"); store.del("st_key"); memToken = null; show("signin"); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && live) load(); });

setLive(live);
readFragment().then(() => showTab(tab)).catch((e) => { $("signin-err").textContent = e.message; }).finally(load);

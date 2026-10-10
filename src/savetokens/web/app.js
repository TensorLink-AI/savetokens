// savetokens browser view: reads /v1/dashboard and draws it. Read-only; no external code.
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
function when(ts, now) {
  if (ts == null) return "–";
  const d = new Date(ts * 1000), n = new Date(now * 1000);
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (d.toDateString() === n.toDateString()) return hm;
  return Math.abs(ts - now) < 6 * 86400 ? `${DAY[d.getDay()]} ${hm}`
    : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + hm;
}
function span(s) {
  s = Math.max(0, s);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 2 * 86400) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}
const pct = (v, d = 0) => (v == null ? "–" : `${v.toFixed(d)}%`);
const usd = (v) => (v == null ? "–" : v >= 100 ? `$${Math.round(v).toLocaleString()}` : `$${v.toFixed(2)}`);
const ago = (ts, now) => (ts ? `${span(now - ts)} ago` : "never");

// ── tiny DOM helper (text only: names come from users' data) ──
function el(tag, cls, ...kids) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}
const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}
function clear(node) { while (node.firstChild) node.firstChild.remove(); return node; }
function empty(text) { return el("div", "empty", text); }

// ── drawing ──
function drawMeta(s) {
  const src = { ephemeris: "Ephemeris", baseline: "local baseline" }[s.source] || "none yet";
  const parts = [`Updated ${when(s.now, s.now)}`, `forecast: ${src}`];
  if (s.forecast_made_at) parts.push(`made ${ago(s.forecast_made_at, s.now)}`);
  const m = (s.machines || []).length;
  if (m) parts.push(`${m} machine${m === 1 ? "" : "s"} this week`);
  if (s.sync_error) parts.push(`server unreachable, showing this machine`);
  $("meta").textContent = parts.join(" · ");
}

function drawHeadline(s) {
  const h = s.headline || { level: "none", text: "" };
  const node = $("headline");
  node.className = `headline ${h.level}`;
  node.textContent = h.text;
}

function limitCard(o, now) {
  const api = o.kind === "api";
  const card = el("div", `card ${o.stage || ""}`);
  const stage = o.stage ? el("span", `chip ${o.stage}`, o.stage.replace("_", " ")) : null;
  card.append(el("div", "tool", el("span", null, TOOLS[o.harness] || o.harness || "", api ? " · API" : ""), stage));
  card.append(el("div", "label", api ? `Budget, ${usd(o.budget_usd)} ${o.per}` : o.label.replace(/^(Codex|Hermes) /, "")));
  card.append(el("div", "big", pct(o.used), el("small", null, api ? `${usd(o.spent_usd)} spent` : "used")));

  const m = el("div", "meter" + (o.used >= 100 ? " over" : ""));
  const clamp = (v) => Math.max(0, Math.min(100, v));
  const fill = el("div", "fill"); fill.style.width = `${clamp(o.used)}%`; m.append(fill);
  if (o.p10 != null && o.p90 != null) {
    const b = el("div", "band"); b.style.left = `${clamp(o.p10)}%`; b.style.width = `${clamp(o.p90) - clamp(o.p10)}%`;
    m.append(b);
  }
  if (o.p50 != null) { const t = el("div", "p50"); t.style.left = `${clamp(o.p50)}%`; t.title = `likely ${pct(o.p50)} at reset`; m.append(t); }
  card.append(m, el("div", "scale", el("span", null, "0"), el("span", null, "100%")));

  const lines = el("div", "lines");
  const ranged = o.p10 != null && o.p90 != null && o.p90 - o.p10 >= 1;
  if (o.used >= 100) {
    lines.append(el("div", "out", `Used up until ${api ? "the " + o.period + " ends" : "it resets"}`));
  } else if (o.p50 != null) {
    lines.append(el("div", null, "Likely ", el("b", null, pct(o.p50)), ` by ${api ? "the " + o.period + "'s end" : "reset"}`,
                    ranged ? ` (${pct(o.p10)}–${pct(o.p90)})` : ""));
    lines.append(el("div", null, el("b", null, `${Math.round((o.p_hit || 0) * 100)}%`), " chance of running out first"));
  } else {
    lines.append(el("div", null, "Forecast on its way"));
  }
  if (o.used >= 100) { /* already said */ }
  else if (o.eta) lines.append(el("div", "out", `Runs out around ${when(o.eta, now)}, ${span(o.resets - o.eta)} early`));
  else if (o.eta_early) lines.append(el("div", null, `If it runs hot: out around ${when(o.eta_early, now)}`));
  lines.append(el("div", null, `${api ? "Period ends" : "Resets"} ${when(o.resets, now)} (in ${span(o.resets - now)})`));
  card.append(lines);
  return card;
}

function drawLimits(s) {
  const box = clear($("limits"));
  if (!s.limits || !s.limits.length) { box.append(empty("No limits known yet. Send a message in Claude Code or Codex with savetokens installed, or set an API budget: savetokens api hermes --budget 100")); return; }
  for (const o of s.limits) box.append(limitCard(o, s.now));
}

function drawChart(s) {
  const d = s.demand || {};
  const box = clear($("chart"));
  $("demand-label").textContent = d.label ? `${d.label}, per hour` : "";
  const past = d.past || [], next = d.next || [], lo = d.next_lo || [], hi = d.next_hi || [];
  if (!past.length && !next.length) { box.append(empty("No usage yet.")); return; }
  // drawn at the box's own size, so the text isn't stretched on a narrow screen
  const n = past.length + next.length, W = Math.max(280, box.clientWidth || 1000), H = W < 600 ? 160 : 200;
  const L = 40, B = 22, T = 8;
  const top = niceTop(Math.max(0.0001, ...past, ...next, ...hi));
  const x = (i) => L + (i * (W - L)) / n, bw = Math.max(1, (W - L) / n - 2);
  const y = (v) => T + (H - T - B) * (1 - v / top);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
                         "aria-label": "Usage per hour: the last 24 hours, then the forecast" });
  for (const f of [0, 0.5, 1]) {
    g.append(svg("line", { class: "axis", x1: L, x2: W, y1: y(top * f), y2: y(top * f) }));
    const t = svg("text", { class: "tick", x: L - 6, y: y(top * f) + 4, "text-anchor": "end" });
    t.textContent = d.unit === "$" ? `$${fmtNum(top * f)}` : `${fmtNum(top * f)}%`;
    g.append(t);
  }
  past.forEach((v, i) => v > 0 && g.append(svg("rect", { class: "past", x: x(i), y: y(v), width: bw, height: y(0) - y(v), rx: 1.5 })));
  next.forEach((v, i) => {
    const j = past.length + i;
    if (hi[i] != null) g.append(svg("rect", { class: "bandr", x: x(j), y: y(hi[i]), width: bw, height: Math.max(0, y(lo[i] || 0) - y(hi[i])) }));
    if (v > 0) g.append(svg("rect", { class: "next", x: x(j), y: y(v), width: bw, height: y(0) - y(v), rx: 1.5 }));
  });
  const nx = x(past.length) - 1;
  g.append(svg("line", { class: "now", x1: nx, x2: nx, y1: T, y2: H - B }));
  const labels = [[0, d.start], [past.length, d.now_hour], [n - 1, d.now_hour + (next.length - 1) * 3600]];
  for (const [i, ts] of labels) {
    if (ts == null) continue;
    const t = svg("text", { class: "tick", x: Math.min(W - 4, Math.max(L, x(i))), y: H - 6,
                            "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle" });
    t.textContent = i === past.length ? "now" : when(ts, s.now);
    g.append(t);
  }
  box.append(g);
}
function niceTop(v) {
  const k = Math.pow(10, Math.floor(Math.log10(v)));
  return [1, 2, 2.5, 5, 10].map((m) => m * k).find((m) => m >= v);
}
function fmtNum(v) { return v >= 10 ? Math.round(v).toString() : String(Number(v.toPrecision(3))); }

function stopping(x, now) {
  const w = x.if_stopped;
  if (!x.running) return "not running";
  if (!w) return "–";
  if (w.eta) return `out ${when(w.eta, now)} → ${w.eta_if_stopped ? when(w.eta_if_stopped, now) : "after reset"}`;
  return `saves ${w.adds.toFixed(1)}% of ${w.short || (w.limit === "five_hour" ? "5-hour" : "weekly")}`;
}

function drawSessions(s) {
  const body = clear($("sessions").tBodies[0]);
  const rows = s.sessions || [];
  if (!rows.length) { const td = el("td", "empty", "No sessions in the last 24 hours."); td.colSpan = 6; body.append(el("tr", null, td)); return; }
  for (const x of rows) {
    const tr = el("tr");
    const name = x.session == null ? x.project : (x.project || x.session);
    tr.append(el("td", null, el("span", "dot" + (x.running ? " on" : ""), "")),
              el("td", null, name, x.model ? el("div", "sub", x.model) : null),
              el("td", "tag", x.session == null ? "" : (TOOLS[x.harness] || x.harness || "") + (x.pool && x.pool.endsWith(":api") ? " · API" : "")),
              el("td", "num", pct(x.pct_week, 1)),
              el("td", "num", x.running && x.pace ? pct(x.pace, 1) : "–"),
              el("td", "sub", x.session == null ? "" : stopping(x, s.now)));
    body.append(tr);
  }
}

function drawModels(s) {
  const box = clear($("models"));
  const ms = s.models || [];
  if (!ms.length) { box.append(empty("No usage this week.")); return; }
  const by = {};
  for (const m of ms) (by[m.harness] = by[m.harness] || []).push(m);
  for (const [h, list] of Object.entries(by)) {
    box.append(el("div", "group", TOOLS[h] || h));
    for (const m of list.slice(0, 5)) {
      const bar = el("div", "bar"), i = el("i"); i.style.width = `${Math.round(m.share * 100)}%`; bar.append(i);
      box.append(el("div", "row", el("span", "name", m.model || "unknown"),
                    el("span", "val", `${Math.round(m.share * 100)}%`, m.subagents >= 0.05 ? ` · ${Math.round(m.subagents * 100)}% subagents` : ""), bar));
    }
  }
}

function drawMachines(s) {
  const box = clear($("machines"));
  const ms = s.machines || [];
  if (!ms.length) { box.append(empty("No machines have sent usage this week.")); return; }
  for (const m of ms) {
    box.append(el("div", "row", el("span", "name", el("code", null, String(m.machine || "?").slice(0, 8)),
                                   " ", el("span", "sub", `last seen ${ago(m.last_seen, s.now)}`)),
                  el("span", "val", `${(m.requests || 0).toLocaleString()} requests`, m.usd ? ` · ${usd(m.usd)} at API prices` : "")));
  }
  const accts = s.accounts || [];
  if (accts.length > 1) box.append(el("div", "note", `Claude accounts: ${accts.map((a) => (a.account || "?") + (a.active ? " (in use)" : "")).join(", ")}`));
}

function drawAlerts(s) {
  const box = clear($("alerts"));
  const as = s.alerts || [];
  if (!as.length) { box.append(empty("No alerts this week.")); return; }
  for (const a of as) box.append(el("div", "row", el("span", "name wrap", a.message), el("span", "val", when(a.ts, s.now))));
}

function drawHits(s) {
  const box = clear($("hits"));
  const hs = s.hits || [];
  if (!hs.length) { box.append(empty("You haven't run out in the last 30 days.")); return; }
  const what = (h) => ({ session: "5-hour limit", weekly: "weekly limit" }[h.kind] || (h.model ? `${h.model} limit` : "a limit"));
  for (const h of hs) box.append(el("div", "row", el("span", "name", what(h)), el("span", "val", when(h.ts, s.now))));
}

function drawTrack(s) {
  const tr = Object.entries(s.track_record || {}).sort((a, b) => a[1].mae - b[1].mae);
  $("track").textContent = tr.length
    ? "Forecast track record: " + tr.map(([p, r]) => `${p} off by ${r.mae.toFixed(2)} points an hour over ${r.forecasts} forecasts`).join("; ") + "."
    : "";
}

// ── spend ──
const PERIOD = { day: ["Today", "Next 24 hours", "by midnight"], week: ["This week", "Next 7 days", "by Sunday night"],
                 month: ["This month", "Next 30 days", "by month's end"] };
let period = store.get("st_period") || "month";
function tok(v) {
  if (v == null) return "–";
  for (const [u, d] of [["B", 1e9], ["M", 1e6], ["k", 1e3]]) if (v >= d) return `${(v / d).toFixed(v / d >= 100 ? 0 : 1)}${u}`;
  return String(Math.round(v));
}
const range = (q, f) => (q && f(q[0]) !== f(q[2]) ? el("span", "range", `${f(q[0])}–${f(q[2])}`) : null);
function kv(k, v) { return el("div", "kv", el("span", null, k), el("b", null, v)); }

function drawSpend(s) {
  const sp = s.spend;
  const periods = clear($("periods")), coming = clear($("coming")), table = $("providers");
  for (const b of [...table.tBodies]) b.remove();
  if (!sp || sp.error || !sp.periods) {
    periods.append(empty(sp && sp.error ? `Couldn't work out spend: ${sp.error}` : "No usage yet."));
    return;
  }
  const src = { ephemeris: "Ephemeris", baseline: "local baseline" }[sp.source];
  $("spend-src").textContent = src ? `forecast: ${src}` : "forecast on its way";
  for (const [k, [name, nextName, by]] of Object.entries(PERIOD)) {
    const x = sp.periods[k];
    if (!x) continue;
    const pc = x.projected.cost, pt = x.projected.tokens;
    const card = el("div", "card",
      el("div", "tool", el("span", null, name)),
      el("div", "big", usd(x.cost), el("small", null, "so far")),
      kv("tokens", tok(x.tokens)),
      kv("pay as you go", usd(x.usd)),
      x.fixed_usd ? kv("subscriptions", usd(x.fixed_usd)) : null,
      x.api_value > x.usd + 0.005 ? kv("API value of all usage", usd(x.api_value)) : null);
    const proj = el("div", "proj");
    if (pc) {
      proj.append(kv(`likely ${by}`, usd(pc[1])), el("div", "kv", el("span", null, "range"), el("b", null, `${usd(pc[0])}–${usd(pc[2])}`)),
                  kv("tokens", `${tok(pt[1])} (${tok(pt[0])}–${tok(pt[2])})`));
    } else proj.append(el("div", "kv", el("span", null, "Forecast on its way")));
    card.append(proj);
    periods.append(card);
    const n = x.next;
    coming.append(el("div", "card",
      el("div", "tool", el("span", null, nextName)),
      el("div", "big small", n.cost ? usd(n.cost[1]) : "–", el("small", null, n.cost ? `${usd(n.cost[0])}–${usd(n.cost[2])}` : "")),
      kv("tokens", n.tokens ? `${tok(n.tokens[1])} (${tok(n.tokens[0])}–${tok(n.tokens[2])})` : "–"),
      n.fixed_usd ? kv("of which subscriptions", usd(n.fixed_usd)) : null));
  }
  $("next-h").textContent = PERIOD[period][1];
  for (const b of document.querySelectorAll(".seg button")) b.setAttribute("aria-pressed", String(b.dataset.period === period));
  const total = sp.periods[period];
  const body = el("tbody");
  const cells = (x, perMtok) => [
    el("td", "num", tok(x.tokens)), el("td", "num", usd(x.cost != null ? x.cost : x.usd)),
    el("td", "num", x.projected && x.projected.cost ? usd(x.projected.cost[1]) : x.projected_usd != null ? usd(x.projected_usd) : "–",
       x.projected && x.projected.cost ? range(x.projected.cost, usd) : null),
    el("td", "num", x.next && x.next.cost ? usd(x.next.cost[1]) : x.next_usd != null ? usd(x.next_usd) : "–",
       x.next && x.next.cost ? range(x.next.cost, usd) : null),
    el("td", "num", perMtok == null ? "" : perMtok ? usd(perMtok) : "plan")];
  for (const e of sp.providers) {
    const x = e.periods[period];
    if (!x) continue;
    const tr = el("tr", "prov");
    tr.append(el("td", null, e.label, e.plan ? el("span", "chip plan", `plan ${usd(e.plan.usd)}/${e.plan.period}`) : null),
              ...cells(x, e.plan && !e.usd_per_mtok ? 0 : e.usd_per_mtok));
    tr.tabIndex = 0;
    const models = (e.models || []).filter((m) => m.periods[period]).map((m) => {
      const r = el("tr", "model");
      r.hidden = true;
      r.append(el("td", null, m.model, m.share_tokens != null ? el("span", "range", `${Math.round(m.share_tokens * 100)}% of its tokens this week`) : null),
               ...cells(m.periods[period], null));
      return r;
    });
    const toggle = () => { tr.classList.toggle("open"); for (const r of models) r.hidden = !r.hidden; };
    tr.addEventListener("click", toggle);
    tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } });
    body.append(tr, ...models);
  }
  const t = el("tr", "total");
  t.append(el("td", null, "All providers"), ...cells(total, null));
  body.append(t);
  table.append(body);
  $("spend-note").textContent = "Spend is pay-as-you-go plus subscriptions, spread evenly over time. Click a provider for its models."
    + (Object.keys(sp.plans || {}).length ? "" : " On a subscription? Add its price: savetokens plan anthropic --usd 200 --per month");
}

for (const b of document.querySelectorAll(".seg button")) {
  b.addEventListener("click", () => { period = b.dataset.period; store.set("st_period", period); if (last) drawSpend(last); });
}

let tab = store.get("st_tab") || "spend";
function drawSetup(s) {
  const steps = s.setup || [], box = $("setup"), list = clear($("setup-steps"));
  const left = steps.filter((x) => !x.done), needed = left.filter((x) => !x.optional);
  box.hidden = !left.length;
  if (!left.length) return;
  $("setup-title").textContent = needed.length ? "Finish setting up" : "Setup";
  $("setup-count").textContent = ` ${steps.length - left.length} of ${steps.length} done`;
  const sig = left.map((x) => x.id).join("|");
  if (box.dataset.sig !== sig) { box.dataset.sig = sig; box.open = needed.length > 0 && store.get("st_setup_closed") !== sig; }
  for (const x of left) {
    const copy = el("button", "copy", "Copy");
    copy.type = "button";
    copy.addEventListener("click", () => {
      navigator.clipboard?.writeText(x.command).then(() => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1500); });
    });
    list.append(el("li", x.optional ? "optional" : null,
      el("div", "st-title", x.title, x.optional ? el("span", "tag", " · optional") : null),
      el("div", "sub", x.detail),
      x.command ? el("div", "cmd", el("code", null, x.command), copy) : null));
  }
}
$("setup").addEventListener("toggle", () => { if (!$("setup").open) store.set("st_setup_closed", $("setup").dataset.sig || ""); });

function showTab(t) {
  tab = t;
  store.set("st_tab", t);
  $("spend").hidden = t !== "spend";
  $("limits-tab").hidden = t !== "limits";
  $("tab-spend").setAttribute("aria-selected", String(t === "spend"));
  $("tab-limits").setAttribute("aria-selected", String(t === "limits"));
  if (t === "limits" && last) drawChart(last);   // drawn at its width, which a hidden tab doesn't have
}
$("tab-spend").addEventListener("click", () => showTab("spend"));
$("tab-limits").addEventListener("click", () => showTab("limits"));

function draw(s) {
  for (const f of [drawMeta, drawSetup, drawSpend, drawHeadline, drawLimits, drawChart, drawSessions, drawModels, drawMachines, drawAlerts, drawHits, drawTrack]) {
    try { f(s); } catch (e) { console.error(f.name, e); }
  }
}

// ── loading ──
function show(which) {
  $("signin").hidden = which !== "signin";
  $("view").hidden = which !== "view";
  $("signout").hidden = !store.get("st_token");
  $("refresh").hidden = which !== "view";
}

let timer = null, last = null;
let resizing = null;
window.addEventListener("resize", () => {
  clearTimeout(resizing);
  resizing = setTimeout(() => last && drawChart(last), 150);
});
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
  }
  timer = setTimeout(load, REFRESH_MS);
}

$("signin-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("signin-err").textContent = "";
  try { await join($("code").value.trim()); $("code").value = ""; await load(); }
  catch (e) { $("signin-err").textContent = e.message; }
});
$("refresh").addEventListener("click", load);
$("signout").addEventListener("click", () => { store.del("st_token"); store.del("st_key"); memToken = null; show("signin"); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

showTab(tab);
readFragment().catch((e) => { $("signin-err").textContent = e.message; }).finally(load);

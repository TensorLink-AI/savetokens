// savetokens browser view ("Mainframe Calm"): reads /v1/dashboard and draws it. Read-only; no external code.
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
  if (h.get("tab") === "spend" || h.get("tab") === "limits") tab = h.get("tab");   // a link straight to a tab
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
function clear(node, keep = 0) { while (node.childNodes.length > keep) node.lastChild.remove(); return node; }
const empty = (text) => el("div", "empty", text);

// ── view state ──
const PERIOD = { day: ["Today", "Next 24 hours", "by midnight", "24 h"], week: ["This week", "Next 7 days", "by Sunday night", "7 days"],
                 month: ["This month", "Next 30 days", "by month's end", "30 days"] };
let tab = store.get("st_tab") || "limits";
let period = store.get("st_period") || "month";
let live = store.get("st_live") !== "off";
let sel = null, more = store.get("st_more") === "1", hover = null;
const open = {};
let last = null;

// ── header, headline ──
function drawMeta(s) {
  const src = { ephemeris: "Ephemeris", baseline: "local baseline" }[s.source] || "none yet";
  const parts = [`forecast: ${src}`];
  if (s.forecast_made_at) parts.push(`made ${ago(s.forecast_made_at, s.now)}`);
  const m = (s.machines || []).length;
  if (m) parts.push(`${m} machine${m === 1 ? "" : "s"} this week`);
  if (s.sync_error) parts.push("server unreachable, showing this machine");
  $("meta").textContent = parts.join(" · ");
}

function drawHeadline(s) {
  const h = s.headline || { level: "none", text: "" };
  const level = { bad: "act", warn: "warn", ok: "ok" }[h.level] || "none";
  $("headline").className = `headline ${level}`;
  $("headline-label").textContent = { act: "┤ ACT NOW ├", warn: "┤ HEADS-UP ├", ok: "┤ ON TRACK ├" }[level] || "┤ STATUS ├";
  $("headline-text").textContent = h.text || "Nothing to report yet.";
}

// ── spend ──
function drawSpend(s) {
  const sp = s.spend, rows = $("periods"), prov = $("providers");
  clear(rows, 1); clear(prov, 1);
  if (!sp || sp.error || !sp.periods) {
    rows.append(empty(sp && sp.error ? `Couldn't work out spend: ${sp.error}` : "No usage yet."));
    $("hero-cost").textContent = "–"; $("hero-likely").textContent = "";
    return;
  }
  const src = { ephemeris: "Ephemeris", baseline: "local baseline" }[sp.source];
  $("spend-src").textContent = src ? `forecast: ${src}` + (s.forecast_made_at ? `, made ${ago(s.forecast_made_at, s.now)}` : "")
    : "forecast on its way";
  const month = sp.periods.month;
  if (month) {
    $("hero-cost").textContent = usd2(month.cost);
    const h = $("hero-likely"); clear(h);
    if (month.projected && month.projected.cost) h.append("likely ", el("b", null, usd(month.projected.cost[1])), ` ${PERIOD.month[2]}`);
    if (month.api_value > month.cost + 0.005) h.append(el("span", "dim", ` · usage worth ${usd(month.api_value)} at API prices`));
  }
  for (const [k, [name, , by, short]] of Object.entries(PERIOD)) {
    const x = sp.periods[k];
    if (!x) continue;
    const r = el("div", "row click" + (k === period ? " sel" : ""),
      el("span", null, el("span", "mark-col", k === period ? "▸" : ""), name),
      el("span", "num", usd2(x.cost)),
      el("span", "num", x.projected && x.projected.cost ? usd(x.projected.cost[1]) : "–"),
      el("span", "r", x.next && x.next.cost ? usd(x.next.cost[1]) : "–", el("span", "dim", ` / ${short}`)));
    r.title = `${name}: ${tok(x.tokens)} tokens; ${by} likely ${x.projected ? rng(x.projected.cost, usd) : "–"}`;
    r.addEventListener("click", () => setPeriod(k));
    rows.append(r);
  }
  $("period-name").textContent = PERIOD[period][0].toUpperCase();
  const cells = (x, last4) => [el("span", "r", tok(x.tokens)), el("span", "r spend", usd2(x.cost != null ? x.cost : x.usd)),
    el("span", "r", x.projected && x.projected.cost ? usd(x.projected.cost[1]) : x.projected_usd != null ? usd(x.projected_usd) : "–"),
    el("span", "r dim", last4)];
  for (const e of sp.providers || []) {
    const x = e.periods[period];
    if (!x) continue;
    const id = e.label;
    const perM = e.plan && !e.usd_per_mtok ? "plan" : e.usd_per_mtok ? usd(e.usd_per_mtok) : "–";
    const r = el("div", "row click" + (open[id] ? " sel" : ""),
      el("span", null, el("span", "mark-col", open[id] ? "−" : "+"), el("span", "name", e.label),
         e.plan ? el("span", "plan", `plan ${usd(e.plan.usd)}/${e.plan.period}`) : null),
      ...cells(x, perM));
    r.tabIndex = 0;
    const toggle = () => { open[id] = !open[id]; drawSpend(last); };
    r.addEventListener("click", toggle);
    r.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } });
    prov.append(r);
    if (open[id]) {
      const d = el("div", "detail", el("div", "sub",
        `Likely range ${x.projected ? rng(x.projected.cost, usd) : "–"} · ${PERIOD[period][1]} ${x.next && x.next.cost ? usd(x.next.cost[1]) : "–"}`
        + (x.next && x.next.cost ? ` (${rng(x.next.cost, usd)})` : "")));
      for (const m of (e.models || []).filter((m) => m.periods[period])) {
        const y = m.periods[period];
        d.append(el("div", "row", el("span", null, m.model || "unknown"), ...cells(y,
          m.share_tokens != null ? `${Math.round(m.share_tokens * 100)}% of tokens` : "")));
      }
      prov.append(d);
    }
  }
  const t = sp.periods[period];
  if (t) prov.append(el("div", "row total", el("span", null, "All providers"), ...cells(t, t.next && t.next.cost ? `next ${usd(t.next.cost[1])}` : "")));
  $("spend-note").textContent = "Spend is pay-as-you-go plus subscriptions, spread evenly over time."
    + (Object.keys(sp.plans || {}).length ? "" : " On a subscription? Add its price: savetokens plan anthropic --usd 200 --per month");
}

// ── limits ──
function limitCard(o, now) {
  const api = o.kind === "api";
  const crit = o.stage === "act" || o.stage === "last_call" || o.used >= 100, warn = !crit && !!o.eta;
  const tool = (TOOLS[o.harness] || o.harness || "") + (api ? " · API" : "");
  const label = api ? `Budget, ${usd(o.budget_usd)} ${o.per || ""}` : String(o.label || "").replace(/^(Codex|Hermes) /, "");
  const card = el("div", "limit" + (crit ? " crit" : warn ? " warn" : ""));
  card.append(el("span", "tool", `${tool} · ${label}`, o.stage ? el("span", `stage ${o.stage}`, o.stage.replace("_", "-")) : null));
  card.append(el("span", "big", el("span", "num", pct(o.used)), el("span", "dim", api ? `${usd2(o.spent_usd)} spent` : "used")));
  const m = el("div", "meter");
  if (o.p10 != null && o.p90 != null) {
    const b = el("div", "band"); b.style.left = `${clamp(o.p10)}%`; b.style.width = `${clamp(o.p90) - clamp(o.p10)}%`; m.append(b);
  }
  const f = el("div", "fill"); f.style.width = `${clamp(o.used)}%`; m.append(f);
  if (o.p50 != null) { const t = el("div", "p50"); t.style.left = `calc(${clamp(o.p50)}% - 1px)`; t.title = `likely ${pct(o.p50)}`; m.append(t); }
  card.append(m);
  let main;
  if (o.used >= 100) main = `Used up until ${api ? "the period ends" : "it resets"}`;
  else if (o.eta) main = `Runs out around ${when(o.eta, now)}, ${span(o.resets - o.eta)} early`;
  else if (o.p50 != null) main = `Likely ${pct(o.p50)} ${api ? "by the period's end" : "by reset"} · ${Math.round((o.p_hit || 0) * 100)}% risk`;
  else main = "Forecast on its way";
  card.append(el("span", "main", main));
  card.append(el("span", "dim", `${api ? "Period ends" : "Resets"} ${when(o.resets, now)} (in ${span(o.resets - now)})`));
  return card;
}

function drawLimits(s) {
  const box = clear($("limits"));
  if (!s.limits || !s.limits.length) {
    box.append(empty("No limits known yet. Send a message in Claude Code or Codex with savetokens installed, or set an API budget: savetokens api hermes --budget 100"));
    return;
  }
  for (const o of s.limits) box.append(limitCard(o, s.now));
}

function drawChart(s) {
  const d = s.demand || {}, plot = clear($("plot")), ticks = clear($("ticks")), axis = clear($("axis"));
  const past = d.past || [], next = d.next || [], lo = d.next_lo || [], hi = d.next_hi || [];
  $("demand-label").textContent = d.label ? `${d.label}, per hour. Solid: used. Outline: forecast. Hover any hour.` : "";
  if (!past.length && !next.length) { plot.append(empty("No usage yet.")); $("readout").textContent = ""; return; }
  const unit = d.unit === "$" ? "$" : "%";
  const fmt = (v, dp = 1) => (unit === "$" ? `$${v.toFixed(dp + 1)}` : `${v.toFixed(dp)}%`);
  const top = niceTop(Math.max(0.0001, ...past, ...next, ...hi));
  for (const v of [top, top / 2, 0]) ticks.append(el("span", null, unit === "$" ? `$${fmtNum(v)}` : `${fmtNum(v)}%`));
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
  bars.forEach((b, i) => {
    const col = el("div", `bar ${b.cls}` + (b.first ? " now" : ""));
    if (b.cls === "next" && b.hi != null) {
      const r = el("div", "rng"); r.style.bottom = `${(100 * (b.lo || 0)) / top}%`;
      r.style.height = `${(100 * Math.max(0, b.hi - (b.lo || 0))) / top}%`; col.append(r);
    }
    const v = el("div", "val"); v.style.height = `${(100 * b.v) / top}%`; col.append(v);
    col.addEventListener("mouseenter", () => { hover = i; plot.classList.add("hovering"); col.classList.add("hover"); readout(b); });
    col.addEventListener("mouseleave", () => { hover = null; plot.classList.remove("hovering"); col.classList.remove("hover");
                                               readout(bars[past.length] || bars[bars.length - 1]); });
    plot.append(col);
  });
  readout(bars[past.length] || bars[bars.length - 1]);
  const end = d.now_hour != null ? d.now_hour + (next.length - 1) * 3600 : null;
  for (const t of [d.start != null ? when(d.start, s.now) : "", "now", end != null ? when(end, s.now) : ""]) axis.append(el("span", null, t));
}

// ── sessions ──
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
  const box = clear($("sessions"), 1), rows = s.sessions || [];
  const running = rows.filter((x) => x.running).length;
  $("sessions-aside").textContent = rows.length ? `${running} running · select one to see what pausing it would do` : "";
  if (!rows.length) { box.append(empty("No sessions in the last 24 hours.")); return; }
  for (const x of rows) {
    if (x.session == null) {   // "N more"
      box.append(el("div", "row rest", el("span"), el("span", null, x.project), el("span"), el("span", "r", pct(x.pct_week, 1)), el("span")));
      continue;
    }
    const id = x.session, name = x.project || x.session;
    const tool = (TOOLS[x.harness] || x.harness || "") + (x.pool && x.pool.endsWith(":api") ? " · API" : "");
    const r = el("div", "row click" + (sel === id ? " sel" : ""), el("span", "dot" + (x.running ? " on" : "")),
      el("span", "name", name), el("span", null, tool), el("span", "r today", pct(x.pct_week, 1)),
      el("span", "r", x.running && x.pace ? pct(x.pace, 1) : "–"));
    r.tabIndex = 0;
    const pick = () => { sel = sel === id ? null : id; drawSessions(last); };
    r.addEventListener("click", pick);
    r.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
    box.append(r);
    if (sel === id) {
      box.append(el("div", "sdetail", el("span", "what", pauseText(x, s.now)),
        el("span", "facts", `${x.model || "–"} · ${String(x.machine || "?").slice(0, 8)}`, el("br"),
           `${(x.requests || 0).toLocaleString("en-US")} requests · ${Math.round((x.subagents || 0) * 100)}% via subagents`, el("br"),
           `first ${when(x.first, s.now)} · last ${x.running ? "just now" : ago(x.last, s.now)}`)));
    }
  }
}

// ── more: models, machines, alerts, hits ──
function drawMore(s) {
  $("more").hidden = !more;
  $("more-sign").textContent = more ? "−" : "+";
  const models = clear($("models")), by = {};
  for (const m of s.models || []) (by[m.harness] = by[m.harness] || []).push(m);
  if (!Object.keys(by).length) models.append(empty("No usage this week."));
  for (const [h, list] of Object.entries(by)) {
    models.append(el("div", "group", TOOLS[h] || h));
    for (const m of list.slice(0, 5)) models.append(el("div", "item", el("span", null, m.model || "unknown",
      m.subagents >= 0.05 ? el("span", "dim", ` · ${Math.round(m.subagents * 100)}% subagents`) : null), el("b", null, `${Math.round(m.share * 100)}%`)));
  }
  const mach = clear($("machines"));
  if (!(s.machines || []).length) mach.append(empty("No machines have sent usage this week."));
  for (const m of s.machines || []) {
    mach.append(el("div", "stack", el("span", "mach", String(m.machine || "?").slice(0, 12)),
      el("span", "seen", `last seen ${ago(m.last_seen, s.now)}`),
      el("span", null, `${(m.requests || 0).toLocaleString("en-US")} requests` + (m.usd ? ` · ${usd(m.usd)} at API prices` : ""))));
  }
  const accts = s.accounts || [];
  if (accts.length > 1) mach.append(el("div", "note", `Claude accounts: ${accts.map((a) => (a.account || "?") + (a.active ? " (in use)" : "")).join(", ")}`));
  const al = clear($("alerts"));
  if (!(s.alerts || []).length) al.append(empty("No alerts this week."));
  for (const a of s.alerts || []) al.append(el("div", "stack", el("span", "when", when(a.ts, s.now)), el("span", "what", a.message)));
  const hits = clear($("hits"));
  const what = (h) => ({ session: "5-hour limit", weekly: "Weekly limit" }[h.kind] || (h.model ? `${h.model} limit` : "A limit"));
  if (!(s.hits || []).length) hits.append(empty("You haven't run out in the last 30 days."));
  for (const h of s.hits || []) hits.append(el("div", "item", el("span", null, what(h)), el("span", "dim", when(h.ts, s.now))));
}

function drawTrack(s) {
  const tr = Object.entries(s.track_record || {}).filter(([k]) => k !== "at").sort((a, b) => a[1].mae - b[1].mae);
  $("track").textContent = tr.length
    ? "Forecast track record: " + tr.map(([p, r]) => `${p} off by ${r.mae.toFixed(2)} points an hour over ${r.forecasts} forecasts`).join("; ") + "."
    : "";
}

// ── setup and add a machine (unchanged behaviour) ──
function drawSetup(s) {
  const steps = s.setup || [], box = $("setup"), list = clear($("setup-steps"));
  const left = steps.filter((x) => !x.done), needed = left.filter((x) => !x.optional);
  box.hidden = !left.length;
  if (!left.length) return;
  $("setup-title").textContent = needed.length ? "FINISH SETTING UP" : "SETUP";
  $("setup-count").textContent = `${steps.length - left.length} of ${steps.length} done`;
  const sig = left.map((x) => x.id).join("|");
  if (box.dataset.sig !== sig) { box.dataset.sig = sig; box.open = needed.length > 0 && store.get("st_setup_closed") !== sig; }
  for (const x of left) {
    const copy = el("button", "copy", "COPY");
    copy.type = "button";
    copy.addEventListener("click", () => navigator.clipboard?.writeText(x.command).then(() => { copy.textContent = "COPIED"; setTimeout(() => { copy.textContent = "COPY"; }, 1500); }));
    list.append(el("li", x.optional ? "optional" : null,
      el("div", "st-title", x.title, x.optional ? el("span", "tag", " · optional") : null),
      el("div", "sub", x.detail),
      x.command ? el("div", "cmd", el("code", null, x.command), copy) : null));
  }
}
$("setup").addEventListener("toggle", () => { if (!$("setup").open) store.set("st_setup_closed", $("setup").dataset.sig || ""); });

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
$("adder").addEventListener("toggle", async () => {
  if (!$("adder").open || $("adder").dataset.made) return;
  $("add-err").textContent = "";
  try {
    const got = await post("v1/pair", { machines: 10, hours: 24 });
    $("add-cmd").textContent = got.command || `savetokens join ${location.origin} ${got.code}`;
    $("adder").dataset.made = "1";
  } catch (e) { $("add-err").textContent = `Couldn't make a code (${e.message}).`; }
});
copyButton($("add-copy"), () => $("add-cmd").textContent);
$("add-token").addEventListener("click", async () => {
  try {
    const got = await post("v1/token");
    $("add-env").textContent = `SAVETOKENS_SERVER=${got.url || location.origin}\nSAVETOKENS_TOKEN=${got.token}`;
    $("add-env-row").hidden = $("add-env-note").hidden = false;
    $("add-token").disabled = true;
  } catch (e) { $("add-err").textContent = `Couldn't make a token (${e.message}).`; }
});
copyButton($("add-env-copy"), () => $("add-env").textContent);

// ── tabs, period, live, more ──
function showTab(t) {
  tab = t;
  store.set("st_tab", t);
  $("spend").hidden = t !== "spend";
  $("limits-tab").hidden = t !== "limits";
  $("tab-spend").setAttribute("aria-selected", String(t === "spend"));
  $("tab-limits").setAttribute("aria-selected", String(t === "limits"));
}
function setPeriod(p) { period = p; store.set("st_period", p); if (last) drawSpend(last); }
function setLive(on) {
  live = on;
  store.set("st_live", on ? "on" : "off");
  $("live").classList.toggle("paused", !on);
  $("live-txt").textContent = on ? "live" : "paused";
  $("key-p").textContent = on ? "pause" : "resume";
  clearTimeout(timer);
  if (on) timer = setTimeout(load, REFRESH_MS);
}
$("tab-spend").addEventListener("click", () => showTab("spend"));
$("tab-limits").addEventListener("click", () => showTab("limits"));
$("live").addEventListener("click", () => setLive(!live));
$("more-toggle").addEventListener("click", () => { more = !more; store.set("st_more", more ? "1" : "0"); if (last) drawMore(last); });

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
  if (c === "spend" || c === "limits") { showTab(c); return say(`Showing ${c}`); }
  if (["day", "today", "week", "month"].includes(c)) { setPeriod(c === "today" ? "day" : c); showTab("spend"); return say(`Period: ${PERIOD[period][0].toLowerCase()}`); }
  if (c === "refresh") { load(); return say("Refreshing"); }
  if (c === "pause" || c === "resume") { setLive(c === "resume"); return say(c === "pause" ? "Live updates paused" : "Live updates on"); }
  if (c.startsWith("sel ")) {
    const q = c.slice(4).trim(), x = (last && last.sessions || []).find((x) => x.session && (x.session.startsWith(q) || (x.project || "").toLowerCase() === q));
    if (x) { sel = x.session; showTab("limits"); drawSessions(last); return say(`Selected ${x.project || x.session}`); }
  }
  say(`Unknown command: ${c}`, false);
}
const KEYS = { "/": openCmd, "1": () => showTab("spend"), "2": () => showTab("limits"), r: () => { load(); say("Refreshing"); }, p: () => setLive(!live) };
for (const b of document.querySelectorAll("#keys button")) b.addEventListener("click", () => KEYS[b.dataset.key]());
$("cmd-form").addEventListener("submit", (ev) => { ev.preventDefault(); run($("cmd").value); });
$("cmd").addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeCmd(); });
window.addEventListener("keydown", (e) => {
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
  for (const f of [drawMeta, drawSetup, drawHeadline, drawSpend, drawLimits, drawChart, drawSessions, drawMore, drawTrack]) {
    try { f(s); } catch (e) { console.error(f.name, e); }
  }
}

function show(which) {
  $("signin").hidden = which !== "signin";
  $("view").hidden = which !== "view";
  $("signout").hidden = !store.get("st_token");
  $("adder").hidden = which !== "view" || !store.get("st_token");   // a hub, not `savetokens web`
  document.querySelector(".tabs").hidden = $("live").hidden = document.querySelector(".bar").hidden = which !== "view";
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

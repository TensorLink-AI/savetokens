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
  if (LENSES.includes(h.get("lens"))) setLens(h.get("lens"));
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
const share = (part, whole) => (!whole ? "–" : part > 0 && (100 * part) / whole < 1 ? "<1%" : `${Math.round((100 * part) / whole)}%`);

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
// extra words go behind an ⓘ: shown on hover or focus
function info(text, side = "right") {
  const i = el("span", "info pop-host", el("span", "i", "i"), el("span", `pop ${side}`, el("span", "pop-in w320", text)));
  i.tabIndex = 0; i.setAttribute("aria-label", "About this");
  return i;
}
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

function planBadge(plan) {
  const b = badge("subscription"); b.title = `Subscription: ${usd(plan.usd)} a ${plan.period}`; return b;
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

// ── the lens: everything, pay as you go only, or plans in their own units; every tab follows it ──
const LENSES = ["all", "api", "plans"];
let lens = LENSES.includes(store.get("st_lens")) ? store.get("st_lens") : "all";
const DEFAULT_PROVIDER = { "claude-code": "anthropic", codex: "openai" };   // a tool on its own maker's service
const PLAN_TOOL = { anthropic: "claude-code", openai: "codex", "openai-codex": "codex" };   // whose limits a plan sets
const inLens = (billing) => lens === "all" || (lens === "api") === (billing === "api");
function provLabel(s, p) {
  const e = ((s.spend || {}).providers || []).find((e) => e.provider === p);
  return e ? e.label : PROVIDERS[p] || p;
}
// "Claude Code", "Claude Code · saygm.com", "Codex · API", "Hermes · engy.ai": the tool, and who serves it when that isn't its own
function toolName(s, harness, provider, billing) {
  const t = TOOLS[harness] || harness || "";
  if (provider && provider !== DEFAULT_PROVIDER[harness]) return `${t} · ${provLabel(s, provider)}`;
  return billing === "api" ? `${t} · API` : t;
}
function setLens(l) {
  lens = l; store.set("st_lens", l);
  for (const b of document.querySelectorAll("#lens button")) b.setAttribute("aria-pressed", String(b.dataset.lens === l));
  if (last) draw(last);
}
for (const b of document.querySelectorAll("#lens button")) b.addEventListener("click", () => setLens(b.dataset.lens));

// ── spend ──
const MIXC = ["c0", "c1", "c2", "c3", "c4", "c5"];
const minus = (q, f) => (q ? q.map((v) => Math.max(0, v - (f || 0))) : null);   // a forecast without the plans' fees
const tri = (v) => (v != null ? [v, v, v] : null);
// one row's numbers in this lens: all of it, or just what's paid as you go
function inView(y) {
  const pj = y.projected || {}, nx = y.next || {};
  if (lens === "api") return { tokens: y.api_tokens, cost: y.usd || 0, value: y.usd || 0,
    likely: pj.cost ? minus(pj.cost, pj.fixed_usd) : tri(y.projected_usd), next: nx.cost ? minus(nx.cost, nx.fixed_usd) : tri(y.next_usd) };
  return { tokens: y.tokens, cost: y.cost != null ? y.cost : y.usd, value: y.api_value,
    likely: pj.cost || tri(y.projected_usd), next: nx.cost || tri(y.next_usd) };
}
// budgets on real money: each tool's API budget and those you set that count API spend
const apiBudgets = (s) => [...(s.limits || []).filter((o) => o.kind === "api"),
  ...(((s.projects || {}).budgets) || []).filter((o) => o.count === "api")];

function drawSpend(s) {
  const sp = s.spend, kpis = clear($("kpis")), prov = clear($("providers"));
  const seg = clear($("periods"));
  for (const [k, [name]] of Object.entries(PERIOD)) {
    const b = el("button", null, name.toUpperCase());
    b.type = "button"; b.setAttribute("aria-pressed", String(k === period));
    b.addEventListener("click", () => setPeriod(k));
    seg.append(b);
  }
  const plans = lens === "plans";
  kpis.hidden = $("spend-split").hidden = plans;
  $("plancards").hidden = !plans;
  if (!sp || sp.error || !sp.periods) {
    (plans ? clear($("plancards")) : kpis).append(empty(sp && sp.error ? `Couldn't work out spend: ${sp.error}` : "No usage yet."));
    return;
  }
  if (plans) return drawPlans(s);
  const api = lens === "api";
  const [name, nextName, by] = PERIOD[period], x = sp.periods[period];
  const kpi = (label, value, sub, rows) => {
    const t = tile("kpi", label, true);
    t.append(el("div", "big", value), el("div", "sub", sub));
    for (const [k, v] of rows) t._pop.append(kv(k, v));
    kpis.append(t);
  };
  if (x) {
    const v = inView(x), pj = x.projected || {}, nx = x.next || {};
    if (api) {
      kpi(`API · ${name.toUpperCase()}`, usd2(v.cost), `${tok(v.tokens)} tokens`,
        [["paid as you go", usd2(v.cost)], ["tokens", tok(v.tokens)], ["of all spend", share(v.cost, x.cost)]]);
    } else {
      kpi(`${name.toUpperCase()} SO FAR`, usd2(x.cost), `${tok(x.tokens)} tokens`,
        [["pay as you go", usd2(x.usd || 0)], ["subscriptions", usd2(x.fixed_usd || 0)], ["tokens", tok(x.tokens)], ["requests", (x.requests || 0).toLocaleString("en-US")]]);
    }
    kpi("LIKELY BY END", v.likely ? usd(v.likely[1]) : "–", v.likely ? `range ${rng(v.likely, usd)}` : "forecast on its way",
      [[`likely ${by}`, v.likely ? usd(v.likely[1]) : "–"], ["range", v.likely ? rng(v.likely, usd) : "–"],
       ...(api ? [] : [["tokens", pj.tokens ? `${tok(pj.tokens[1])} (${rng(pj.tokens, tok)})` : "–"]])]);
    kpi(nextName.toUpperCase(), v.next ? usd(v.next[1]) : "–", v.next ? `range ${rng(v.next, usd)}` : "forecast on its way",
      [["likely", v.next ? usd(v.next[1]) : "–"], ...(api ? [] : [["of which subscriptions", usd(nx.fixed_usd)],
       ["tokens", nx.tokens ? `${tok(nx.tokens[1])} (${rng(nx.tokens, tok)})` : "–"]])]);
    if (api) {
      const bs = apiBudgets(s).sort((a, b) => (b.used || 0) - (a.used || 0)), topB = bs[0];
      kpi("API BUDGETS", topB ? pct(topB.used) : "none", topB ? `most used: ${topB.label || TOOLS[topB.harness] || ""}` : "savetokens budget --provider NAME --usd 20",
        bs.length ? bs.map((o) => [o.label || `${TOOLS[o.harness] || o.harness} API`, `${pct(o.used)} of ${usd(o.budget_usd || o.amount)}`]) : [["none set", "–"]]);
      $("saved-label").textContent = "Of all spend";
      $("saved").textContent = share(v.cost, x.cost);
      $("saved-info").textContent = "Pay-as-you-go as a share of all spend this period: the rest is plan fees, spread evenly over time.";
    } else {
      const saved = Math.max(0, (x.api_value || 0) - x.cost);
      kpi("API VALUE", usd(x.api_value), saved > 0.005 ? `plans save you ${usd(saved)}` : "what it'd cost pay-as-you-go",
        [["API value of all usage", usd2(x.api_value)], ["what you pay", usd2(x.cost)], ["saved by plans", usd2(saved)]]);
      $("saved-label").textContent = "Plans save you";
      $("saved").textContent = usd(saved);
      $("saved-info").textContent = "What the same usage would cost pay-as-you-go, less what the plans cost for it.";
    }
  }
  $("period-name").textContent = (api ? "API · " : "") + name.toUpperCase();
  // which tools used each provider this week, for the API view's last column
  const usedBy = {};
  for (const m of s.models || []) if (m.billing === "api") (usedBy[m.provider] = usedBy[m.provider] || new Set()).add(TOOLS[m.harness] || m.harness);
  prov.append(el("div", "row head", el("span", null, "PROVIDER"), el("span", "r", "TOKENS"), el("span", "r", api ? "PAID" : "SPEND"),
    el("span", "r", "LIKELY BY END"), el("span", "r", api ? "TOOLS" : "API VALUE")));
  const cells = (y, p) => {
    const v = inView(y);
    return [el("span", "r", tok(v.tokens)), el("span", "r spend", usd2(v.cost)), el("span", "r", v.likely ? usd(v.likely[1]) : "–"),
      api ? el("span", "r dim", p ? [...(usedBy[p] || [])].join(", ") || "–" : "") : el("span", "r dim", v.value ? usd2(v.value) : "–")];
  };
  const list = (sp.providers || []).filter((e) => e.periods[period] && (!api || e.periods[period].api_tokens > 0 || e.periods[period].usd > 0.005));
  for (const e of list) {
    const y = e.periods[period], id = e.label;
    const r = el("div", "row click" + (open[id] ? " sel" : ""),
      el("span", null, el("span", "mark-col", open[id] ? "▼" : "▶"), el("span", "name", e.label),
         api ? null : e.plan ? planBadge(e.plan) : null, api ? null : y.usd > 0.005 || !e.plan ? badge("api") : null),
      ...cells(y, e.provider));
    r.tabIndex = 0;
    const toggle = () => { open[id] = !open[id]; drawSpend(last); };
    r.addEventListener("click", toggle);
    r.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); } });
    prov.append(r);
    if (open[id]) {
      const d = el("div", "detail"), v = inView(y);
      for (const m of (e.models || []).filter((m) => m.periods[period] && (!api || m.periods[period].api_tokens > 0))) {
        const mr = el("div", "row", el("span", null, m.model || "unknown"), ...cells(m.periods[period]));
        if (m.share_tokens != null) mr.title = `${Math.round(m.share_tokens * 100)}% of ${e.label}'s tokens`;
        d.append(mr);
      }
      d.append(el("div", "sub", `likely range ${v.likely ? rng(v.likely, usd) : "–"} · ${nextName.toLowerCase()} `
        + (v.next ? `${usd(v.next[1])} (${rng(v.next, usd)})` : "–")));
      prov.append(d);
    }
  }
  if (!list.length) prov.append(empty(api ? "Nothing paid as you go this period: everything was on plans." : "No usage this period."));
  else if (x) prov.append(el("div", "row total", el("span", null, api ? "All API" : "All providers"), ...cells(x)));
  // what each provider is of the period's spend (or of its API spend)
  const bar = clear($("mixbar")), ml = clear($("mixlist")), tot = x ? inView(x).cost : 0;
  list.forEach((e, i) => {
    const vv = inView(e.periods[period]).cost || 0, share = tot > 0 ? (100 * vv) / tot : 0;
    const segm = el("span", MIXC[i % MIXC.length]); segm.style.flex = String(Math.max(share, 1)); segm.title = `${e.label} ${Math.round(share)}%`;
    bar.append(segm);
    ml.append(el("div", "mixrow", el("span", "sw " + MIXC[i % MIXC.length]), el("span", "n", e.label, e.plan || api ? null : el("span", "dim", " · API")),
      el("span", null, usd2(vv)), el("span", "r dim", `${Math.round(share)}%`)));
  });
  if (!list.length) ml.append(empty(api ? "No API spend yet this period." : "No spend yet this period."));
  $("spend-note").textContent = api
    ? "Paid: what was billed per token, real money. Likely by end: the forecast for pay-as-you-go only, without plan fees. Tools: which tools used that provider this week."
    : "Spend is pay-as-you-go plus subscriptions, spread evenly over time, so two plans at the same price spend the same."
      + " API value is what the same usage would cost at pay-as-you-go prices: on a plan, that's what the plan saves you."
      + (Object.keys(sp.plans || {}).length ? "" : " On a subscription? Add its price: savetokens plan anthropic --usd 200 --per month");
}

// plans, in their own units: how much of each limit is used, and what the plan bought at API prices
function drawPlans(s) {
  const box = clear($("plancards")), sp = s.spend, [name] = PERIOD[period];
  const subs = (sp.providers || []).filter((e) => e.plan);
  if (!subs.length) { box.append(empty("No subscriptions known. Add one: savetokens plan anthropic --usd 200 --per month")); return; }
  for (const e of subs) {
    const y = e.periods[period] || {}, h = PLAN_TOOL[e.provider];
    const lims = (s.limits || []).filter((o) => o.kind === "subscription" && o.harness === h);
    const week = lims.find((o) => o.name === "seven_day"), five = lims.find((o) => o.name === "five_hour");
    const crit = week && (week.stage === "act" || week.stage === "last_call" || week.used >= 100), warn = week && !crit && !!week.eta;
    const t = tile("limit plan" + (crit ? " crit" : warn ? " warn" : ""), null, true);
    t.prepend(el("span", "legend", `┤ ${e.label}${h ? ` · ${TOOLS[h]}` : ""} ├`));
    t.append(el("span", "label", planBadge(e.plan), `${usd(e.plan.usd)} a ${e.plan.period}`));
    if (week) {
      t.append(el("span", "big", el("span", "num", pct(week.used)), el("span", "dim", "of the weekly limit")));
      const m = el("div", "meter");
      if (week.p10 != null && week.p90 != null) {
        const b = el("div", "band"); b.style.left = `${clamp(week.p10)}%`; b.style.width = `${clamp(week.p90) - clamp(week.p10)}%`; m.append(b);
      }
      const f = el("div", "fill"); f.style.width = `${clamp(week.used)}%`; m.append(f);
      if (week.p50 != null) { const tk = el("div", "p50"); tk.style.left = `calc(${clamp(week.p50)}% - 1px)`; m.append(tk); }
      t.append(m, el("span", "main", week.eta ? `Runs out ${when(week.eta, s.now)}, ${span(week.resets - week.eta)} early`
        : week.p50 != null ? `Likely ${pct(week.p50)} · ${Math.round((week.p_hit || 0) * 100)}% risk` : "Forecast on its way"));
    } else {
      t.append(el("span", "big", el("span", "num", "–"), el("span", "dim", "no limit readings yet")));
    }
    const value = Math.max(0, (y.api_value || 0) - (y.usd || 0)), fee = y.fixed_usd || 0;
    t.append(el("span", "ratio", fee > 0.005 ? `${usd(value)} at API prices · ${(value / fee).toFixed(1)}× its fee ${name.toLowerCase()}`
      : `${usd(value)} at API prices ${name.toLowerCase()}`));
    if (five) t._pop.append(kv("5-hour limit now", pct(five.used)));
    if (week) t._pop.append(kv("weekly, likely at reset", pct(week.p50)), kv("chance of running out", `${Math.round((week.p_hit || 0) * 100)}%`),
      kv("resets", `${when(week.resets, s.now)} (in ${span(week.resets - s.now)})`));
    t._pop.append(kv(`its fee, ${name.toLowerCase()}`, usd2(fee)), kv("its usage at API prices", usd2(value)),
      kv("limits hit, 30 days", String((s.hits || []).filter((x) => x.harness === h).length)),
      el("span", "faint sep", "The Limits tab has the 5-hour window, the hourly chart and each session."));
    box.append(t);
  }
}

// ── limits ──
function limitCard(o, now) {
  const api = o.kind === "api", goal = o.kind === "goal", money = api || (goal && o.unit === "usd");
  const crit = o.stage === "act" || o.stage === "last_call" || o.used >= 100, warn = !crit && !!o.eta;
  const tool = goal ? String(o.label || "").replace(/ budget$/, "") : (o.tool || TOOLS[o.harness] || o.harness || "");
  const label = api ? `Budget, ${usd(o.budget_usd)} ${o.per || ""}` : goal ? `${money ? usd(o.amount) : tok(o.amount)} ${o.per || ""}`
    : `${String(o.label || "").replace(/^.* plan's /, "").replace(/^./, (c) => c.toUpperCase())}`;
  const card = tile("limit " + (o.kind || "") + (crit ? " crit" : warn ? " warn" : ""), null, true);
  card.prepend(el("span", "legend", "┤ ", tool, api ? " · API" : goal ? " · budget" : "", " ├"));
  card.append(el("span", "label", badge(o.kind), label));
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
  else if (o.eta) main = `Runs out ${when(o.eta, now)}, ${span(o.resets - o.eta)} early`;
  else if (o.p50 != null) main = `Likely ${pct(o.p50)} · ${Math.round((o.p_hit || 0) * 100)}% risk`;
  else main = "Forecast on its way";
  const dnow = (o.decision || {}).now;
  if (dnow) main = `${dnow.short} · decision model, ${Math.round((dnow.p || 0) * 100)}% sure`;
  card.append(el("span", "main", main));
  if (goal && o.counts) card.append(el("span", "dim small", `Counts ${o.counts}`));
  if (goal && String(o.pool || "").startsWith("goal:")) {
    const rm = el("button", "rm", "REMOVE");
    rm.type = "button";
    rm.setAttribute("aria-label", `Remove the ${o.label} budget`);
    rm.onclick = async () => {
      if (!confirm(`Remove the ${o.label} budget?`)) return;
      try { await post("v1/budget", { off: o.pool.slice(5) }); await load(); }
      catch (e) { alert(`Couldn't remove it (${e.message}).`); }
    };
    card.append(rm);
    card.append(modePicker(o));
    const d = o.decision;
    if (d && d.now) card.append(decideActions(o.pool.slice(5), o.label, o.amount, o.unit, d.now));
    else if (d) card.append(el("span", "dim small", `Decision model's last call (${when(d.at, now)}): ${d.last}`),
      ...(d.history[0] && d.history[0].would ? [el("span", "faint small", `Likely end, by choice: ${d.history[0].would}`)] : []));
    else if (o.mode === "decide") card.append(el("span", "dim small", "Decision model: no call yet. It's asked once this budget is at risk or used up"));
  }
  if (o.stage) card._pop.append(kv("alert", o.stage.replace("_", "-")));
  if (o.decision) decideHistory(card._pop, o.decision, now);
  if (o.eta) card._pop.append(kv("runs out", `${when(o.eta, now)}, ${span(o.resets - o.eta)} before ${api || goal ? "the period ends" : "it resets"}`));
  card._pop.append(kv(`likely at ${end}`, pct(o.p50)), kv("range", o.p10 != null ? `${pct(o.p10)}–${pct(o.p90)}` : "–"),
    kv("chance of running out", o.p_hit != null ? `${Math.round(o.p_hit * 100)}%` : "–"),
    kv(api || goal ? "period ends" : "resets", `${when(o.resets, now)} (in ${span(o.resets - now)})`),
    el("span", "faint sep", "bar: used now, then the likely range at reset; the white tick is the most likely point"));
  return card;
}

function drawLimits(s) {
  const box = clear($("limits"));
  const list = lens === "api" ? apiBudgets(s) : (s.limits || []).filter((o) => o.kind !== "goal" && inLens(o.kind === "api" ? "api" : "plan"));
  if (!list.length) {
    box.append(empty(lens === "api" ? "No API budgets yet. Add one on the Projects tab (BUDGETS), or: savetokens budget --provider NAME --usd 20 --per month"
      : lens === "plans" ? "No plan limits yet: they appear after a message in Claude Code or Codex."
      : "No limits known yet. Send a message in Claude Code or Codex with savetokens installed, or set a budget for a provider you pay as you go: savetokens budget --provider NAME --usd 20 --per month"));
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
  const planned = new Set(Object.keys((s.spend || {}).plans || {}));
  const fits = (c) => lens === "all" || (c.group === "limits" ? (c.unit === "$") === (lens === "api")
    : planned.has(String(c.id).replace(/^tokens:/, "")) === (lens === "plans"));
  const list = ((s.charts && s.charts.length) ? s.charts
    : s.demand ? [{ id: s.demand.pool || "demand", group: "limits", ...s.demand }] : []).filter(fits);
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
  if (!past.some((v) => v > 0) && !next.some((v) => v > 0)) {
    plot.append(empty(list.length ? "No usage yet." : `Nothing to chart in the ${lens.toUpperCase()} view yet.`)); clear($("readout")); clear(axis); return;
  }
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
  const box = clearRows($("sessions")), rows = (s.sessions || []).filter((x) => x.session != null && inLens(x.billing));
  const rests = (s.sessions || []).filter((x) => x.session == null && inLens(x.billing));
  const rest = rests.length ? { project: rests.map((x) => x.project).join(", "), pct_week: rests.some((x) => x.pct_week != null)
    ? rests.reduce((a, x) => a + (x.pct_week || 0), 0) : null, usd: rests.reduce((a, x) => a + (x.usd || 0), 0) } : null;
  const running = rows.filter((x) => x.running).length;
  $("sessions-aside").textContent = rows.length ? `${running} running` : "";
  const more = $("sessions-more");
  if (!rows.length) { box.append(empty(lens === "all" ? "No sessions in the last 24 hours." : `No ${lens === "api" ? "API" : "plan"} sessions in the last 24 hours.`)); more.hidden = true; return; }
  // a session on a plan is measured in % of its limit; one paid as you go without a budget, in dollars
  const amount = (x) => (x.pct_week != null ? pct(x.pct_week, 1) : usd2(x.usd || 0));
  const topPct = Math.max(1, ...rows.map((x) => x.pct_week || 0)), topUsd = Math.max(0.01, ...rows.map((x) => (x.pct_week == null ? x.usd || 0 : 0)));
  const barOf = (x) => (x.pct_week != null ? (100 * x.pct_week) / topPct : (100 * (x.usd || 0)) / topUsd);
  const shown = allSessions || rows.some((x, i) => i >= SESSIONS_SHOWN && x.session === sel) ? rows : rows.slice(0, SESSIONS_SHOWN);
  for (const x of shown) {
    const id = x.session, name = x.project || x.session;
    const tool = toolName(s, x.harness, x.provider, x.billing);
    const fill = el("i"); fill.style.width = `${barOf(x)}%`;
    const r = el("div", "row click" + (sel === id ? " sel" : ""), el("span", "dot" + (x.running ? " on" : "")),
      el("span", "name", name, x.project ? el("span", "sid", id) : null), el("span", "tool", tool),
      el("span", "todaybar", el("span", "track", fill), el("span", "v", amount(x))),
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
    el("span", "todaybar", el("span", "track"), el("span", "v", rest.pct_week != null ? pct(rest.pct_week, 1) : usd2(rest.usd))), el("span")));
  const hidden = rows.length - SESSIONS_SHOWN;
  more.hidden = hidden <= 0 && !rest;
  more.textContent = allSessions ? "− show fewer" : `+ ${hidden > 0 ? `${hidden} more session${hidden === 1 ? "" : "s"}` : "the rest"}`;
}

// ── models, alerts, machines, hits ──
const HIT = (h) => ({ session: "5-hour limit", weekly: "Weekly limit" }[h.kind] || (h.model ? `${h.model} limit` : "A limit"));
function drawLists(s) {
  const models = clear($("models")), by = {};
  for (const m of (s.models || []).filter((m) => inLens(m.billing))) {
    const g = toolName(s, m.harness, m.provider, m.billing);
    (by[g] = by[g] || []).push(m);
  }
  if (!Object.keys(by).length) models.append(empty(lens === "all" ? "No usage this week." : `No ${lens === "api" ? "API" : "plan"} usage this week.`));
  for (const [g, list] of Object.entries(by)) {
    models.append(el("div", "group", g));
    const all = list.reduce((a, m) => a + m.share, 0) || 1;   // each model's part of this group's tokens
    for (const m of list.slice(0, 5)) {
      const part = Math.round((100 * m.share) / all), fill = el("i"); fill.style.width = `${part}%`;
      const row = el("div", "model", el("span", "n", m.model || "unknown"), el("span", "track", fill), el("b", null, `${part}%`));
      if (m.subagents >= 0.05) row.title = `${Math.round(m.subagents * 100)}% via subagents`;
      models.append(row);
    }
  }
  const mach = clear($("machines"));
  if (!(s.machines || []).length) mach.append(empty("No machines have synced this week."));
  for (const m of s.machines || []) {
    const off = m.last_sync && s.now - m.last_sync > QUIET;
    mach.append(el("div", "stack", el("span", "n", String(m.machine || "?").slice(0, 12), off ? el("span", "badge quiet", "QUIET") : null),
      el("span", "s", m.last_sync ? `synced ${ago(m.last_sync, s.now)}` : m.last_seen ? `last seen ${ago(m.last_seen, s.now)}` : "no usage this week",
        off ? info("Not reaching this hub for over a day. Switched off, or pointed at an old address: add it again with ADD MACHINE.") : null),
      el("span", "s", `${(m.requests || 0).toLocaleString("en-US")} requests this week`
        + (m.tokens ? ` · ${share(m.api_tokens || 0, m.tokens)} API` : ""))));
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
  $("hits-tile").hidden = lens === "api";   // pay as you go has no limits to hit
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
  $("projects-days").textContent = `PROJECTS · LAST ${pv.days} DAYS`;
  const all = pv.list.reduce((a, p) => a + (p.tokens || 0), 0) || 1;
  const planOf = (p) => (p.by || []).filter((b) => b.billing === "plan").reduce((a, b) => ({ tokens: a.tokens + b.tokens, value: a.value + b.value_usd }), { tokens: 0, value: 0 });
  const key = { all: (p) => p.value_usd || 0, api: (p) => p.api_usd || 0, plans: (p) => planOf(p).tokens }[lens];
  const shown = pv.list.filter((p) => lens === "all" || key(p) > (lens === "api" ? 0.005 : 0)).sort((a, b) => key(b) - key(a));
  for (const p of shown) {
    const name = p.name || `project ${p.id.slice(0, 8)}`;
    const c = tile("proj", null, true);
    c.prepend(el("span", "legend" + (p.name ? "" : " anon"), `┤ ${name} ├`));
    const days = p.last_days || [], hi = Math.max(1, ...days);
    const spark = el("div", "spark");
    days.forEach((v, i) => { const b = el("span", i === days.length - 1 ? "last" : null); b.style.height = `${Math.max(6, (100 * v) / hi)}%`; spark.append(b); });
    spark.title = `tokens a day, the last ${days.length} days`;
    const pl = planOf(p);
    const [big, sub] = lens === "api" ? [usd2(p.api_usd), "paid as you go"]
      : lens === "plans" ? [tok(pl.tokens), "tokens on plans"]
      : [usd2(p.value_usd), p.api_usd > 0.005 ? `at API prices · ${usd(p.api_usd)} paid` : "at API prices"];
    c.append(el("div", "top-row", el("span", null, el("div", "big", big), el("div", "dim small", sub)), days.length ? spark : null));
    c.append(mix(p));
    const n = p.sessions;
    c.append(el("span", "kv", el("span", null, `${tok(p.tokens)} tokens` + (pv.more ? "" : ` · ${Math.round((100 * p.tokens) / all)}%`)),
      el("span", null, n == null ? "" : `${n} session${n === 1 ? "" : "s"}`)));
    const pop = c._pop;
    pop.append(el("span", "head", "WHERE ITS TOKENS WENT"));
    for (const b of p.by) pop.append(el("span", "kv", el("span", null, badge(BILL[b.billing]), `${TOOLS[b.tool] || b.tool} · ${PROVIDERS[b.provider] || b.provider}`),
      el("span", null, `${Math.round((100 * b.tokens) / (p.tokens || 1))}%`)));
    pop.append(kv("at API prices, all of it", usd2(p.value_usd)), kv("paid as API", p.api_usd > 0.005 ? usd2(p.api_usd) : "–"),
      kv("on plans, at API prices", usd2(pl.value)), kv("requests", (p.requests || 0).toLocaleString("en-US")));
    for (const o of pv.budgets.filter((o) => ((o.scope || {}).projects || []).includes(p.id)))
      pop.append(el("span", "kv", el("span", null, badge("goal"), o.label), el("span", null, `${pct(o.used)} used`)));
    if (p.tasks.length) {
      pop.append(el("span", "head", "BIGGEST TASKS"));
      for (const t of p.tasks.slice(0, 4)) pop.append(kv((t.name || `task ${t.id.slice(0, 8)}`) + (t.done ? " ✓" : ""), `${tok(t.tokens)} · ${usd(t.value_usd)}`));
      if (p.tasks.length > 4 || p.more_tasks) pop.append(el("span", "faint", `More: savetokens tasks --project ${p.name || "NAME"}`));
    }
    grid.append(c);
  }
  if (!shown.length) grid.append(empty(lens === "api" ? "No project has paid-as-you-go usage in this window."
    : lens === "plans" ? "No project has plan usage in this window." : "No usage with a project yet: it's counted from the folder each session runs in."));
  const note = clear($("projects-note"));
  if (pv.unnamed) note.append("ids, not names ", info("Names stay on your machines, so this hub shows ids. To see names here, run on each machine: savetokens projects --share-names on"));
  if (pv.more) note.append(el("span", "dim", ` · ${pv.more} more `), info("Smaller projects: savetokens projects lists them all."));
  const bs = pv.budgets.filter((o) => lens === "all" || (o.count === "api") === (lens === "api"));
  for (const o of bs) cards.append(limitCard(o, s.now));
  if (!bs.length) cards.append(empty("No budgets yet. Add one below, or from a terminal: savetokens budget --project webapp --usd 150 --per month"));
  budgetChoices(s);
}

// the decide mode: the call that needs you now (on every tab), what you can do about it, and the history
function decideActions(id, label, amount, unit, d) {
  const row = el("span", "decide-acts");
  const act = (text, title, fn) => { const b = el("button", "chip", text); b.type = "button"; b.title = title;
    b.onclick = async () => { b.disabled = true; try { await fn(); await load(); } catch (e) { alert(`Couldn't do that (${e.message}).`); b.disabled = false; } };
    row.append(b); };
  if (unit !== "tokens") act("RAISE BUDGET", `Set a new amount for the ${label} budget`, async () => {
    const v = prompt(`New amount for the ${label} budget, in $ (now ${usd(amount)}):`, String(Math.round(amount * 1.5)));
    if (v == null) return;
    await post("v1/budget", { id, usd: Number(v) });
  });
  if (d.pause) act("PAUSE PROJECT", "Set it to guard: new work on it stops once the budget is used up", async () => {
    if (confirm(`Pause the ${label} project? Its budget goes to guard.`)) await post("v1/budget", { id, mode: "guard" });
  });
  if (d.blocks) act("LET WORK CONTINUE", "Switch it to advise: work goes on, and the agent is told to pace itself", async () => {
    if (confirm(`Let work on the ${label} budget continue? It switches to advise: the decision model stops deciding for it.`)) await post("v1/budget", { id, mode: "advise" });
  });
  return row;
}
function drawDecide(s) {
  const calls = s.decide_now || [];
  $("decide").hidden = !calls.length;
  const box = clear($("decide-list"));
  for (const d of calls) {
    const until = d.blocks && d.do === "wait_for_reset" && d.resets ? ` until it resets ${when(d.resets, s.now)}` : "";
    const next = d.next ? ` Next most likely: ${d.next.short} (${Math.round(d.next.p * 100)}%).` : "";
    box.append(el("p", null, `${d.label}: ${d.short}${until}. ${d.text}${next}`),
      ...(d.would ? [el("p", "faint small", `Likely end of the period, by choice: ${d.would}`)] : []), decideActions(d.id, d.label, d.amount, d.unit, d));
  }
}
function decideHistory(pop, d, now) {
  pop.append(kv("decision model", `${d.calls} call${d.calls === 1 ? "" : "s"}, $${(d.cost_usd || 0).toFixed(4)}`));
  if (d.periods_ended) pop.append(kv("track record", `ended within budget in ${d.periods_within} of ${d.periods_ended} periods`));
  for (const h of d.history || []) {
    const sum = el("summary", null, `${when(h.at, now)} · ${h.short} (${Math.round((h.p || 0) * 100)}%)${h.machine ? ` · ${h.machine}` : ""}`
      + (h.ended_pct != null ? ` · ended at ${Math.round(h.ended_pct)}%` : ""));
    pop.append(el("details", "decide-call small", sum, el("span", "dim", h.text),
      h.next ? el("span", "faint", `Next most likely: ${h.next.short} (${Math.round(h.next.p * 100)}%)${h.model_version ? ` · ${h.model_version}` : ""}`) : null,
      el("span", "faint", h.would ? `Likely end, by choice: ${h.would}` : `Offered: ${h.choices.join(", ").replaceAll("_", " ")}`),
      el("pre", "faint", JSON.stringify(h.sent, null, 1))));
  }
}

// the budget agent: what it may do for each budget
const MODES = {
  off: "Off: no alerts",
  watch: "Watch: alerts you as it runs out",
  advise: "Advise: also tells the agent at work to pace itself; you approve bigger changes",
  guard: "Guard: also stops new work once it's used up (Claude Code prompts blocked; savetokens check exits 4; Codex and Hermes told through MCP)",
  decide: "Decide: a decision model (decisionmodels.io) picks what to do as it runs out: carry on, finish then stop, a cheaper model, another plan, wait for the reset, or stop now. savetokens acts on it; guard's rule if it can't be reached. Needs a key on the machine: savetokens decide --key",
};
function modeSelect(value) {
  const sel = el("select", "mode");
  for (const [k, t] of Object.entries(MODES)) { const o = el("option", null, k.toUpperCase()); o.value = k; o.title = t; sel.append(o); }
  sel.value = value || "watch";
  sel.title = MODES[sel.value];
  return sel;
}
function modePicker(o) {
  const sel = modeSelect(o.mode);
  sel.setAttribute("aria-label", `Budget agent for the ${o.label}`);
  sel.onchange = async () => {
    sel.disabled = true;
    try { await post("v1/budget", { id: o.pool.slice(5), mode: sel.value }); await load(); }
    catch (e) { alert(`Couldn't change it (${e.message}).`); sel.value = o.mode || "watch"; sel.disabled = false; }
  };
  return el("span", "agent small", el("span", "dim", "AGENT "), sel, info(MODES[o.mode || "watch"]));
}

// what a new budget can cover: everything paid as you go, a provider, or a project
function budgetChoices(s) {
  const sel = $("b-for"), keep = sel.value;
  const opts = [["overall", "All API spend (every provider)"]];
  for (const e of ((s.spend || {}).providers || [])) if (e.provider) opts.push([`provider:${e.provider}`, `Provider: ${e.label || PROVIDERS[e.provider] || e.provider}`]);
  for (const p of (((s.projects || {}).list) || [])) opts.push([`project:${p.id}`, `Project: ${p.name || `project ${p.id.slice(0, 8)}`}`]);
  const sig = opts.map((o) => o[0]).join("|");
  if (sel.dataset.sig === sig) return;   // unchanged: keep the list (and what's picked) as it is
  sel.dataset.sig = sig;
  clear(sel);
  for (const [v, t] of opts) { const o = el("option", null, t); o.value = v; sel.append(o); }
  if (opts.some((o) => o[0] === keep)) sel.value = keep;
}
$("budget-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("b-err").textContent = "";
  const [what, ...rest] = $("b-for").value.split(":"), id = rest.join(":");
  const body = { usd: Number($("b-usd").value), per: $("b-per").value, mode: $("b-mode").value };
  if (what === "provider") body.provider = id;
  if (what === "project") body.project = id;
  try { await post("v1/budget", body); $("b-usd").value = ""; await load(); }
  catch (e) { $("b-err").textContent = `Couldn't set it (${e.message}).`; }
});

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
  if (LENSES.includes(c)) { setLens(c); return say(`Counting ${c === "all" ? "everything" : c === "api" ? "pay as you go only" : "plans only"}`); }
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
  for (const f of [drawMeta, drawSetup, drawDecide, drawHeadline, drawSpend, drawLimits, drawProjects, drawChart, drawSessions, drawLists, drawTrack]) {
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
setLens(lens);
readFragment().then(() => showTab(tab)).catch((e) => { $("signin-err").textContent = e.message; }).finally(load);

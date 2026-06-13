// ---- tiny API helper ----
// ---- global request tracking (sync / latency indicator) ----
let _inflight = 0;
function setSyncing(show, slow) {
  const el = document.getElementById("syncIndicator");
  if (!el) return;
  el.classList.toggle("show", show);
  el.classList.toggle("slow", !!slow);
  el.querySelector(".txt").textContent = slow ? "Network slow — tap to retry" : "Syncing with Broker…";
}
async function trackedFetch(url, opts) {
  _inflight++;
  const t500 = setTimeout(() => setSyncing(true, false), 500);   // >500ms latency
  const t5000 = setTimeout(() => setSyncing(true, true), 5000);  // >5s = slow
  try { return await fetch(url, opts); }
  finally {
    clearTimeout(t500); clearTimeout(t5000);
    if (--_inflight === 0) setSyncing(false, false);
  }
}
function checkAuth(r) {
  if (r.status === 401) { location.href = "/login"; throw new Error("Login required"); }
  if (r.status === 402) { showExpired(); throw new Error("Plan expired"); }
  return r;
}
function showExpired() {
  if (document.getElementById("expiredScreen")) return;
  const d = document.createElement("div");
  d.id = "expiredScreen";
  d.style.cssText = "position:fixed;inset:0;z-index:10000;background:rgba(13,17,23,.97);display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;";
  d.innerHTML = `<div style="max-width:440px;">
    <div style="font-size:48px;">⏳</div>
    <h2 style="margin:8px 0;">Your plan has expired</h2>
    <p class="muted">Renew your subscription to resume trading. Contact your administrator or visit the plans page.</p>
    <button class="btn" onclick="fetch('/api/auth/logout',{method:'POST'}).then(()=>location.href='/login')">Log out</button>
  </div>`;
  document.body.appendChild(d);
}

// ---- toast notifications (small boxes, top-right, like a broker site) ----
function toast(message, type = "info", ms = 3500) {
  let host = document.getElementById("toastHost");
  if (!host) { host = document.createElement("div"); host.id = "toastHost"; host.className = "toast-host"; document.body.appendChild(host); }
  const el = document.createElement("div");
  el.className = "toast toast-" + type;
  el.textContent = message;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  const kill = () => { el.classList.remove("show"); setTimeout(() => el.remove(), 250); };
  el.onclick = kill;
  setTimeout(kill, ms);
}

async function _readErr(r) { try { return (await r.json()).detail || "Request failed"; } catch (e) { return "Request failed"; } }
const api = {
  async get(url) { const r = checkAuth(await trackedFetch(url)); return r.json(); },
  async post(url, body) {
    const r = checkAuth(await trackedFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null,
    }));
    if (!r.ok) { const d = await _readErr(r); toast("⚠️ " + d, "neg"); throw new Error(d); }
    return r.json();
  },
  async del(url) {
    const r = checkAuth(await trackedFetch(url, { method: "DELETE" }));
    if (!r.ok) { const d = await _readErr(r); toast("⚠️ " + d, "neg"); throw new Error(d); }
    return r.json();
  },
};

document.getElementById("syncIndicator").onclick = () => { setSyncing(false, false); refreshAll(); };

let _lastBalance;
const money = (n) => (n >= 0 ? "₹" : "-₹") + Math.abs(n).toLocaleString("en-IN");
const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");

// ---- tabs ----
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("tab-" + t.dataset.tab).classList.add("active");
    document.body.setAttribute("data-tab", t.dataset.tab);   // drives P&L visibility on mobile
    window.scrollTo({ top: 0 });
    pushWatch();   // (un)subscribe the live tick stream for the new tab
  };
});

// ---- summary / P&L ----
let pnlFilter = "ALL";
function istToday() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata",
    year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
}
let dateFilter = istToday();     // "" = all days; default to today
let _pnlTO = null;
function showPnlLoading(on) {
  const sec = document.querySelector(".pnl-section");
  let ov = document.getElementById("pnlOverlay");
  if (on) {
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "pnlOverlay"; ov.className = "glass-overlay";
      ov.innerHTML = '<span class="spinner"></span> Aggregating…';
      sec.appendChild(ov);
    }
    clearTimeout(_pnlTO);
    _pnlTO = setTimeout(() => showPnlLoading(false), 5000);   // timeout fallback
  } else if (ov) { ov.remove(); clearTimeout(_pnlTO); }
}
document.getElementById("pnlFilter").onchange = (e) => {
  pnlFilter = e.target.value;
  showPnlLoading(true);
  refreshSummary().finally(() => showPnlLoading(false));
};
// date picker — show a particular day's trades & P&L
const dateFilterEl = document.getElementById("dateFilter");
dateFilterEl.value = dateFilter;
dateFilterEl.onchange = (e) => { dateFilter = e.target.value; refreshAll(); };
document.getElementById("dateAll").onclick = () => { dateFilter = ""; dateFilterEl.value = ""; refreshAll(); };

function setCard(id, val) {
  const el = document.getElementById(id);
  if (el) { el.textContent = money(val); el.className = "card-value " + cls(val); }
}

async function refreshSummary() {
  const s = await api.get("/api/summary?broker=" + pnlFilter + "&date=" + encodeURIComponent(dateFilter));
  const p = s.pnl || {};
  setCard("sumBooked", p.booked || 0);
  setCard("sumActive", p.active || 0);
  setCard("sumTotal", p.total || 0);
  const scope = document.getElementById("dateScope");
  if (scope) scope.textContent = dateFilter ? `· ${dateFilter}` : "· all days";
  // per-account breakdown (only in the All view)
  const bd = document.getElementById("pnlBreakdown");
  const list = s.pnl_breakdown || [];
  bd.innerHTML = (s.pnl_filter === "ALL")
    ? list.map((x) => `${x.label}: <span class="${cls(x.net)}">${money(x.net)}</span>`).join(" &nbsp;·&nbsp; ")
    : "";
  // daily limit halt banner
  const halt = document.getElementById("haltBanner");
  if (halt) {
    if (s.daily_halt) {
      halt.style.display = "block";
      halt.innerHTML = `⛔ Trading halted for today — ${s.daily_halt_reason || "daily limit hit"}. `
        + `<span class="link" id="resetHaltLink">Resume trading</span>`;
      const rl = document.getElementById("resetHaltLink");
      if (rl) rl.onclick = async () => {
        if (confirm("Clear the daily limit halt and allow new trades again?")) {
          await api.post("/api/settings/reset_halt", {}); toast("✅ Trading resumed", "pos"); await refreshAll();
        }
      };
    } else halt.style.display = "none";
  }
  // strict broker lock: disable provider switching while any trade is active/pending
  const locked = !!s.active_locked;
  ["dataProvider", "tradeProvider"].forEach((id) => {
    const e = document.getElementById(id);
    if (e) { e.disabled = locked; e.title = locked ? "Locked while trades are active or pending" : ""; }
  });
  const ln = document.getElementById("brokerLockNote");
  if (ln) ln.style.display = locked ? "block" : "none";
  // kill switch state
  const on = s.kill_switch === "on";
  const ks = document.getElementById("killState");
  ks.textContent = "KILL: " + (on ? "ON" : "OFF");
  ks.className = "pill " + (on ? "pill-on" : "pill-off");
  document.getElementById("killBtn").classList.toggle("on", on);

  // price-feed banner
  const banner = document.getElementById("mdBanner");
  const inst = s.instruments || {};
  const m = s.md_status || "";
  const dataName = s.data_name || "broker";
  if (m.indexOf("ok") === 0) {
    banner.style.display = "block"; banner.className = "banner ok";
    banner.textContent =
        m === "ok:demo" ? "🧪 DEMO mode — simulated prices (no real money)."
      : m === "ok:ws" ? `⚡ Real-time prices (WebSocket) flowing from ${dataName}.`
      : m === "ok:angel" ? `✅ Live prices flowing from ${dataName}.`
      : m === "ok:zerodha" ? `✅ Live prices flowing from ${dataName}.`
      : m === "ok:alice" ? `✅ Live prices flowing from ${dataName}.`
      : `✅ Live prices (1-second) flowing from ${dataName}.`;
  } else if (m) {
    banner.style.display = "block"; banner.className = "banner";
    banner.textContent = "⚠️ " + m;
  } else {
    banner.style.display = "none";
  }
  // broker name + balance (auto-updates every 2s with the summary)
  const ds = document.getElementById("dataSource");
  if (ds) ds.innerHTML = s.data_name ? `📡 Data: <b>${s.data_name}</b>` : "";
  const bs = document.getElementById("brokerStatus");
  if (bs) {
    const bal = s.balance != null ? " · Avail ₹" + Number(s.balance).toLocaleString("en-IN") : "";
    bs.innerHTML = s.broker_name ? `🧾 Trade: <b>${s.broker_name}</b>${bal}` : "";
    // micro-interaction: pulse when the live balance changes
    if (_lastBalance !== undefined && _lastBalance !== s.balance) {
      bs.classList.remove("pulse"); void bs.offsetWidth; bs.classList.add("pulse");
    }
    _lastBalance = s.balance;
  }
  // connection-lost alert while trades are running
  const ba = document.getElementById("brokerAlert");
  if (ba) {
    if (s.broker_alert) { ba.style.display = "block"; ba.textContent = "⚠️ " + s.alert_msg; }
    else ba.style.display = "none";
  }

  // broker of the active data / trade providers (from the accounts list)
  const accBroker = (v) => { const a = _accounts.find((x) => String(x.id) === String(v)); return a ? a.broker : "DEMO"; };
  const dataBroker = accBroker(s.data_provider), tradeBroker = accBroker(s.trade_provider);
  window._angelSyncing = !!(s.angel_map && s.angel_map.loading) &&
    (dataBroker === "ANGEL" || tradeBroker === "ANGEL");

  // demo direction state
  const dir = s.demo_direction || 0;
  const dirState = document.getElementById("demoDirState");
  if (dirState) dirState.textContent = dir > 0 ? "drifting UP ▲" : dir < 0 ? "drifting DOWN ▼" : "flat (random)";
  document.querySelectorAll("[data-dir]").forEach((b) => {
    const on = (b.dataset.dir === "UP" && dir > 0) || (b.dataset.dir === "DOWN" && dir < 0) || (b.dataset.dir === "FLAT" && dir === 0);
    b.classList.toggle("active", on);
  });

  // symbol list state (Broker tab) — reflect the active data provider
  const ss = document.getElementById("symbolsState");
  const ssLabel = document.getElementById("symbolsLabel");
  // Maps for non-Dhan brokers carry a {count, loading}; Dhan uses the instruments store.
  const brokerMap = { ANGEL: s.angel_map, ZERODHA: s.zerodha_map, ALICE: s.alice_map, DELTA: s.delta_map };
  const map = brokerMap[dataBroker];
  if (ssLabel) {
    const src = { DEMO: "Demo", DHAN: "Dhan", ANGEL: "Angel One",
                  ZERODHA: "Zerodha", ALICE: "Alice Blue", DELTA: "Delta Exchange" }[dataBroker] || "Dhan";
    ssLabel.textContent = `Symbol list (auto-downloaded from ${src}, refreshes daily):`;
  }
  if (ss) {
    if (map) {   // Angel / Zerodha / Alice translation master
      if (map.loading) { ss.textContent = "Downloading…"; ss.className = "pill pill-off"; }
      else if (map.count > 0) { ss.textContent = map.count.toLocaleString("en-IN") + " instruments"; ss.className = "pill pill-ok"; }
      else { ss.textContent = "Not loaded"; ss.className = "pill pill-off"; }
    } else {     // Dhan (the universal picker base) or Demo
      if (inst.loading) { ss.textContent = "Downloading…"; ss.className = "pill pill-off"; }
      else if (inst.underlyings > 0) { ss.textContent = inst.underlyings.toLocaleString("en-IN") + " underlyings loaded"; ss.className = "pill pill-ok"; }
      else { ss.textContent = "Not loaded"; ss.className = "pill pill-off"; }
    }
  }
}

// ---- trades table (with quick status filter) ----
let tradesById = {};
let orderFilter = "ALL";
function showTradesSkeleton(n = 4) {
  document.getElementById("tradesBody").innerHTML = Array.from({ length: n })
    .map(() => `<tr class="skel-row"><td colspan="13"><div class="skel-bar"></div></td></tr>`).join("");
  const c = document.getElementById("orderCards");
  if (c) c.innerHTML = Array.from({ length: n }).map(() => `<div class="order-card skel"><div class="skel-bar" style="width:55%"></div><div class="skel-bar" style="width:35%;margin-top:12px"></div></div>`).join("");
}
const _entryCond = (t) => {
  if (t.entry_type === "SCHEDULED" && t.scheduled_time) return "Time " + t.scheduled_time;
  if (t.entry_type === "TRIGGER" && t.trigger_price)
    return "Trig " + (t.trigger_dir === "ABOVE" ? "≥" : t.trigger_dir === "BELOW" ? "≤" : "@") + " " + t.trigger_price;
  if (t.entry_type === "LIMIT") return "Limit ₹" + t.entry_price;
  return "Market";
};
function tradeCard(t) {
  const isPending = t.status === "PENDING";
  const isDone = ["CLOSED", "CANCELLED", "REJECTED"].includes(t.status);
  const exited = t.exited_qty || 0;
  const qtyTxt = (exited > 0 && t.status === "OPEN") ? `${t.quantity - exited}/${t.quantity}` : t.quantity;
  const avg = t.entry_fill_price || t.entry_price || 0;
  const ltp = t.last_price || 0;
  const arrow = t.pnl > 0 ? "▲" : t.pnl < 0 ? "▼" : "";
  const statusBadge = (t.status === "CLOSED" && t.exit_reason)
    ? `<span class="oc-status st-CLOSED">DONE · ${t.exit_reason}</span>`
    : `<span class="oc-status st-${t.status}">${t.status}</span>`;
  const meta = isPending
    ? `<span>Qty&nbsp;<b>${t.quantity}</b></span><span>Entry&nbsp;<b>${_entryCond(t)}</b></span>`
    : `<span>Qty&nbsp;<b>${qtyTxt}</b></span><span>Avg&nbsp;<b>${avg ? "₹" + avg : "—"}</b></span><span>LTP&nbsp;<b>${ltp ? "₹" + ltp : "—"}</b></span>`;
  const sub = [];
  if (!isPending && (t.stop_loss > 0 || t.sl_points > 0)) sub.push(`SL ${t.stop_loss || t.sl_points + "p"}`);
  const tg = targetCell(t); if (!isPending && tg && tg !== "-") sub.push(`Tgt ${tg}`);
  sub.push(brokerLabel(t));
  const pnlBlock = (isPending || (!avg && !isDone)) ? "" : `<div class="oc-pnl ${cls(t.pnl)}">${money(t.pnl)} <span class="oc-arr">${arrow}</span></div>`;
  return `<div class="order-card oc-${t.status}" data-id="${t.id}">
    <div class="oc-top"><div class="oc-sym"><span class="oc-tag tag-${t.side}">${t.side === "BUY" ? "B" : "S"}</span><span class="oc-name">${esc(t.symbol)}</span><span class="mode-chip m-${t.mode}">${t.mode}</span></div>${statusBadge}</div>
    <div class="oc-mid"><div class="oc-meta">${meta}</div>${pnlBlock}</div>
    <div class="oc-sub">${sub.join(`<span class="dot">·</span>`)}</div>
    <div class="oc-actions">${actionsFor(t, true)}</div></div>`;
}

async function refreshTrades() {
  const rows = await api.get("/api/trades?date=" + encodeURIComponent(dateFilter));
  tradesById = {};
  rows.forEach((t) => (tradesById[t.id] = t));
  const counts = { ALL: rows.length, OPEN: 0, PENDING: 0, CLOSED: 0 };
  rows.forEach((t) => { if (t.status === "OPEN") counts.OPEN++; else if (t.status === "PENDING") counts.PENDING++; else counts.CLOSED++; });
  document.querySelectorAll("[data-cn]").forEach((el) => { const n = counts[el.dataset.cn] || 0; el.textContent = n ? n : ""; });
  const inFilter = (t) => orderFilter === "ALL" ? true
    : orderFilter === "CLOSED" ? ["CLOSED", "CANCELLED", "REJECTED"].includes(t.status)
      : t.status === orderFilter;
  const filtered = rows.filter(inFilter);
  // desktop table
  const body = document.getElementById("tradesBody");
  body.innerHTML = "";
  for (const t of filtered) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.id}</td><td>${t.symbol}</td>
      <td><span class="badge b-${t.mode}">${t.mode}</span></td>
      <td>${brokerLabel(t)}</td><td>${t.side}</td><td>${qtyCell(t)}</td>
      <td>${entryCell(t)}</td>
      <td>${t.stop_loss || (t.sl_points ? t.sl_points + "p" : "-")}</td>
      <td>${targetCell(t)}</td><td>${t.last_price || "-"}</td>
      <td class="${cls(t.pnl)}">${money(t.pnl)}</td>
      <td>${statusCell(t)}</td><td>${actionsFor(t)}</td>`;
    body.appendChild(tr);
  }
  // mobile cards
  const cards = document.getElementById("orderCards");
  cards.innerHTML = filtered.length ? filtered.map(tradeCard).join("")
    : `<div class="order-empty">No ${orderFilter === "ALL" ? "" : orderFilter.toLowerCase() + " "}orders${dateFilter ? " for " + dateFilter : ""}.<br><span class="link" id="emptyNew">+ Place a new trade</span></div>`;
  const en = document.getElementById("emptyNew"); if (en) en.onclick = () => document.querySelector('.tab[data-tab="new"]').click();
  document.querySelectorAll("#tradesBody [data-act], #orderCards [data-act]").forEach((b) => {
    b.onclick = () => { if (b.dataset.act === "modify") openModify(b.dataset.id); else doAction(b.dataset.act, b.dataset.id); };
  });
}
function brokerLabel(t) {
  const name = t.broker === "PAPER" ? "Paper" : (t.broker || (t.mode === "TEST" ? "Paper" : "—"));
  return name + (t.source === "EXTERNAL" ? ' <span class="rbadge r-MANUAL">EXT</span>' : "");
}
function entryCell(t) {
  if (t.status === "PENDING") {
    if (t.entry_type === "SCHEDULED" && t.scheduled_time) return `<span title="scheduled market order">⏱ ${t.scheduled_time}</span>`;
    if (t.entry_type === "TRIGGER" && t.trigger_price)
      return `<span title="algo trigger">🎯 ${t.trigger_dir === "ABOVE" ? "≥" : t.trigger_dir === "BELOW" ? "≤" : "@"} ${t.trigger_price}</span>`;
  }
  return t.entry_fill_price || t.entry_price || "-";
}
function qtyCell(t) {
  const exited = t.exited_qty || 0;
  if (exited > 0 && t.status === "OPEN") return `<span title="remaining / total">${t.quantity - exited} / ${t.quantity}</span>`;
  return t.quantity;
}
function statusCell(t) {
  if (t.status === "CLOSED" && t.exit_reason)
    return `<span class="badge b-CLOSED">CLOSED</span> <span class="rbadge r-${t.exit_reason}">${t.exit_reason}</span>`;
  return `<span class="badge b-${t.status}">${t.status}</span>`;
}
function targetCell(t) {
  if (t.targets_json && t.targets_json !== "[]") {
    try { const ts = JSON.parse(t.targets_json); return `multi ${ts.filter((x) => x.hit).length}/${ts.length}`; } catch (e) {}
  }
  return t.target || (t.target_points ? t.target_points + "p" : "-");
}
function actionsFor(t, card) {
  const c = card ? " oc-btn" : "";
  let h = "";
  if (t.status === "PENDING")
    h += `<button class="btn btn-sm${c}" data-act="cancel" data-id="${t.id}">Cancel</button> `;
  if (t.status === "OPEN")
    h += `<button class="btn btn-sm${c}" data-act="modify" data-id="${t.id}">${card ? "Modify" : "SL/TP"}</button> `
       + `<button class="btn btn-sm${c}${card ? " oc-btn-exit" : ""}" data-act="close" data-id="${t.id}">${card ? "Exit" : "Close"}</button> `;
  if (t.status === "CLOSED" || t.status === "CANCELLED" || t.status === "REJECTED")
    h += `<button class="btn btn-sm${c}" data-act="del" data-id="${t.id}">Delete</button>`;
  return h;
}
// status filter chips
document.querySelectorAll("[data-of]").forEach((c) => c.onclick = () => {
  orderFilter = c.dataset.of;
  document.querySelectorAll("[data-of]").forEach((x) => x.classList.toggle("active", x === c));
  refreshTrades();
});

async function doAction(act, id, sym) {
  try {
    if (act === "cancel") { await api.post(`/api/trades/${id}/cancel`); toast(`Trade #${id} cancelled`, "info"); }
    else if (act === "close") { await api.post(`/api/trades/${id}/close`); toast(`Trade #${id} closed`, "pos"); }
    else if (act === "del") { await api.del(`/api/trades/${id}`); toast(`Trade #${id} deleted`, "info"); }
    await refreshAll();
  } catch (e) { /* error toast already shown by the API helper */ }
}

// ---- logs (day-wise + paginated) ----
let logState = { date: "", level: "", page: 1, pages: 1, inited: false };
let lastDaysKey = "";

async function refreshLogs() {
  const p = new URLSearchParams({ date: logState.date, level: logState.level, page: logState.page, per_page: 25 });
  const d = await api.get("/api/logs?" + p.toString());
  logState.pages = d.pages;
  buildDateOptions(d.days);
  // default to the most recent day on first load
  if (!logState.inited && d.days.length) {
    logState.inited = true; logState.date = d.days[0]; logState.page = 1;
    document.getElementById("logDate").value = logState.date;
    return refreshLogs();
  }
  const body = document.getElementById("logsBody");
  body.innerHTML = "";
  for (const r of d.logs) {
    const tr = document.createElement("tr");
    const time = new Date(r.time + "Z").toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" });
    const reason = r.message.match(/EXIT \((\w+)\)/);
    const msg = reason ? `<span class="rbadge r-${reason[1]}">${reason[1]}</span> ${r.message}` : r.message;
    tr.innerHTML = `<td>${time}</td><td><span class="lvl lvl-${r.level}">${r.level}</span></td>
      <td>${r.trade_id || "-"}</td><td>${msg}</td>`;
    body.appendChild(tr);
  }
  document.getElementById("logPageInfo").textContent =
    `${logState.date || "All days"} · page ${d.page}/${d.pages} · ${d.total} entries`;
  document.getElementById("logPrev").disabled = d.page <= 1;
  document.getElementById("logNext").disabled = d.page >= d.pages;
}

function buildDateOptions(days) {
  const key = days.join(",");
  if (key === lastDaysKey) return;
  lastDaysKey = key;
  const sel = document.getElementById("logDate");
  sel.innerHTML = `<option value="">All days</option>` + days.map((d) => `<option value="${d}">${d}</option>`).join("");
  sel.value = logState.date;
}

document.getElementById("logDate").onchange = (e) => { logState.date = e.target.value; logState.page = 1; refreshLogs(); };
document.getElementById("logLevel").onchange = (e) => { logState.level = e.target.value; logState.page = 1; refreshLogs(); };
document.getElementById("logPrev").onclick = () => { if (logState.page > 1) { logState.page--; refreshLogs(); } };
document.getElementById("logNext").onclick = () => { if (logState.page < logState.pages) { logState.page++; refreshLogs(); } };

// ---- broker-style instrument picker ----
const form = document.getElementById("tradeForm");
const ulSearch = document.getElementById("ulSearch");
const ulResults = document.getElementById("ulResults");
const expiryWrap = document.getElementById("expiryWrap");
const expirySelect = document.getElementById("expirySelect");
const chainWrap = document.getElementById("chainWrap");
const chainBody = document.getElementById("chainBody");
const futWrap = document.getElementById("futWrap");
const pickerHint = document.getElementById("pickerHint");

let currentSeg = "OPTION";
let currentUnderlying = null;
let ulTimer = null, ltpTimer = null;
let chainData = [], chainScrolled = false;
let currentLotSize = 1;

// ---- lots -> quantity ----
const lotsInput = document.getElementById("lotsInput");
function updateQty() {
  const lots = parseInt(lotsInput.value) || 1;
  const qty = lots * currentLotSize;
  form.quantity.value = qty;
  document.getElementById("qtyComputed").textContent = `= ${qty} qty (lot size ${currentLotSize})`;
  const mt = document.getElementById("multiToggle");
  if (mt && mt.checked) trimAndDistribute();
}
lotsInput.addEventListener("input", updateQty);

// ---- order form controls (buttons / stepper / multi-targets) ----
document.getElementById("lotMinus").onclick = () => { lotsInput.value = Math.max(1, (parseInt(lotsInput.value) || 1) - 1); updateQty(); };
document.getElementById("lotPlus").onclick = () => { lotsInput.value = (parseInt(lotsInput.value) || 1) + 1; updateQty(); };

document.querySelectorAll("[data-side]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("[data-side]").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); form.side.value = b.dataset.side;
  };
});

// Mode buttons (TEST / LIVE)
document.querySelectorAll("[data-mode]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("[data-mode]").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); form.mode.value = b.dataset.mode;
  };
});
function setMode(m) {
  document.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("active", x.dataset.mode === m));
  form.mode.value = m;
}

const entryPrice = document.getElementById("entryPrice");
function applyEntryType(et) {
  form.entry_type.value = et;
  document.querySelectorAll("[data-et]").forEach((x) => x.classList.toggle("active", x.dataset.et === et));
  const show = (id, on) => { const e = document.getElementById(id); if (e) e.style.display = on ? "" : "none"; };
  show("entryPriceWrap", et === "LIMIT");
  show("schedWrap", et === "SCHEDULED");
  show("trigWrap", et === "TRIGGER");
  entryPrice.disabled = et !== "LIMIT";
  if (et !== "LIMIT") entryPrice.value = 0;
}
document.querySelectorAll("[data-et]").forEach((b) => { b.onclick = () => applyEntryType(b.dataset.et); });
applyEntryType("MARKET");   // normalize the optional entry fields on load

// small helper: read/write a numeric input by id
const numVal = (id) => parseFloat(document.getElementById(id).value) || 0;
const setVal = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };

const multiToggle = document.getElementById("multiToggle");
const multiWrap = document.getElementById("multiWrap");
const targetField = document.getElementById("targetField");
multiToggle.onchange = () => {
  const on = multiToggle.checked;
  multiWrap.style.display = on ? "block" : "none";
  targetField.style.display = on ? "none" : "";
  if (on && !document.querySelector("#targetRows .trow")) addTargetRow();
};
document.getElementById("addTarget").onclick = () => addTargetRow();

function newTargetCount() { return document.querySelectorAll("#targetRows .trow").length; }

// Spread the total lots evenly across the target rows and show the QUANTITY
// (lots x lot size) for each, so lot size is clearly applied.
function distributeNewTargets() {
  const rows = [...document.querySelectorAll("#targetRows .trow")];
  const N = parseInt(lotsInput.value) || 1, M = rows.length;
  if (!M) return;
  const base = Math.floor(N / M), rem = N % M;
  rows.forEach((r, i) => {
    const lots = base + (i < rem ? 1 : 0);
    r.querySelector(".tl").value = lots * currentLotSize;       // quantity
    r.querySelector(".tlots").textContent = `(${lots} lot${lots > 1 ? "s" : ""})`;
  });
  updateCumulative();
}

// Show the running (cumulative) profit level each target actually exits at.
function updateCumulative() {
  let running = 0;
  document.querySelectorAll("#targetRows .trow").forEach((r) => {
    running += parseFloat(r.querySelector(".tp").value) || 0;
    const c = r.querySelector(".tcum");
    if (c) c.textContent = running > 0 ? `→ exits at +${running} pts` : "";
  });
}
// If lots drop below the number of targets, trim extra targets, then redistribute.
function trimAndDistribute() {
  const N = parseInt(lotsInput.value) || 1;
  const rows = [...document.querySelectorAll("#targetRows .trow")];
  while (rows.length > N) rows.pop().remove();
  distributeNewTargets();
}
function addTargetRow(points = "") {
  const N = parseInt(lotsInput.value) || 1;
  if (newTargetCount() >= N) {
    alert(`You can have at most ${N} target(s) — one per lot. Increase Lots to add more.`);
    return;
  }
  const div = document.createElement("div");
  div.className = "trow";
  div.innerHTML = `<input class="tp" type="number" step="0.05" placeholder="points" value="${points}" />
    <input class="tl" type="number" readonly title="quantity (auto-distributed)" style="width:80px;" />
    <span class="muted" style="font-size:11px;">qty <span class="tlots"></span> <span class="tcum" style="color:var(--accent);"></span></span>
    <button type="button" class="step trm">×</button>`;
  div.querySelector(".tp").addEventListener("input", updateCumulative);
  div.querySelector(".trm").onclick = () => { div.remove(); distributeNewTargets(); };
  document.getElementById("targetRows").appendChild(div);
  distributeNewTargets();
}

// Auto-generate equal target legs: total target split into N legs, lots spread evenly.
document.getElementById("genLegs").onclick = () => {
  const total = parseFloat(document.getElementById("autoTotal").value) || 0;
  const legs = parseInt(document.getElementById("autoLegs").value) || 0;
  const N = parseInt(lotsInput.value) || 1;
  if (total <= 0 || legs < 1) { alert("Enter total target points and number of legs."); return; }
  if (legs > N) { alert(`Legs (${legs}) cannot exceed Lots (${N}) — each leg needs at least 1 lot.`); return; }
  document.getElementById("targetRows").innerHTML = "";
  const per = Math.round((total / legs) * 100) / 100;   // incremental points per leg
  for (let i = 0; i < legs; i++) addTargetRow(per);
};

function resetOrderForm() {
  document.querySelectorAll("[data-side]").forEach((x) => x.classList.toggle("active", x.dataset.side === "BUY"));
  form.side.value = "BUY";
  setMode("TEST");
  applyEntryType("MARKET");
  multiToggle.checked = false; multiWrap.style.display = "none"; targetField.style.display = "";
  document.getElementById("targetRows").innerHTML = "";
  if (form.max_profit_amt) form.max_profit_amt.value = 0;
  if (form.max_loss_amt) form.max_loss_amt.value = 0;
  if (form.lock_step) form.lock_step.value = 0;
  if (form.lock_amount) form.lock_amount.value = 0;
  currentLotSize = 1; lotsInput.value = 1; updateQty();
}
const HINTS = { OPTION: "— search index / stock / commodity (NIFTY, RELIANCE, GOLD…), then pick a strike",
                FUTURES: "— search index / stock / commodity future (NIFTY, CRUDEOIL…)",
                EQUITY: "— search a stock or index" };

function resetPicker() {
  currentUnderlying = null;
  _selContract = null;        // stop the stream from refreshing a stale selection
  ulResults.classList.remove("show");
  expiryWrap.style.display = "none";
  chainWrap.style.display = "none";
  futWrap.style.display = "none";
  futWrap.innerHTML = ""; chainBody.innerHTML = "";
  if (ltpTimer) { clearInterval(ltpTimer); ltpTimer = null; }
  if (typeof selLtpTimer !== "undefined" && selLtpTimer) { clearInterval(selLtpTimer); selLtpTimer = null; }
  pushWatch();                // clear the server-side watch for this chain
}

document.querySelectorAll("[data-seg]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("[data-seg]").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    currentSeg = b.dataset.seg;
    pickerHint.textContent = HINTS[currentSeg];
    ulSearch.value = ""; resetPicker();
  };
});
pickerHint.textContent = HINTS.OPTION;

ulSearch.addEventListener("input", () => {
  clearTimeout(ulTimer);
  const q = ulSearch.value.trim();
  if (q.length < 2) { ulResults.classList.remove("show"); return; }
  ulTimer = setTimeout(async () => {
    const rows = currentSeg === "EQUITY"
      ? await api.get("/api/equities/search?q=" + encodeURIComponent(q))
      : await api.get(`/api/underlyings/search?kind=${currentSeg}&q=` + encodeURIComponent(q));
    renderUlResults(rows);
  }, 250);
});

function renderUlResults(rows) {
  const syncNote = window._angelSyncing
    ? `<div class="item"><div class="meta"><span class="spinner"></span> Syncing Angel instruments… some strikes may not be visible yet</div></div>` : "";
  if (!rows.length) {
    ulResults.innerHTML = syncNote || `<div class="item"><div class="meta">No matches.</div></div>`;
  } else if (currentSeg === "EQUITY") {
    ulResults.innerHTML = rows.map((r, i) => `<div class="item" data-i="${i}">
      <div class="sym">${r.symbol}</div>
      <div class="meta">${r.instrument_type} · ${r.exchange_segment} · id ${r.security_id}</div></div>`).join("");
    ulResults.querySelectorAll(".item").forEach((el) => {
      const r = rows[el.dataset.i]; if (r) el.onclick = () => { pickContract(r); ulResults.classList.remove("show"); };
    });
  } else {
    ulResults.innerHTML = rows.map((r, i) => `<div class="item" data-i="${i}">
      <div class="sym">${r.underlying} <span class="exch-tag">${r.exchange || ""}</span></div>
      <div class="meta">${currentSeg === "OPTION" ? "Options" : "Futures"} available</div></div>`).join("");
    ulResults.querySelectorAll(".item").forEach((el) => {
      const r = rows[el.dataset.i]; if (r) el.onclick = () => selectUnderlying(r.underlying);
    });
  }
  ulResults.classList.add("show");
}

async function selectUnderlying(underlying) {
  currentUnderlying = underlying;
  ulSearch.value = underlying;
  ulResults.classList.remove("show");
  applyPreset(underlying, currentSeg);   // auto-fill saved defaults for this instrument type
  if (currentSeg === "OPTION") {
    const exps = await api.get("/api/expiries?kind=OPTION&underlying=" + encodeURIComponent(underlying));
    expirySelect.innerHTML = exps.map((e) => `<option>${e}</option>`).join("");
    expiryWrap.style.display = exps.length ? "" : "none";
    futWrap.style.display = "none";
    if (exps.length) loadChain(underlying, exps[0]);
  } else {
    const futs = await api.get("/api/futures?underlying=" + encodeURIComponent(underlying));
    expiryWrap.style.display = "none"; chainWrap.style.display = "none";
    renderFutures(futs);
  }
}

expirySelect.onchange = () => { if (currentUnderlying) loadChain(currentUnderlying, expirySelect.value); };

async function loadChain(underlying, expiry) {
  const data = await api.get(`/api/optionchain?underlying=${encodeURIComponent(underlying)}&expiry=${encodeURIComponent(expiry)}`);
  chainData = data.strikes;
  chainScrolled = false;
  chainBody.innerHTML = data.strikes.map((row, i) => {
    const cell = (c, cls) => c
      ? `<div class="chain-cell ${cls}" data-c='${JSON.stringify(c)}'><span class="ltp dim" data-ltp="${c.security_id}">tap to pick</span></div>`
      : `<div class="chain-cell ${cls}"><span class="ltp dim">-</span></div>`;
    return `<div class="chain-row" data-row="${i}">${cell(row.ce, "ce")}
      <div class="chain-strike" data-strike="${i}">${row.strike}</div>${cell(row.pe, "pe")}</div>`;
  }).join("");
  chainWrap.style.display = "block";
  chainBody.querySelectorAll(".chain-cell[data-c]").forEach((el) => {
    el.onclick = () => { pickContract(JSON.parse(el.dataset.c)); markSelected(el); };
  });
  refreshChainLtp();
  pushWatch();                 // stream live ticks for this chain
  if (ltpTimer) clearInterval(ltpTimer);
  ltpTimer = setInterval(refreshChainLtp, 5000);
}

function markSelected(el) {
  chainBody.querySelectorAll(".chain-cell.sel").forEach((x) => x.classList.remove("sel"));
  el.classList.add("sel");
}

async function refreshChainLtp() {
  // Don't poll Dhan for chain prices unless the New Trade tab is open (saves
  // requests / avoids rate limits).
  if (!document.getElementById("tab-new").classList.contains("active")) return;
  const cells = [...chainBody.querySelectorAll(".chain-cell[data-c]")].slice(0, 500);
  if (!cells.length) return;
  const items = cells.map((el) => { const c = JSON.parse(el.dataset.c);
    return { security_id: c.security_id, exchange_segment: c.exchange_segment }; });
  let res; try { res = await api.post("/api/ltp", { items }); } catch { return; }
  const legend = document.querySelector(".chain-legend");
  if (res.need_broker) {
    if (legend) legend.innerHTML = '⚠️ <b>Connect a broker</b> to see live prices — go to the <span class="link" onclick="document.querySelector(\'.tab[data-tab=&quot;broker&quot;]\').click()">Broker</span> tab.';
    return;
  }
  if (res.error && !Object.keys(res.prices || {}).length && legend) {
    legend.innerHTML = `⚠️ Price feed: ${esc(res.error).slice(0, 120)}`;
  }
  const prices = res.prices || {};
  chainBody.querySelectorAll(".ltp[data-ltp]").forEach((sp) => {
    const p = prices[sp.dataset.ltp];
    if (p != null) { sp.textContent = "₹" + p; sp.classList.remove("dim"); }
  });
  classifyChain(prices);
}

// Mark each strike ITM / ATM / OTM and scroll to ATM.
// ATM = the strike where Call and Put premiums are closest (at-the-money).
function classifyChain(prices) {
  let atm = -1, best = Infinity;
  chainData.forEach((row, i) => {
    if (!row.ce || !row.pe) return;
    const ce = prices[row.ce.security_id], pe = prices[row.pe.security_id];
    if (ce == null || pe == null || ce <= 0 || pe <= 0) return;
    const diff = Math.abs(ce - pe);
    if (diff < best) { best = diff; atm = i; }
  });
  if (atm < 0) return;                       // no live prices yet
  const atmStrike = chainData[atm].strike;
  chainData.forEach((row, i) => {
    const el = chainBody.querySelector(`.chain-row[data-row="${i}"]`);
    if (!el) return;
    const ceCell = el.querySelector(".chain-cell.ce");
    const peCell = el.querySelector(".chain-cell.pe");
    el.classList.toggle("atm", i === atm);
    // CALL: in-the-money when strike is below spot; PUT: opposite.
    if (ceCell) { ceCell.classList.toggle("itm", row.strike < atmStrike);
                  ceCell.classList.toggle("otm", row.strike > atmStrike); }
    if (peCell) { peCell.classList.toggle("itm", row.strike > atmStrike);
                  peCell.classList.toggle("otm", row.strike < atmStrike); }
    const sCell = el.querySelector(".chain-strike");
    if (sCell) sCell.innerHTML = row.strike + (i === atm ? ' <span class="atm-badge">ATM</span>' : "");
  });
  if (!chainScrolled) {
    const atmEl = chainBody.querySelector(`.chain-row[data-row="${atm}"]`);
    if (atmEl) { atmEl.scrollIntoView({ block: "center" }); chainScrolled = true; }
  }
}

// ---- real-time tick stream (Server-Sent Events) -------------------------
// The broker's WebSocket pushes ticks to the server; the server streams them
// here. The 5s /api/ltp poll stays on as a safety net, so prices keep flowing
// even if this stream (or the underlying socket) drops.
let _livePx = {};            // security_id -> latest price (from ticks)
let _selContract = null;     // the contract currently shown in "selected"
let _es = null;
let _classifyTimer = null;

function stopStream() {
  try { if (_es) _es.close(); } catch (e) {}
  _es = null;
}
function startStream() {
  if (typeof EventSource === "undefined") return;
  if (document.hidden) return;                 // don't stream to a backgrounded tab
  stopStream();                                // never stack a second connection
  try { _es = new EventSource("/api/stream"); } catch (e) { _es = null; return; }
  _es.onmessage = (ev) => {
    if (!ev.data) return;
    let obj; try { obj = JSON.parse(ev.data); } catch (e) { return; }
    onTicks(obj);
  };
  _es.onerror = () => {
    // The browser auto-reconnects an open stream; if it fully closed, retry.
    if (_es && _es.readyState === EventSource.CLOSED) { _es = null; if (!document.hidden) setTimeout(startStream, 3000); }
  };
}
// Release the live stream when the tab is hidden (saves a server connection per
// idle tab); reopen it the moment the user comes back.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopStream(); else startStream();
});

function onTicks(obj) {
  let touchedChain = false;
  for (const sid in obj) {
    const px = obj[sid];
    _livePx[sid] = px;
    // live option-chain cells
    chainBody.querySelectorAll(`.ltp[data-ltp="${sid}"]`).forEach((sp) => {
      sp.textContent = "₹" + px; sp.classList.remove("dim"); touchedChain = true;
    });
    // the contract picked into the trade form (futures / equity / option)
    if (_selContract && String(_selContract.security_id) === String(sid)) showSelected(_selContract, px);
  }
  // recompute ATM / ITM / OTM from the freshest prices (throttled)
  if (touchedChain && !_classifyTimer) {
    _classifyTimer = setTimeout(() => { _classifyTimer = null; classifyChain(_livePx); }, 600);
  }
}

// Tell the server which instruments this browser is looking at, so it keeps the
// broker socket subscribed to them. Debounced; reads the current chain + pick.
let _watchTimer = null;
function pushWatch() {
  if (_watchTimer) clearTimeout(_watchTimer);
  _watchTimer = setTimeout(_doPushWatch, 150);
}
async function _doPushWatch() {
  const map = {};
  const onNew = document.getElementById("tab-new").classList.contains("active");
  if (onNew) {
    chainBody.querySelectorAll(".chain-cell[data-c]").forEach((el) => {
      try {
        const c = JSON.parse(el.dataset.c);
        if (c.security_id) map[c.exchange_segment + "|" + c.security_id] =
          { security_id: c.security_id, exchange_segment: c.exchange_segment };
      } catch (e) {}
    });
    if (form.security_id.value && form.exchange_segment.value)
      map[form.exchange_segment.value + "|" + form.security_id.value] =
        { security_id: form.security_id.value, exchange_segment: form.exchange_segment.value };
  }
  try { await api.post("/api/stream/watch", { items: Object.values(map).slice(0, 500) }); } catch (e) {}
}

function renderFutures(futs) {
  if (!futs.length) { futWrap.style.display = "none"; return; }
  futWrap.innerHTML = futs.map((f, i) => `<button type="button" class="fut-btn" data-i="${i}">
    ${f.symbol}<br><span class="muted" style="font-size:11px;">exp ${f.expiry} · lot <span class="fut-lot">${parseInt(parseFloat(f.lot_size))}</span></span></button>`).join("");
  futWrap.style.display = "flex";
  // All expiries of one commodity share the same lot — fetch the broker-correct
  // value once and update every hint (Dhan reports 1 for MCX; brokers differ).
  api.get(`/api/lotsize?security_id=${encodeURIComponent(futs[0].security_id)}&exchange_segment=${encodeURIComponent(futs[0].exchange_segment)}`)
    .then((d) => { const L = parseInt(d.lot_size) || 0; if (L > 0) futWrap.querySelectorAll(".fut-lot").forEach((e) => (e.textContent = L)); })
    .catch(() => {});
  futWrap.querySelectorAll(".fut-btn").forEach((el) => {
    const f = futs[el.dataset.i];
    el.onclick = () => { pickContract(f); futWrap.querySelectorAll(".fut-btn").forEach((x) => x.classList.remove("sel")); el.classList.add("sel"); };
  });
}

let selLtpTimer = null;
function pickContract(r) {
  form.symbol.value = r.symbol;
  form.security_id.value = r.security_id;
  form.exchange_segment.value = r.exchange_segment;
  form.instrument_type.value = r.instrument_type;
  _selContract = r;            // stream updates this contract's LTP live
  currentLotSize = (r.lot_size && parseInt(parseFloat(r.lot_size)) > 0) ? parseInt(parseFloat(r.lot_size)) : 1;
  updateQty();
  // Lot size is broker-specific (esp. MCX) — fetch the correct one from the
  // connected broker's master and refresh the qty once it returns.
  api.get(`/api/lotsize?security_id=${encodeURIComponent(r.security_id)}&exchange_segment=${encodeURIComponent(r.exchange_segment)}`)
    .then((d) => {
      if (form.security_id.value === r.security_id && d && parseInt(d.lot_size) > 0) {
        currentLotSize = parseInt(d.lot_size); updateQty();
      }
    }).catch(() => {});
  pushWatch();                 // ensure the socket streams the picked contract
  applyPreset((currentSeg === "EQUITY" ? r.symbol : currentUnderlying) || r.symbol, currentSeg);
  // Auto-fetch & live-update the LTP for stocks / futures / index right away.
  if (selLtpTimer) { clearInterval(selLtpTimer); selLtpTimer = null; }
  if (["EQUITY", "FUTURES", "INDEX"].includes(r.instrument_type)) {
    showSelected(r, "LOADING");           // inline spinner until first tick
    let gotPrice = false;
    const run = async () => {
      if (form.security_id.value !== r.security_id) return;
      let res; try { res = await api.post("/api/ltp", { items: [{ security_id: r.security_id, exchange_segment: r.exchange_segment }] }); } catch { return; }
      const p = (res.prices || {})[r.security_id];
      if (p != null) { gotPrice = true; showSelected(r, p); }
    };
    run();
    selLtpTimer = setInterval(run, 3000);
    // Timeout/fallback: if no tick within 5s, stop spinning and warn.
    setTimeout(() => { if (!gotPrice && form.security_id.value === r.security_id) showSelected(r, "SLOW"); }, 5000);
  } else {
    showSelected(r, null);
  }
}
function showSelected(r, ltp) {
  let ltpTxt = "";
  if (ltp === "LOADING") ltpTxt = ` · <span class="spinner"></span>`;
  else if (ltp === "SLOW") ltpTxt = ` · <span style="color:var(--red)">price slow… <span class="link" onclick="retrySelLtp()">retry</span></span>`;
  else if (ltp != null) ltpTxt = ` · <span style="color:var(--accent)">LTP ₹${ltp}</span>`;
  document.getElementById("selectedSymbol").innerHTML =
    `✅ <b>${r.symbol}</b> — ${r.instrument_type} · ${r.exchange_segment} · ID ${r.security_id} · lot ${currentLotSize}${ltpTxt}`;
}
function retrySelLtp() {
  if (form.security_id.value) {
    pickContract({ symbol: form.symbol.value, security_id: form.security_id.value,
      exchange_segment: form.exchange_segment.value, instrument_type: form.instrument_type.value,
      lot_size: currentLotSize });
  }
}

document.addEventListener("click", (e) => {
  if (!ulSearch.contains(e.target) && !ulResults.contains(e.target)) ulResults.classList.remove("show");
});

// ---- new trade form ----
form.onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = Object.fromEntries(fd.entries());
  ["entry_price", "sl_points", "target_points", "trail_sl", "trigger_price",
   "max_profit_amt", "max_loss_amt", "lock_step", "lock_amount"]
    .forEach((k) => (payload[k] = parseFloat(payload[k]) || 0));
  payload.quantity = parseInt(payload.quantity) || 1;
  payload.lot_size = currentLotSize;
  if (multiToggle.checked) {
    payload.targets = [...document.querySelectorAll("#targetRows .trow")].map((r) => ({
      points: parseFloat(r.querySelector(".tp").value) || 0,
      qty: parseInt(r.querySelector(".tl").value) || 0,     // .tl already holds quantity
    })).filter((x) => x.points > 0 && x.qty > 0);
    payload.target_points = 0;
  } else {
    payload.targets = [];
  }
  const msg = document.getElementById("formMsg");
  if (!payload.security_id) {
    msg.textContent = "❌ Please search and select a symbol first.";
    msg.className = "msg neg"; return;
  }
  // Group mode: this form is firing a copy-trading group's master order.
  if (gpMode.active) { await gpSubmit(payload); return; }
  // Basket mode: this form is configuring a basket leg, not a standalone trade.
  if (bkMode.active) { await bkSubmitLeg(payload); return; }
  try {
    const t = await api.post("/api/trades", payload);
    msg.textContent = `✅ Created trade #${t.id} (${t.symbol}).`; msg.className = "msg pos";
    toast(`✅ Trade #${t.id} created — ${t.symbol}`, "pos");
    e.target.reset();
    document.getElementById("selectedSymbol").textContent = "No symbol selected yet.";
    ulSearch.value = ""; resetOrderForm(); resetPicker();
    await refreshAll();
  } catch (err) { msg.textContent = "❌ " + err.message; msg.className = "msg neg"; }
};

// ====================== FOREX / CRYPTO TRADE (Delta etc.) ======================
// Self-contained tab: its own symbol search, order form and watchlist. Shown only
// when a Forex-category broker is connected. Orders use the same /api/trades engine.
const FOREX_SEGMENTS = ["DELTA"];
const FOREX_SEGMENT_FOR = { DELTA: "DELTA" };
const FX = { mode: "TEST", side: "BUY", et: "MARKET", broker: "DELTA", account: null, sel: null, ltpTimer: null };
const isForexItem = (w) => FOREX_SEGMENTS.includes(w.exchange_segment);

function fxApplyBrokerState(b) {
  const fb = (b && b.forex_brokers) || ["DELTA"];
  // Show the Forex tab once a Forex broker has been SET UP (credentials saved),
  // not only after it connects. Prefer a connected account for trading.
  const forexAccts = (b && b.accounts || []).filter((a) => fb.includes(a.broker) && (a.connected || a.has_secret));
  const acc = forexAccts.find((a) => a.connected) || forexAccts[0] || null;
  FX.account = acc;
  FX.broker = acc ? acc.broker : "DELTA";
  const tab = document.querySelector('.tab[data-tab="forex"]');
  if (tab) tab.style.display = acc ? "" : "none";
  // If the Forex broker is removed while its tab is open, fall back to Orders.
  if (!acc && document.body.getAttribute("data-tab") === "forex") {
    const ot = document.querySelector('.tab[data-tab="dashboard"]'); if (ot) ot.click();
  }
  fxRenderBanner(b);
}

function fxRenderBanner(b) {
  const banner = document.getElementById("fxProviderBanner");
  if (!banner) return;
  if (!FX.account) { FX.onForex = false; banner.style.display = "none"; return; }
  const id = String(FX.account.id);
  banner.style.display = "block";
  if (!FX.account.connected) {                 // set up but not authenticated yet
    FX.onForex = false;
    banner.className = "banner";
    banner.innerHTML = `⚠️ ${FX.account.label} is not connected yet. Open the ` +
      `<span class="link" onclick="document.querySelector('.tab[data-tab=&quot;broker&quot;]').click()">Broker</span> tab and click <b>Login</b> to trade crypto.`;
    return;
  }
  const onForex = String(b.trade_provider || "") === id && String(b.data_provider || "") === id;
  FX.onForex = onForex;
  if (onForex) {
    banner.className = "banner ok";
    banner.textContent = `✅ Crypto prices & orders are routing to ${FX.account.label}.`;
  } else {
    banner.className = "banner";
    banner.innerHTML = `⚠️ Live prices & orders here need ${FX.account.label} set as your Data + Trading account. ` +
      `<button type="button" class="btn btn-sm" id="fxUseProvider">Use ${FX.account.label}</button>`;
    const btn = document.getElementById("fxUseProvider");
    if (btn) btn.onclick = async () => {
      try {
        await api.post("/api/providers", { data_provider: id, trade_provider: id });
        toast("✅ Switched to " + FX.account.label, "pos"); await refreshBroker();
      } catch (e) { toast("❌ " + e.message, "neg"); }
    };
  }
}

// mode / side / entry-type toggles
document.querySelectorAll("[data-fxmode]").forEach((b) => b.onclick = () => {
  document.querySelectorAll("[data-fxmode]").forEach((x) => x.classList.remove("active"));
  b.classList.add("active"); FX.mode = b.dataset.fxmode;
});
document.querySelectorAll("[data-fxside]").forEach((b) => b.onclick = () => {
  document.querySelectorAll("[data-fxside]").forEach((x) => x.classList.remove("active"));
  b.classList.add("active"); FX.side = b.dataset.fxside;
});
document.querySelectorAll("[data-fxet]").forEach((b) => b.onclick = () => {
  document.querySelectorAll("[data-fxet]").forEach((x) => x.classList.remove("active"));
  b.classList.add("active"); FX.et = b.dataset.fxet;
  document.getElementById("fxEntryPrice").disabled = FX.et !== "LIMIT";
});
document.getElementById("fxQtyMinus").onclick = () => { const i = document.getElementById("fxQty"); i.value = Math.max(1, (parseInt(i.value) || 1) - 1); };
document.getElementById("fxQtyPlus").onclick = () => { const i = document.getElementById("fxQty"); i.value = (parseInt(i.value) || 1) + 1; };

// symbol search
let fxTimer = null;
document.getElementById("fxSearch").addEventListener("input", () => {
  clearTimeout(fxTimer);
  const box = document.getElementById("fxResults");
  const q = document.getElementById("fxSearch").value.trim();
  if (q.length < 2) { box.classList.remove("show"); box.innerHTML = ""; return; }
  fxTimer = setTimeout(async () => {
    let rows = [];
    try { rows = await api.get(`/api/forex/search?broker=${FX.broker}&q=` + encodeURIComponent(q)); } catch {}
    if (!rows.length) {
      box.innerHTML = `<div class="item"><div class="meta">No matches (product list may still be loading).</div></div>`;
    } else {
      box.innerHTML = rows.map((r, i) => `<div class="item" data-i="${i}">
        <div class="sym">${r.symbol}</div>
        <div class="meta">${r.contract_type || "product"} · id ${r.product_id}</div></div>`).join("");
      box.querySelectorAll(".item").forEach((el) => { const r = rows[el.dataset.i]; if (r) el.onclick = () => fxPick(r); });
    }
    box.classList.add("show");
  }, 250);
});
document.addEventListener("click", (e) => {
  const box = document.getElementById("fxResults"), inp = document.getElementById("fxSearch");
  if (box && inp && !inp.contains(e.target) && !box.contains(e.target)) box.classList.remove("show");
});

function fxSet(sel) {
  FX.sel = sel;
  document.getElementById("fxSearch").value = sel.symbol;
  document.getElementById("fxResults").classList.remove("show");
  fxShowSelected("LOADING");
  fxStartLtp();
}
function fxPick(r) {
  fxSet({ symbol: r.symbol, security_id: r.symbol, exchange_segment: FOREX_SEGMENT_FOR[FX.broker] || "DELTA",
          instrument_type: "FUTURES", product_id: r.product_id, contract_value: r.contract_value });
}
function fxShowSelected(px) {
  const el = document.getElementById("fxSelected");
  if (!FX.sel) { el.textContent = "No symbol selected yet."; return; }
  const pxTxt = px === "LOADING" ? '<span class="muted">fetching price…</span>'
    : (px > 0 ? `LTP <b>${px}</b>` : '<span class="muted">price unavailable — set this broker as your Data account</span>');
  // Show the contract value so the point math is clear: P&L = qty × contract_value × move.
  const cv = parseFloat(FX.sel.contract_value);
  const cvTxt = cv > 0 ? ` · <span class="muted">1 contract = ${FX.sel.contract_value}; P&L = qty × ${FX.sel.contract_value} × points</span>` : "";
  el.innerHTML = `✅ <b>${FX.sel.symbol}</b> — ${FX.broker}${FX.sel.product_id ? " · id " + FX.sel.product_id : ""} — ${pxTxt}${cvTxt}`;
}
function fxStartLtp() {
  if (FX.ltpTimer) { clearInterval(FX.ltpTimer); FX.ltpTimer = null; }
  const run = async () => {
    if (!FX.sel) return;
    let res; try { res = await api.post("/api/ltp", { items: [{ security_id: FX.sel.security_id, exchange_segment: FX.sel.exchange_segment }] }); } catch { return; }
    const p = (res.prices || {})[FX.sel.security_id];
    if (p != null) fxShowSelected(p);
  };
  run(); FX.ltpTimer = setInterval(run, 3000);
}

// ---- options chain (Delta) ----
let fxChainData = [], fxChainTimer = null, fxUnderlying = null;
document.querySelectorAll("[data-fxseg]").forEach((b) => b.onclick = () => {
  document.querySelectorAll("[data-fxseg]").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  const opt = b.dataset.fxseg === "OPTION";
  document.getElementById("fxPerpPicker").style.display = opt ? "none" : "";
  document.getElementById("fxOptionPicker").style.display = opt ? "" : "none";
  if (opt) { if (!document.getElementById("fxUlButtons").children.length) fxLoadUnderlyings(); }
  else if (fxChainTimer) { clearInterval(fxChainTimer); fxChainTimer = null; }
});

async function fxLoadUnderlyings() {
  const box = document.getElementById("fxUlButtons");
  let uls = [];
  try { uls = await api.get(`/api/forex/underlyings?broker=${FX.broker}`); } catch {}
  if (!uls.length) {
    box.innerHTML = `<span class="muted" style="font-size:12px;">No options found yet (the product list may still be loading).</span>`;
    return;
  }
  box.innerHTML = uls.map((u) => `<button type="button" class="seg" data-fxul="${u}">${u}</button>`).join("");
  box.querySelectorAll("[data-fxul]").forEach((el) => el.onclick = () => fxSelectUnderlying(el.dataset.fxul));
}

async function fxSelectUnderlying(u) {
  fxUnderlying = u;
  document.querySelectorAll("[data-fxul]").forEach((x) => x.classList.toggle("active", x.dataset.fxul === u));
  let exps = [];
  try { exps = await api.get(`/api/forex/expiries?broker=${FX.broker}&underlying=` + encodeURIComponent(u)); } catch {}
  const sel = document.getElementById("fxExpiry");
  sel.innerHTML = exps.map((e) => `<option>${e}</option>`).join("");
  if (exps.length) fxLoadChain(u, exps[0]);
  else { document.getElementById("fxChainWrap").style.display = "none"; }
}
document.getElementById("fxExpiry").onchange = () => { if (fxUnderlying) fxLoadChain(fxUnderlying, document.getElementById("fxExpiry").value); };

async function fxLoadChain(underlying, expiry) {
  let data; try { data = await api.get(`/api/forex/optionchain?broker=${FX.broker}&underlying=${encodeURIComponent(underlying)}&expiry=${encodeURIComponent(expiry)}`); } catch { return; }
  fxChainData = data.strikes || [];
  const body = document.getElementById("fxChainBody");
  body.innerHTML = fxChainData.map((row, i) => {
    const cell = (c, cls) => c
      ? `<div class="chain-cell ${cls}" data-c='${JSON.stringify(c)}'><span class="ltp dim" data-ltp="${c.security_id}">tap to pick</span></div>`
      : `<div class="chain-cell ${cls}"><span class="ltp dim">-</span></div>`;
    return `<div class="chain-row" data-row="${i}">${cell(row.ce, "ce")}
      <div class="chain-strike">${row.strike}</div>${cell(row.pe, "pe")}</div>`;
  }).join("");
  document.getElementById("fxChainWrap").style.display = fxChainData.length ? "block" : "none";
  body.querySelectorAll(".chain-cell[data-c]").forEach((el) => {
    el.onclick = () => {
      body.querySelectorAll(".chain-cell.sel").forEach((x) => x.classList.remove("sel"));
      el.classList.add("sel");
      fxSet(JSON.parse(el.dataset.c));
    };
  });
  fxChainLtp();
  if (fxChainTimer) clearInterval(fxChainTimer);
  fxChainTimer = setInterval(fxChainLtp, 5000);
}

async function fxChainLtp() {
  if (document.body.getAttribute("data-tab") !== "forex") return;
  const body = document.getElementById("fxChainBody");
  const cells = [...body.querySelectorAll(".chain-cell[data-c]")].slice(0, 500);
  if (!cells.length) return;
  const items = cells.map((el) => { const c = JSON.parse(el.dataset.c);
    return { security_id: c.security_id, exchange_segment: c.exchange_segment }; });
  let res; try { res = await api.post("/api/ltp", { items }); } catch { return; }
  const prices = res.prices || {};
  body.querySelectorAll(".ltp[data-ltp]").forEach((sp) => {
    const p = prices[sp.dataset.ltp]; if (p != null) { sp.textContent = p; sp.classList.remove("dim"); }
  });
  fxClassifyChain(prices);
}

// Mark ATM (closest CALL/PUT premium) + ITM/OTM shading, like the India chain.
function fxClassifyChain(prices) {
  let atm = -1, best = Infinity;
  fxChainData.forEach((row, i) => {
    if (!row.ce || !row.pe) return;
    const ce = prices[row.ce.security_id], pe = prices[row.pe.security_id];
    if (ce == null || pe == null || ce <= 0 || pe <= 0) return;
    const diff = Math.abs(ce - pe);
    if (diff < best) { best = diff; atm = i; }
  });
  if (atm < 0) return;
  const atmStrike = fxChainData[atm].strike;
  const body = document.getElementById("fxChainBody");
  fxChainData.forEach((row, i) => {
    const el = body.querySelector(`.chain-row[data-row="${i}"]`); if (!el) return;
    const ceCell = el.querySelector(".chain-cell.ce"), peCell = el.querySelector(".chain-cell.pe");
    el.classList.toggle("atm", i === atm);
    if (ceCell) { ceCell.classList.toggle("itm", row.strike < atmStrike); ceCell.classList.toggle("otm", row.strike > atmStrike); }
    if (peCell) { peCell.classList.toggle("itm", row.strike > atmStrike); peCell.classList.toggle("otm", row.strike < atmStrike); }
    const sCell = el.querySelector(".chain-strike");
    if (sCell) sCell.innerHTML = row.strike + (i === atm ? ' <span class="atm-badge">ATM</span>' : "");
  });
}

document.getElementById("fxSubmit").onclick = async () => {
  const msg = document.getElementById("fxMsg");
  if (!FX.sel) { msg.textContent = "❌ Search and select a symbol first."; msg.className = "msg neg"; return; }
  if (FX.mode === "LIVE" && !FX.onForex) {
    msg.textContent = `❌ Set ${FX.account ? FX.account.label : "your crypto broker"} as the Data + Trading account first (use the banner above), or a LIVE order would route to the wrong broker.`;
    msg.className = "msg neg"; return;
  }
  const payload = {
    mode: FX.mode, symbol: FX.sel.symbol, security_id: FX.sel.security_id,
    exchange_segment: FX.sel.exchange_segment, instrument_type: FX.sel.instrument_type,
    side: FX.side, quantity: Math.max(1, parseInt(document.getElementById("fxQty").value) || 1),
    lot_size: 1, entry_type: FX.et,
    entry_price: parseFloat(document.getElementById("fxEntryPrice").value) || 0,
    sl_points: parseFloat(document.getElementById("fxSl").value) || 0,
    target_points: parseFloat(document.getElementById("fxTarget").value) || 0,
  };
  try {
    const t = await api.post("/api/trades", payload);
    msg.textContent = `✅ Created trade #${t.id} (${t.symbol}).`; msg.className = "msg pos";
    toast(`✅ Forex trade #${t.id} — ${t.symbol}`, "pos");
    await refreshAll();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

document.getElementById("fxAddWatch").onclick = async () => {
  const msg = document.getElementById("fxMsg");
  if (!FX.sel) { msg.textContent = "❌ Select a symbol first."; msg.className = "msg neg"; return; }
  try {
    await api.post("/api/watchlist", { symbol: FX.sel.symbol, security_id: FX.sel.security_id,
      exchange_segment: FX.sel.exchange_segment, instrument_type: FX.sel.instrument_type,
      underlying: FX.sel.symbol, lot_size: 1 });
    toast(`⭐ ${FX.sel.symbol} added to watchlist`, "pos");
    await loadWatchlist();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

function fxRenderWatch(items) {
  const box = document.getElementById("fxWatchChips"), empty = document.getElementById("fxWlEmpty");
  if (!box || !empty) return;
  if (!items.length) { box.innerHTML = ""; empty.style.display = ""; return; }
  empty.style.display = "none";
  box.innerHTML = items.map((w) =>
    `<span class="wl-chip" data-fxwl="${w.id}" title="${w.exchange_segment} · ${w.security_id}">${w.symbol}<span class="wl-x" data-fxwlx="${w.id}">×</span></span>`).join("");
  box.querySelectorAll(".wl-chip").forEach((el) => {
    el.onclick = (e) => { if (e.target.classList.contains("wl-x")) return;
      const w = items.find((x) => String(x.id) === el.dataset.fxwl); if (w) fxWlLoad(w); };
  });
  box.querySelectorAll(".wl-x").forEach((x) => {
    x.onclick = async (e) => { e.stopPropagation(); await api.del("/api/watchlist/" + x.dataset.fxwlx); await loadWatchlist(); };
  });
}
function fxWlLoad(w) {
  document.querySelector('.tab[data-tab="forex"]').click();
  const acc = document.querySelector("#tab-forex .watchlist-acc"); if (acc) acc.open = false;
  fxSet({ symbol: w.symbol, security_id: w.security_id || w.symbol,
          exchange_segment: w.exchange_segment || "DELTA", instrument_type: w.instrument_type || "FUTURES", product_id: "" });
}

// ---- logout ----
document.getElementById("logoutBtn").onclick = async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  location.href = "/login";
};

// ---- kill switch ----
document.getElementById("killBtn").onclick = async () => {
  const s = await api.get("/api/summary");
  const turningOn = s.kill_switch !== "on";
  if (turningOn && !confirm("Turn ON kill switch? This blocks new entries and flattens open positions.")) return;
  await api.post("/api/settings", { kill_switch: turningOn });
  toast(turningOn ? "🛑 Kill switch ON — entries blocked" : "✅ Kill switch OFF", turningOn ? "neg" : "pos");
  await refreshAll();
};

// ---- broker / providers / accounts ----
let _accounts = [];
let _enabledBrokers = ["DHAN", "ANGEL", "ZERODHA", "ALICE", "DELTA"];
const _BROKER_LABEL = { DHAN: "Dhan", ANGEL: "Angel One", ZERODHA: "Zerodha", ALICE: "Alice Blue", DELTA: "Delta Exchange" };
function buildAcctBrokerOptions() {
  const sel = document.getElementById("newAcctBroker");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = _enabledBrokers.map((b) => `<option value="${b}">${_BROKER_LABEL[b] || b}</option>`).join("");
  if (_enabledBrokers.includes(cur)) sel.value = cur;
}
function provLabel(value) {
  if (value === "DEMO") return "🧪 Demo (simulated)";
  const a = _accounts.find((x) => String(x.id) === String(value));
  return a ? a.label : "—";
}
function fillProviderSelect(id, value) {
  const sel = document.getElementById(id);
  const demoOk = window._me && window._me.demo_allowed;
  // Demo broker is for administrators only (anti-abuse).
  let html = demoOk ? `<option value="DEMO">🧪 Demo (simulated)</option>` : `<option value="">— select broker —</option>`;
  html += _accounts.map((a) => `<option value="${a.id}">${a.label}${a.connected ? " ✓" : ""}</option>`).join("");
  sel.innerHTML = html;
  sel.value = value || (demoOk ? "DEMO" : "");
}

// Demo price controls
document.querySelectorAll("[data-dir]").forEach((b) => {
  b.onclick = async () => { await api.post("/api/demo/direction", { direction: b.dataset.dir }); await refreshSummary(); };
});
document.getElementById("demoReset").onclick = async () => { await api.post("/api/demo/reset"); await refreshAll(); };

function renderAccounts() {
  const box = document.getElementById("accountsList");
  if (!_accounts.length) { box.innerHTML = `<div class="muted" style="margin-bottom:8px;">No accounts yet — add one below.</div>`; return; }
  // Only an admin may REMOVE a broker account (anti-abuse: stops a user freeing up a
  // broker login to move it to another account). Normal users see no delete button.
  const isAdmin = !!(window._me && window._me.is_admin);
  box.innerHTML = _accounts.map((a) => `
    <div class="acct-row">
      <span class="acct-name">${a.label}</span>
      <span class="pill ${a.connected ? "pill-ok" : "pill-off"}">${a.connected ? (a.token_hours_left != null ? `Connected · ~${a.token_hours_left}h` : "Connected") : "Not connected"}</span>
      <span style="flex:1;"></span>
      <button class="btn btn-sm" data-login-acc="${a.id}" data-broker="${a.broker}">🔐 Login</button>
      <button class="btn btn-sm" data-edit-acc="${a.id}">Edit</button>
      ${isAdmin ? `<button class="btn btn-sm" data-del-acc="${a.id}">✕</button>` : ""}
    </div>`).join("");
  if (!isAdmin) box.innerHTML += `<div class="muted" style="font-size:11px; margin-top:4px;">To remove a broker account, contact an admin.</div>`;
  box.querySelectorAll("[data-login-acc]").forEach((b) => b.onclick = () => loginAccount(b.dataset.loginAcc, b.dataset.broker));
  box.querySelectorAll("[data-edit-acc]").forEach((b) => b.onclick = () => editAccount(b.dataset.editAcc));
  box.querySelectorAll("[data-del-acc]").forEach((b) => b.onclick = async () => {
    if (confirm("Delete this account?")) { await api.del("/api/accounts/" + b.dataset.delAcc); await refreshBroker(); }
  });
}

async function loginAccount(id, broker) {
  const msg = document.getElementById("brokerMsg");
  try {
    if (broker === "DHAN") {
      const r = await api.get("/api/dhan/login?account_id=" + id);
      window.location.href = r.login_url;        // redirect to Dhan
    } else if (broker === "ZERODHA") {
      const r = await api.get("/api/zerodha/login?account_id=" + id);
      window.location.href = r.login_url;        // redirect to Kite
    } else if (broker === "ALICE") {
      await api.post("/api/aliceblue/login", { account_id: Number(id) });
      msg.textContent = "✅ Logged in to Alice Blue."; msg.className = "msg pos";
      await refreshBroker();
    } else if (broker === "DELTA") {
      await api.post("/api/delta/login", { account_id: Number(id) });
      msg.textContent = "✅ Connected to Delta Exchange."; msg.className = "msg pos";
      await refreshBroker();
    } else {
      await api.post("/api/angel/login", { account_id: Number(id) });
      msg.textContent = "✅ Logged in to Angel One."; msg.className = "msg pos";
      await refreshBroker();
    }
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
}

function buildPnlFilter() {
  const sel = document.getElementById("pnlFilter");
  const cur = pnlFilter;
  let html = `<option value="ALL">All Accounts</option><option value="0">Demo / Paper</option>`;
  html += _accounts.map((a) => `<option value="${a.id}">${a.label}</option>`).join("");
  sel.innerHTML = html;
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "ALL";
  pnlFilter = sel.value;
}

async function refreshBroker() {
  const b = await api.get("/api/broker");
  _accounts = b.accounts || [];
  _enabledBrokers = b.enabled_brokers || ["DHAN", "ANGEL", "ZERODHA", "ALICE", "DELTA"];
  buildAcctBrokerOptions();
  fillProviderSelect("dataProvider", b.data_provider || "");
  fillProviderSelect("tradeProvider", b.trade_provider || "");
  buildPnlFilter();
  renderAccounts();
  // Demo controls + Demo provider are admin-only.
  document.getElementById("demoControls").style.display =
    (b.demo_allowed && b.data_provider === "DEMO") ? "flex" : "none";
  document.getElementById("providerDesc").textContent =
    `Data from ${provLabel(b.data_provider)}, orders to ${provLabel(b.trade_provider)}.` +
    (b.data_provider !== b.trade_provider ? " Symbols are auto-translated between brokers." : "");
  document.getElementById("redirectUrl").textContent = b.redirect_url || "—";
  document.getElementById("postbackUrl").textContent = b.postback_url || "—";
  document.getElementById("angelRedirectUrl").textContent = b.angel_redirect_url || "—";
  document.getElementById("angelPostbackUrl").textContent = b.angel_postback_url || "—";
  document.getElementById("zerodhaRedirectUrl").textContent = b.zerodha_redirect_url || "—";
  document.getElementById("zerodhaPostbackUrl").textContent = b.zerodha_postback_url || "—";
  const aliceP = document.getElementById("alicebluePostbackUrl");
  if (aliceP) aliceP.textContent = b.aliceblue_postback_url || "—";
  document.getElementById("staticIp").textContent = b.static_ip || "detecting…";
  const st = document.getElementById("brokerState");
  const dot = (ok) => (ok ? "🟢" : "🔴");
  st.innerHTML = `${dot(b.data_connected)} Data: ${provLabel(b.data_provider)} &nbsp;|&nbsp; ${dot(b.trade_connected)} Trading: ${provLabel(b.trade_provider)}`;
  st.className = "pill " + (b.data_connected && b.trade_connected ? "pill-ok" : "pill-off");
  fxApplyBrokerState(b);     // show/hide the Forex tab + its provider banner
}

// provider dropdowns (Demo is all-or-nothing)
["dataProvider", "tradeProvider"].forEach((id) => {
  document.getElementById(id).onchange = async () => {
    let dp = document.getElementById("dataProvider").value;
    let tp = document.getElementById("tradeProvider").value;
    if (id === "dataProvider") { if (dp === "DEMO") tp = "DEMO"; else if (tp === "DEMO") tp = dp; }
    else { if (tp === "DEMO") dp = "DEMO"; else if (dp === "DEMO") dp = tp; }
    const msg = document.getElementById("brokerMsg");
    try {
      await api.post("/api/providers", { data_provider: dp, trade_provider: tp });
      msg.textContent = ""; toast("✅ Provider updated", "pos"); await refreshBroker();
    } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; await refreshBroker(); }
  };
});

// add / edit account form
const BROKER_NAME = { DHAN: "Dhan", ANGEL: "Angel One", ZERODHA: "Zerodha", ALICE: "Alice Blue", DELTA: "Delta Exchange" };
function showAcctForm(broker, acc) {
  document.getElementById("acctForm").style.display = "block";
  document.getElementById("acctBroker").value = broker;
  document.getElementById("acctId").value = acc ? acc.id : "";
  document.getElementById("acctFormTitle").textContent = (acc ? "Edit " : "New ") + (BROKER_NAME[broker] || broker) + " account";
  document.querySelectorAll(".dhan-f").forEach((e) => e.style.display = broker === "DHAN" ? "" : "none");
  document.querySelectorAll(".api-f").forEach((e) => e.style.display = (broker === "ANGEL" || broker === "ZERODHA" || broker === "ALICE" || broker === "DELTA") ? "" : "none");
  document.querySelectorAll(".angel-f").forEach((e) => e.style.display = broker === "ANGEL" ? "" : "none");
  document.querySelectorAll(".secret-f").forEach((e) => e.style.display = (broker === "ZERODHA" || broker === "DELTA") ? "" : "none");
  // Client / User ID is required for EVERY broker (so each broker login stays tied
  // to one user and can't be shared across accounts).
  document.getElementById("acctClientId").closest("label").style.display = "";
  ["acctClientId", "acctAppId", "acctAppSecret", "acctApiKey", "acctPin", "acctTotp", "acctZSecret"].forEach((i) => document.getElementById(i).value = "");
  document.getElementById("acctClientId").value = acc ? acc.client_id : "";
  if (acc && acc.has_secret) {
    const f = broker === "DHAN" ? "acctAppSecret" : "acctApiKey";
    document.getElementById(f).placeholder = "•••••• saved (blank = keep)";
  }
}
document.getElementById("addAcctBtn").onclick = () => showAcctForm(document.getElementById("newAcctBroker").value, null);
function editAccount(id) { const a = _accounts.find((x) => String(x.id) === String(id)); if (a) showAcctForm(a.broker, a); }
document.getElementById("acctCancelBtn").onclick = () => document.getElementById("acctForm").style.display = "none";
document.getElementById("acctSaveBtn").onclick = async () => {
  const broker = document.getElementById("acctBroker").value;
  const msg = document.getElementById("brokerMsg");
  const clientId = document.getElementById("acctClientId").value.trim();
  if (!clientId) {                       // required for every broker (anti-abuse tracking)
    msg.textContent = "❌ Client / User ID is required for every broker."; msg.className = "msg neg"; return;
  }
  const payload = { broker, id: document.getElementById("acctId").value || undefined, client_id: clientId };
  if (broker === "DHAN") {
    payload.app_id = document.getElementById("acctAppId").value;
    payload.app_secret = document.getElementById("acctAppSecret").value;
  } else if (broker === "ZERODHA" || broker === "DELTA") {
    payload.api_key = document.getElementById("acctApiKey").value;
    payload.api_secret = document.getElementById("acctZSecret").value;
  } else if (broker === "ALICE") {
    payload.api_key = document.getElementById("acctApiKey").value;
  } else {
    payload.api_key = document.getElementById("acctApiKey").value;
    payload.pin = document.getElementById("acctPin").value;
    payload.totp_secret = document.getElementById("acctTotp").value;
  }
  try {
    await api.post("/api/accounts", payload);
    document.getElementById("acctForm").style.display = "none";
    msg.textContent = "✅ Account saved."; msg.className = "msg pos";
    await refreshBroker();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

// message after returning from a broker login redirect
(function () {
  const p = new URLSearchParams(location.search);
  const m = document.getElementById("brokerMsg");
  if (p.get("login") === "ok" && m) { m.textContent = "✅ Logged in."; m.className = "msg pos"; }
  else if (p.get("login") === "failed" && m) { m.textContent = "❌ Login failed — check the account details and try again."; m.className = "msg neg"; }
})();

// ---- modify SL/Targets modal (direct prices) ----
const modifyModal = document.getElementById("modifyModal");
let modifyId = null, modLotSize = 1, modRemainingLots = 1;

function modDistribute() {
  const rows = [...document.querySelectorAll("#modTargetRows .trow")];
  const N = modRemainingLots, M = rows.length;
  if (!M) return;
  const base = Math.floor(N / M), rem = N % M;
  rows.forEach((r, i) => {
    const lots = base + (i < rem ? 1 : 0);
    r.querySelector(".mtl").value = lots * modLotSize;     // quantity
    r.querySelector(".mtlots").textContent = `(${lots} lot${lots > 1 ? "s" : ""})`;
  });
}
function modAddTargetRow(price = "") {
  if ([...document.querySelectorAll("#modTargetRows .trow")].length >= modRemainingLots) {
    alert(`At most ${modRemainingLots} target(s) — one per remaining lot.`);
    return;
  }
  const div = document.createElement("div");
  div.className = "trow";
  div.innerHTML = `<input class="mtp" type="number" step="0.05" placeholder="price" value="${price}" />
    <input class="mtl" type="number" readonly title="quantity (auto-distributed)" style="width:80px;" />
    <span class="muted" style="font-size:11px;">qty <span class="mtlots"></span></span>
    <button type="button" class="step trm">×</button>`;
  div.querySelector(".trm").onclick = () => { div.remove(); modDistribute(); };
  document.getElementById("modTargetRows").appendChild(div);
  modDistribute();
}
document.getElementById("modAddTarget").onclick = () => modAddTargetRow();

function openModify(id) {
  const t = tradesById[id];
  if (!t) return;
  modifyId = id;
  modLotSize = t.lot_size || 1;
  const remainingQty = t.quantity - (t.exited_qty || 0);
  modRemainingLots = Math.max(1, Math.floor(remainingQty / modLotSize));
  document.getElementById("modSl").value = t.stop_loss || 0;
  document.getElementById("modTrail").value = t.trail_sl || 0;
  document.getElementById("modTrailMode").value = t.trail_mode || "CONTINUE";
  const rows = document.getElementById("modTargetRows");
  rows.innerHTML = "";
  let prices = [];
  if (t.targets_json && t.targets_json !== "[]") {
    // Only the targets that HAVEN'T fired yet — these are the ones that still
    // apply to the current open position. Already-hit targets are done.
    try { prices = JSON.parse(t.targets_json).filter((tg) => !tg.hit).map((tg) => tg.price).filter((p) => p > 0); }
    catch (e) { /* ignore */ }
  } else if (t.target) {
    prices = [t.target];
  }
  prices.slice(0, modRemainingLots).forEach((pr) => modAddTargetRow(pr));
  modDistribute();
  document.getElementById("modMaxProfit").value = t.max_profit_amt || 0;
  document.getElementById("modMaxLoss").value = t.max_loss_amt || 0;
  setVal("modLockStep", t.lock_step || 0);
  setVal("modLockAmount", t.lock_amount || 0);
  document.getElementById("modMsg").textContent = "";
  document.getElementById("modifyInfo").innerHTML =
    `<b>${t.symbol}</b> — entry ₹${t.entry_fill_price} · LTP ₹${t.last_price} · remaining ${remainingQty} qty (${modRemainingLots} lots, lot size ${modLotSize})`;
  modifyModal.style.display = "flex";
}
document.getElementById("modCancel").onclick = () => { modifyModal.style.display = "none"; };
document.getElementById("modSave").onclick = async () => {
  const targets = [...document.querySelectorAll("#modTargetRows .trow")].map((r) => ({
    price: parseFloat(r.querySelector(".mtp").value) || 0,
    qty: parseInt(r.querySelector(".mtl").value) || 0,     // .mtl already holds quantity
  })).filter((x) => x.price > 0 && x.qty > 0);
  try {
    await api.post(`/api/trades/${modifyId}/modify`, {
      stop_loss: parseFloat(document.getElementById("modSl").value) || 0,
      trail_sl: parseFloat(document.getElementById("modTrail").value) || 0,
      trail_mode: document.getElementById("modTrailMode").value,
      targets,
      max_profit_amt: parseFloat(document.getElementById("modMaxProfit").value) || 0,
      max_loss_amt: parseFloat(document.getElementById("modMaxLoss").value) || 0,
      lock_step: numVal("modLockStep"),
      lock_amount: numVal("modLockAmount"),
    });
    modifyModal.style.display = "none";
    toast(`✅ Trade #${modifyId} updated`, "pos");
    await refreshAll();
  } catch (e) {
    const mm = document.getElementById("modMsg");
    mm.textContent = "❌ " + e.message; mm.className = "msg neg";
  }
};

// ---- refresh symbols button ----
document.getElementById("refreshSymbolsBtn").onclick = async () => {
  const ss = document.getElementById("symbolsState");
  ss.textContent = "Refreshing…"; ss.className = "pill pill-off";
  await api.post("/api/instruments/refresh");
};

// ---- symbol presets ----
let _presets = {};
async function loadPresets() {
  try {
    const rows = await api.get("/api/presets");
    _presets = {};
    rows.forEach((p) => { _presets[`${p.kind || "OPTION"}|${String(p.symbol).toUpperCase()}`] = p; });
    renderPresets(rows);
  } catch (e) { /* ignore */ }
}
function applyPreset(symbol, kind) {
  if (!symbol) return;
  kind = kind || currentSeg;       // OPTION / FUTURES / EQUITY
  const norm = String(symbol).trim().toUpperCase();
  // Match this instrument type only; try exact underlying then its first word.
  const p = _presets[`${kind}|${norm}`] || _presets[`${kind}|${norm.split(/\s+/)[0]}`];
  if (!p) return;
  if (p.lots && p.lots > 0) { lotsInput.value = p.lots; updateQty(); }
  form.sl_points.value = p.sl_points || 0;
  form.trail_sl.value = p.trail_sl || 0;
  if (form.trail_mode) form.trail_mode.value = p.trail_mode || "CONTINUE";
  const rowsBox = document.getElementById("targetRows");
  if (p.targets && p.targets.length > 1) {
    if (p.targets.length > (parseInt(lotsInput.value) || 1)) { lotsInput.value = p.targets.length; updateQty(); }
    multiToggle.checked = true; multiWrap.style.display = "block"; targetField.style.display = "none";
    rowsBox.innerHTML = "";
    p.targets.forEach((pts) => addTargetRow(pts));
    form.target_points.value = 0;
  } else {
    multiToggle.checked = false; multiWrap.style.display = "none"; targetField.style.display = "";
    rowsBox.innerHTML = "";
    form.target_points.value = (p.targets && p.targets.length === 1) ? p.targets[0] : (p.target_points || 0);
  }
  if (form.max_profit_amt) form.max_profit_amt.value = p.max_profit_amt || 0;
  if (form.max_loss_amt) form.max_loss_amt.value = p.max_loss_amt || 0;
  if (form.lock_step) form.lock_step.value = p.lock_step || 0;
  if (form.lock_amount) form.lock_amount.value = p.lock_amount || 0;
  const msg = document.getElementById("formMsg");
  msg.textContent = `↺ Preset applied for ${p.symbol}.`; msg.className = "msg pos";
}
function renderPresets(rows) {
  const box = document.getElementById("presetList");
  if (!box) return;
  if (!rows.length) { box.innerHTML = `<p class="muted" style="font-size:12px;">No presets saved yet.</p>`; return; }
  const kindLabel = { OPTION: "Option", FUTURES: "Futures", EQUITY: "Stock" };
  box.innerHTML = `<table class="data"><thead><tr><th>Symbol</th><th>Type</th><th>Lots</th><th>SL</th><th>Trail</th><th>Targets</th><th>Max P / L</th><th>Lock</th><th></th></tr></thead><tbody>`
    + rows.map((p) => {
      const tg = (p.targets && p.targets.length) ? p.targets.join(", ") + "p" : (p.target_points ? p.target_points + "p" : "-");
      const lock = (p.lock_step && p.lock_amount) ? `₹${p.lock_step}→₹${p.lock_amount}` : "-";
      return `<tr><td><b>${p.symbol}</b></td><td>${kindLabel[p.kind] || p.kind || "Option"}</td><td>${p.lots || "-"}</td><td>${p.sl_points || "-"}</td><td>${p.trail_sl || "-"}</td><td>${tg}</td>
        <td>${p.max_profit_amt || "-"} / ${p.max_loss_amt || "-"}</td><td>${lock}</td>
        <td><button class="btn btn-sm" data-pr-edit="${p.id}">Edit</button> <button class="btn btn-sm" data-pr-del="${p.id}">✕</button></td></tr>`;
    }).join("") + `</tbody></table>`;
  box.querySelectorAll("[data-pr-edit]").forEach((b) => b.onclick = () => editPreset(rows.find((x) => String(x.id) === b.dataset.prEdit)));
  box.querySelectorAll("[data-pr-del]").forEach((b) => b.onclick = async () => {
    if (confirm("Delete this preset?")) { await api.del("/api/presets/" + b.dataset.prDel); await loadPresets(); }
  });
}
// ---- preset multi-target widget (mirrors New Trade: cumulative points + auto
//      lot-split driven by the preset's Lots field) ----
const prLotsInput = document.getElementById("prLots");
const prMulti = document.getElementById("prMulti");
const prSingleWrap = document.getElementById("prSingleWrap");
const prMultiWrap = document.getElementById("prMultiWrap");
function prTargetCount() { return document.querySelectorAll("#prTargetRows .ptrow").length; }
function prUpdateCumulative() {
  let running = 0;
  document.querySelectorAll("#prTargetRows .ptrow").forEach((r) => {
    running += parseFloat(r.querySelector(".ptp").value) || 0;
    const c = r.querySelector(".ptcum"); if (c) c.textContent = running > 0 ? `→ +${running} pts` : "";
  });
}
function prDistribute() {
  const rows = [...document.querySelectorAll("#prTargetRows .ptrow")];
  const N = parseInt(prLotsInput.value) || 1, M = rows.length;
  if (!M) return;
  const base = Math.floor(N / M), rem = N % M;
  rows.forEach((r, i) => { const lots = base + (i < rem ? 1 : 0); r.querySelector(".ptl").textContent = `${lots} lot${lots > 1 ? "s" : ""}`; });
  prUpdateCumulative();
}
function prTrimDistribute() {
  const N = parseInt(prLotsInput.value) || 1;
  const rows = [...document.querySelectorAll("#prTargetRows .ptrow")];
  while (rows.length > N) rows.pop().remove();
  prDistribute();
}
function prAddTargetRow(points = "") {
  const N = parseInt(prLotsInput.value) || 1;
  if (prTargetCount() >= N) { alert(`At most ${N} target(s) — one per lot. Increase Lots to add more.`); return; }
  const div = document.createElement("div");
  div.className = "ptrow trow";
  div.innerHTML = `<input class="ptp" type="number" step="0.05" placeholder="points" value="${points}" />
    <span class="muted" style="font-size:11px;">qty <span class="ptl"></span> <span class="ptcum" style="color:var(--accent);"></span></span>
    <button type="button" class="step ptrm">×</button>`;
  div.querySelector(".ptp").addEventListener("input", prUpdateCumulative);
  div.querySelector(".ptrm").onclick = () => { div.remove(); prDistribute(); };
  document.getElementById("prTargetRows").appendChild(div);
  prDistribute();
}
prMulti.onchange = () => {
  const on = prMulti.checked;
  prMultiWrap.style.display = on ? "block" : "none";
  prSingleWrap.style.display = on ? "none" : "block";
  if (on && !prTargetCount()) prAddTargetRow();
};
document.getElementById("prAddTarget").onclick = () => prAddTargetRow();
prLotsInput.addEventListener("input", () => { if (prMulti.checked) prTrimDistribute(); });
document.getElementById("prGenLegs").onclick = () => {
  const total = parseFloat(document.getElementById("prAutoTotal").value) || 0;
  const legs = parseInt(document.getElementById("prAutoLegs").value) || 0;
  const N = parseInt(prLotsInput.value) || 1;
  if (total <= 0 || legs < 1) { alert("Enter total target points and number of legs."); return; }
  if (legs > N) { alert(`Legs (${legs}) cannot exceed Lots (${N}).`); return; }
  document.getElementById("prTargetRows").innerHTML = "";
  const per = Math.round((total / legs) * 100) / 100;
  for (let i = 0; i < legs; i++) prAddTargetRow(per);
};

// preset instrument-type selector (Option / Futures / Stock)
let prKind = "OPTION";
function setPrKind(k) {
  prKind = k;
  document.querySelectorAll("[data-prkind]").forEach((x) => x.classList.toggle("active", x.dataset.prkind === k));
}
document.querySelectorAll("[data-prkind]").forEach((b) => { b.onclick = () => setPrKind(b.dataset.prkind); });

function editPreset(p) {
  if (!p) return;
  setPrKind(p.kind || "OPTION");
  setVal("prSymbol", p.symbol); setVal("prLots", p.lots || 0);
  setVal("prSl", p.sl_points || 0); setVal("prTrail", p.trail_sl || 0);
  document.getElementById("prTrailMode").value = p.trail_mode || "CONTINUE";
  setVal("prMaxProfit", p.max_profit_amt || 0); setVal("prMaxLoss", p.max_loss_amt || 0);
  setVal("prLockStep", p.lock_step || 0); setVal("prLockAmount", p.lock_amount || 0);
  document.getElementById("prTargetRows").innerHTML = "";
  if (p.targets && p.targets.length > 1) {
    if ((p.lots || 0) < p.targets.length) setVal("prLots", p.targets.length);
    prMulti.checked = true; prMultiWrap.style.display = "block"; prSingleWrap.style.display = "none";
    setVal("prTarget", 0);
    p.targets.forEach((pts) => prAddTargetRow(pts));
  } else {
    prMulti.checked = false; prMultiWrap.style.display = "none"; prSingleWrap.style.display = "block";
    setVal("prTarget", (p.targets && p.targets.length === 1) ? p.targets[0] : (p.target_points || 0));
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function clearPresetForm() {
  ["prSl", "prTrail", "prTarget", "prMaxProfit", "prMaxLoss", "prLockStep", "prLockAmount", "prLots"]
    .forEach((id) => setVal(id, 0));
  setVal("prSymbol", ""); setPrKind("OPTION");
  document.getElementById("prTrailMode").value = "CONTINUE";
  prMulti.checked = false; prMultiWrap.style.display = "none"; prSingleWrap.style.display = "block";
  document.getElementById("prTargetRows").innerHTML = "";
}
document.getElementById("clearPresetBtn").onclick = clearPresetForm;
document.getElementById("savePresetBtn").onclick = async () => {
  const msg = document.getElementById("presetMsg");
  let targets = [], target_points = 0;
  if (prMulti.checked) {
    targets = [...document.querySelectorAll("#prTargetRows .ptrow")]
      .map((r) => parseFloat(r.querySelector(".ptp").value) || 0).filter((x) => x > 0);
  } else {
    target_points = numVal("prTarget");
  }
  try {
    await api.post("/api/presets", {
      symbol: document.getElementById("prSymbol").value,
      kind: prKind,
      lots: parseInt(document.getElementById("prLots").value) || 0,
      sl_points: numVal("prSl"), trail_sl: numVal("prTrail"),
      trail_mode: document.getElementById("prTrailMode").value,
      target_points, targets,
      max_profit_amt: numVal("prMaxProfit"), max_loss_amt: numVal("prMaxLoss"),
      lock_step: numVal("prLockStep"), lock_amount: numVal("prLockAmount"),
    });
    msg.textContent = "✅ Preset saved."; msg.className = "msg pos";
    toast("✅ Preset saved", "pos");
    clearPresetForm(); await loadPresets();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

// ---- preset symbol search (same sources as the New Trade picker) ----
const prSymbol = document.getElementById("prSymbol");
const prResults = document.getElementById("prResults");
let prTimer = null;
prSymbol.addEventListener("input", () => {
  clearTimeout(prTimer);
  const q = prSymbol.value.trim();
  if (q.length < 2) { prResults.classList.remove("show"); return; }
  prTimer = setTimeout(async () => {
    let opt = [], eq = [];
    try { opt = await api.get("/api/underlyings/search?kind=OPTION&q=" + encodeURIComponent(q)); } catch {}
    try { eq = await api.get("/api/equities/search?q=" + encodeURIComponent(q)); } catch {}
    const names = [];
    opt.forEach((r) => names.push({ name: r.underlying, meta: (r.exchange || "") + " · options/futures" }));
    eq.forEach((r) => { if (!names.some((n) => n.name === r.symbol)) names.push({ name: r.symbol, meta: r.exchange_segment + " · equity" }); });
    if (!names.length) { prResults.innerHTML = `<div class="item"><div class="meta">No matches.</div></div>`; prResults.classList.add("show"); return; }
    prResults.innerHTML = names.slice(0, 20).map((n, i) => `<div class="item" data-i="${i}">
      <div class="sym">${n.name}</div><div class="meta">${n.meta}</div></div>`).join("");
    prResults.querySelectorAll(".item").forEach((el) => {
      const n = names[el.dataset.i]; if (n) el.onclick = () => { prSymbol.value = n.name; prResults.classList.remove("show"); };
    });
    prResults.classList.add("show");
  }, 250);
});
document.addEventListener("click", (e) => {
  if (!prSymbol.contains(e.target) && !prResults.contains(e.target)) prResults.classList.remove("show");
});

// ---- global settings (daily limits + account profit-lock) ----
async function loadSettings() {
  try {
    const s = await api.get("/api/settings");
    setVal("dailyMaxProfit", s.daily_max_profit || 0);
    setVal("dailyMaxLoss", s.daily_max_loss || 0);
    setVal("globalLockStep", s.global_lock_step || 0);
    setVal("globalLockAmount", s.global_lock_amount || 0);
    const sq = s.auto_squareoff_time || "15:15:00";
    const off = String(sq).toLowerCase() === "off";
    document.getElementById("autoSqoffOff").checked = off;
    document.getElementById("autoSqoffTime").value = off ? "15:15:00" : sq;
    document.getElementById("autoSqoffTime").disabled = off;
  } catch (e) { /* ignore */ }
}
document.getElementById("autoSqoffOff").onchange = (e) => {
  document.getElementById("autoSqoffTime").disabled = e.target.checked;
};
document.getElementById("saveSettingsBtn").onclick = async () => {
  const msg = document.getElementById("settingsMsg");
  try {
    await api.post("/api/settings", {
      daily_max_profit: numVal("dailyMaxProfit"),
      daily_max_loss: numVal("dailyMaxLoss"),
      global_lock_step: numVal("globalLockStep"),
      global_lock_amount: numVal("globalLockAmount"),
      auto_squareoff_time: document.getElementById("autoSqoffOff").checked
        ? "off" : (document.getElementById("autoSqoffTime").value || "15:15:00"),
    });
    msg.textContent = "✅ Settings saved."; msg.className = "msg pos";
    toast("✅ Settings saved", "pos");
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

// ---- watchlist (one-click load into the New Trade form) ----
let _watchlist = [];
async function loadWatchlist() {
  try { renderWatchlist(await api.get("/api/watchlist")); } catch (e) { /* ignore */ }
}
function renderWatchlist(all) {
  _watchlist = all || [];
  // Forex/crypto watchlist items live on their own tab; keep them out of the India list.
  fxRenderWatch(_watchlist.filter(isForexItem));
  const items = _watchlist.filter((w) => !isForexItem(w));
  const box = document.getElementById("watchlistChips");
  const empty = document.getElementById("wlEmpty");
  if (!items.length) { box.innerHTML = ""; empty.style.display = ""; return; }
  empty.style.display = "none";
  const tagOf = (w) => {
    if (w.security_id) return "";   // specific contract — name already says it all
    const t = { OPTION: "chain", FUTURES: "fut", EQUITY: "eq" }[w.instrument_type] || "";
    return t ? ` <span class="wl-tag">${t}</span>` : "";
  };
  box.innerHTML = items.map((w) =>
    `<span class="wl-chip" data-wl="${w.id}" title="${w.security_id ? w.exchange_segment + ' · id ' + w.security_id : 'opens the ' + w.instrument_type.toLowerCase() + ' picker'}">${w.symbol}${tagOf(w)}<span class="wl-x" data-wlx="${w.id}">×</span></span>`).join("");
  box.querySelectorAll(".wl-chip").forEach((el) => {
    el.onclick = (e) => { if (e.target.classList.contains("wl-x")) return;
      const w = _watchlist.find((x) => String(x.id) === el.dataset.wl); if (w) wlLoad(w); };
  });
  box.querySelectorAll(".wl-x").forEach((x) => {
    x.onclick = async (e) => { e.stopPropagation(); await api.del("/api/watchlist/" + x.dataset.wlx); await loadWatchlist(); };
  });
}
function wlLoad(item) {
  const seg = item.instrument_type === "FUTURES" ? "FUTURES"
            : (item.instrument_type === "OPTION" ? "OPTION" : "EQUITY");
  currentSeg = seg;
  document.querySelectorAll("[data-seg]").forEach((x) => x.classList.toggle("active", x.dataset.seg === seg));
  pickerHint.textContent = HINTS[seg];
  document.querySelector('.tab[data-tab="new"]').click();   // ensure the New Trade tab is open
  const acc = document.querySelector(".watchlist-acc"); if (acc) acc.open = false;  // auto-close after selecting
  if (item.security_id) {                 // a specific contract -> load it directly
    currentUnderlying = item.underlying || null;
    ulSearch.value = item.underlying || item.symbol;
    pickContract({ symbol: item.symbol, security_id: item.security_id,
      exchange_segment: item.exchange_segment, instrument_type: item.instrument_type, lot_size: item.lot_size });
  } else {                                // a whole symbol -> open its chain / futures list
    selectUnderlying(item.underlying || item.symbol);
  }
}
document.getElementById("addSymbolBtn").onclick = async () => {
  const msg = document.getElementById("formMsg");
  const sym = (currentSeg === "EQUITY") ? form.symbol.value : currentUnderlying;
  if (!sym) { msg.textContent = "❌ Search and select a symbol first, then add it."; msg.className = "msg neg"; return; }
  try {
    // Equity has no chain, so a "symbol" there is just the stock contract itself.
    if (currentSeg === "EQUITY") {
      await api.post("/api/watchlist", { symbol: form.symbol.value, security_id: form.security_id.value,
        exchange_segment: form.exchange_segment.value, instrument_type: form.instrument_type.value,
        underlying: form.symbol.value, lot_size: currentLotSize });
    } else {
      await api.post("/api/watchlist", { symbol: sym, security_id: "", exchange_segment: "",
        instrument_type: currentSeg, underlying: sym, lot_size: 1 });
    }
    msg.textContent = `⭐ Added ${sym} to watchlist.`; msg.className = "msg pos";
    toast(`⭐ ${sym} added to watchlist`, "pos");
    await loadWatchlist();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};
document.getElementById("addWatchBtn").onclick = async () => {
  const msg = document.getElementById("formMsg");
  if (!form.security_id.value) { msg.textContent = "❌ Pick a strike / contract first, then add it."; msg.className = "msg neg"; return; }
  try {
    await api.post("/api/watchlist", {
      symbol: form.symbol.value, security_id: form.security_id.value,
      exchange_segment: form.exchange_segment.value, instrument_type: form.instrument_type.value,
      underlying: (currentSeg === "EQUITY" ? form.symbol.value : (currentUnderlying || "")),
      lot_size: currentLotSize,
    });
    msg.textContent = "⭐ Added to watchlist."; msg.className = "msg pos";
    toast("⭐ Added to watchlist", "pos");
    await loadWatchlist();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

// ---- account / onboarding ----
async function loadMe() {
  let me; try { me = await api.get("/api/me"); } catch (e) { return; }
  window._me = me;
  if (me.plan_expired) { showExpired(); return; }
  // header identity
  const idEl = document.getElementById("userId");
  if (idEl) idEl.innerHTML = `👤 <b>${me.email}</b>` + (me.is_admin ? ' <span class="rbadge r-TARGET">ADMIN</span>' : "");
  const plEl = document.getElementById("planPill");
  if (plEl && me.plan_expiry) {
    const days = Math.max(0, Math.ceil((new Date(me.plan_expiry) - new Date()) / 86400000));
    plEl.textContent = me.is_admin ? "Admin" : `${me.plan_name} · ${days}d left`;
    plEl.className = "pill " + (days <= 3 && !me.is_admin ? "pill-off" : "pill-ok");
  }
  // admin tab
  if (me.is_admin) document.querySelectorAll(".admin-only").forEach((e) => e.style.display = "");
  // Identity may resolve after the first broker render — re-render so the admin-only
  // delete (✕) button reflects the now-known role.
  if (_accounts && _accounts.length && typeof renderAccounts === "function") renderAccounts();
  // onboarding nudge — lock trading until a broker is connected (non-admins).
  const nudge = document.getElementById("onboardNudge");
  const needBroker = !me.is_admin && !me.broker_connected;
  if (nudge) nudge.style.display = needBroker ? "block" : "none";
  const form = document.getElementById("tradeForm");
  if (form) {
    form.querySelectorAll("input,button,select").forEach((el) => {
      if (el.id !== "addWatchBtn" && el.id !== "addSymbolBtn") el.disabled = needBroker;
    });
  }
}

// ---- refresh loop ----
async function refreshAll() {
  await Promise.all([refreshSummary(), refreshTrades(), refreshLogs(), refreshBroker(), loadMe()]);
}
showTradesSkeleton();   // skeleton rows until the first data arrives
loadHowto();
loadPresets();
loadSettings();
loadWatchlist();
refreshAll();
startStream();          // open the real-time tick stream (SSE)
setInterval(() => { refreshSummary(); refreshTrades(); refreshLogs(); bkPoll(); gpPoll(); }, 2000);

// ---- how-to guide (admin-editable rich HTML, shown on the Broker tab) ----
async function loadHowto() {
  try {
    const d = await api.get("/api/howto");
    const box = document.getElementById("howtoBox");
    if (box) box.innerHTML = d.html || "";
  } catch (e) { /* ignore */ }
}
function esc(s) { return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
// All system times shown in IST.
function _utc(iso) { let s = String(iso || ""); if (s && !/[Z+]/.test(s.slice(10))) s += "Z"; return s; }
function fmtIST(iso) {
  try { return new Date(_utc(iso)).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: true }); }
  catch (e) { return iso || ""; }
}

// ---- WordPress-style rich text editors (How-to + email templates) ----
const getRTE = (id) => { const e = document.getElementById(id); return e ? e.innerHTML : ""; };
const setRTE = (id, html) => { const e = document.getElementById(id); if (e) e.innerHTML = html || ""; };
function initRichEditors() {
  document.querySelectorAll(".rte-toolbar").forEach((bar) => {
    if (bar._init) return; bar._init = true;
    const ed = document.getElementById(bar.dataset.for);
    const b = (label, cmd, val) => `<button type="button" class="rte-b" data-cmd="${cmd}"${val ? ` data-val="${val}"` : ""}>${label}</button>`;
    let html = b("<b>B</b>", "bold") + b("<i>I</i>", "italic") + b("<u>U</u>", "underline")
      + b("H", "formatBlock", "h3") + b("¶", "formatBlock", "p")
      + b("• List", "insertUnorderedList") + b("1. List", "insertOrderedList") + b("🔗", "createLink");
    if (bar.dataset.ph) html += '<span class="rte-sep"></span>'
      + bar.dataset.ph.split(",").map((p) => `<button type="button" class="rte-ph-chip" data-ins="{${p.trim()}}">{${p.trim()}}</button>`).join("");
    bar.innerHTML = html;
    bar.querySelectorAll("[data-cmd]").forEach((btn) => btn.onmousedown = (e) => {
      e.preventDefault(); ed.focus();
      if (btn.dataset.cmd === "createLink") { const u = prompt("Link URL:"); if (u) document.execCommand("createLink", false, u); }
      else document.execCommand(btn.dataset.cmd, false, btn.dataset.val || null);
    });
    bar.querySelectorAll("[data-ins]").forEach((btn) => btn.onmousedown = (e) => {
      e.preventDefault(); ed.focus(); document.execCommand("insertText", false, btn.dataset.ins);
    });
  });
}
initRichEditors();
// Refresh the (admin-editable) how-to each time the Broker tab is opened.
const _bt = document.querySelector('.tab[data-tab="broker"]');
if (_bt) _bt.addEventListener("click", loadHowto);

// ============================ ADMIN PANEL ============================
document.querySelectorAll("[data-asec]").forEach((b) => b.onclick = () => {
  document.querySelectorAll("[data-asec]").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  document.querySelectorAll(".asec").forEach((s) => s.style.display = "none");
  document.getElementById("asec-" + b.dataset.asec).style.display = "";
  if (b.dataset.asec === "users") adminLoadUsers();
  if (b.dataset.asec === "alllogs") adminLoadAllLogs();
  if (b.dataset.asec === "plans") adminLoadPlans();
  if (b.dataset.asec === "saas" || b.dataset.asec === "email") adminLoadSettings();
  if (b.dataset.asec === "email") adminLoadTemplate();
});
// load users when the Admin tab is opened
document.querySelector('.tab[data-tab="admin"]').addEventListener("click", () => adminLoadUsers());

let _adUserTimer = null;
function wireAdminSearch() {
  const s = document.getElementById("adUserSearch");
  if (s && !s._w) { s._w = true; s.oninput = () => { clearTimeout(_adUserTimer); _adUserTimer = setTimeout(adminLoadUsers, 250); }; }
}
async function adminLoadUsers() {
  wireAdminSearch();
  const q = (document.getElementById("adUserSearch") || {}).value || "";
  let rows; try { rows = await api.get("/api/admin/users?q=" + encodeURIComponent(q)); } catch (e) { return; }
  const box = document.getElementById("adminUsers");
  box.innerHTML = `<table class="data"><thead><tr><th>Email</th><th>Role</th><th>Plan</th><th>Expiry</th><th>Status</th><th>Brokers</th><th>Actions</th></tr></thead><tbody>`
    + rows.map((u) => {
      const exp = u.plan_expiry ? new Date(u.plan_expiry).toLocaleDateString("en-IN") : "—";
      const st = u.status === "BLOCKED" ? '<span class="rbadge r-STOPLOSS">BLOCKED</span>'
        : (u.expired ? '<span class="rbadge r-MANUAL">EXPIRED</span>' : '<span class="rbadge r-TARGET">ACTIVE</span>');
      const isAdmin = u.role === "SUPER_ADMIN";
      return `<tr><td><b>${esc(u.email)}</b> ${u.online ? "🟢" : ""}</td><td>${u.role === "SUPER_ADMIN" ? "Admin" : "User"}</td>
        <td>${esc(u.plan_name || "")}</td><td>${exp}</td><td>${st}</td><td>${u.accounts}</td>
        <td>
          <button class="btn btn-sm" data-au-view="${u.id}" data-email="${esc(u.email)}" data-uuid="${u.uuid}">Inspect</button>
          ${isAdmin ? "" : `<button class="btn btn-sm" data-au-plan="${u.id}">+Days</button>
          <button class="btn btn-sm" data-au-block="${u.id}" data-on="${u.status === "BLOCKED" ? 1 : 0}">${u.status === "BLOCKED" ? "Unblock" : "Block"}</button>
          <button class="btn btn-sm" data-au-del="${u.id}">✕</button>`}
        </td></tr>`;
    }).join("") + "</tbody></table>";
  box.querySelectorAll("[data-au-view]").forEach((b) => b.onclick = () => adminInspect(b.dataset.auView, b.dataset.email, b.dataset.uuid));
  box.querySelectorAll("[data-au-block]").forEach((b) => b.onclick = async () => {
    await api.post(`/api/admin/users/${b.dataset.auBlock}/status`, { blocked: b.dataset.on !== "1" });
    toast("User updated", "info"); adminLoadUsers();
  });
  box.querySelectorAll("[data-au-del]").forEach((b) => b.onclick = async () => {
    if (confirm("Delete this user and ALL their data permanently?")) { await api.del("/api/admin/users/" + b.dataset.auDel); toast("User deleted", "info"); adminLoadUsers(); }
  });
  box.querySelectorAll("[data-au-plan]").forEach((b) => b.onclick = async () => {
    const days = parseInt(prompt("Add how many access days?", "30")); if (!days) return;
    await api.post(`/api/admin/users/${b.dataset.auPlan}/plan`, { days, extend: true, plan_name: days + " Days" });
    toast(`Extended by ${days} days`, "pos"); adminLoadUsers();
  });
}
document.getElementById("adCreateUser").onclick = async () => {
  const msg = document.getElementById("adUserMsg");
  try {
    const d = await api.post("/api/admin/users", { email: document.getElementById("adNewEmail").value.trim(),
      days: parseInt(document.getElementById("adNewDays").value) || 30 });
    msg.textContent = "✅ User created. " + (d.set_password_code ? "Set-password code (SMTP off): " + d.set_password_code : "A set-password email was sent.");
    msg.className = "msg pos"; document.getElementById("adNewEmail").value = ""; adminLoadUsers();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

// ---- remote inspector (full broker control on behalf of a user) ----
let _auId = null, _auUuid = "", _auTimer = null;
const AU_NAME = { DHAN: "Dhan", ANGEL: "Angel One", ZERODHA: "Zerodha", ALICE: "Alice Blue", DELTA: "Delta Exchange" };
async function adminInspect(uid, email, uuid) {
  _auId = uid; _auUuid = uuid || "";
  document.getElementById("auTitle").textContent = "🛠️ " + email;
  document.getElementById("adminUserModal").style.display = "flex";
  document.getElementById("auMsg").textContent = "";
  auResetForm();
  const base = location.origin;
  document.getElementById("auRedirect").textContent = base + "/api/dhan/callback  ·  " + base + "/api/zerodha/callback";
  document.getElementById("auHook").textContent = `${base}/api/webhook/${_auUuid}/{dhan|angel|zerodha|aliceblue}`;
  await auRefresh();
  if (_auTimer) clearInterval(_auTimer);
  _auTimer = setInterval(auRefresh, 3000);   // live balance / P&L / accounts
}
async function auRefresh() {
  const uid = _auId; if (!uid) return;
  try {
    const s = await api.get(`/api/admin/users/${uid}/summary`);
    document.getElementById("auPnl").textContent = money(s.pnl.total || 0);
    document.getElementById("auBal").textContent = s.balance != null ? money(s.balance) : "—";
    document.getElementById("auOpen").textContent = `${s.open} / ${s.pending}`;
  } catch (e) {}
  try {
    const ts = await api.get(`/api/admin/users/${uid}/trades`);
    document.getElementById("auTrades").innerHTML = ts.length ? (`<table class="data"><thead><tr><th>Sym</th><th>Side</th><th>Mode</th><th>Status</th><th>P&L</th></tr></thead><tbody>`
      + ts.map((t) => `<tr><td>${esc(t.symbol)}</td><td>${t.side}</td><td>${t.mode}</td><td>${t.status}${t.exit_reason ? " · " + t.exit_reason : ""}</td><td class="${cls(t.pnl)}">${money(t.pnl)}</td></tr>`).join("") + "</tbody></table>")
      : "<div class='muted'>No trades.</div>";
  } catch (e) {}
  try {
    _auAccts = await api.get(`/api/admin/users/${uid}/accounts`);
    document.getElementById("auAccounts").innerHTML = _auAccts.length ? _auAccts.map((a) =>
      `<div class="acct-row"><span class="acct-name">${esc(a.label)}</span>
        <span class="pill ${a.connected ? "pill-ok" : "pill-off"}">${a.connected ? (a.token_hours_left != null ? `Connected ~${a.token_hours_left}h` : "Connected") : "Not connected"}</span>
        <span style="flex:1;"></span>
        <button class="btn btn-sm" data-au-login="${a.id}" data-broker="${a.broker}">Login</button>
        <button class="btn btn-sm" data-au-edit="${a.id}">Edit</button>
        <button class="btn btn-sm" data-au-rm="${a.id}">✕</button></div>`).join("")
      : "<div class='muted'>No broker accounts yet — add one below.</div>";
    const box = document.getElementById("auAccounts");
    box.querySelectorAll("[data-au-login]").forEach((b) => b.onclick = () => auLogin(b.dataset.auLogin, b.dataset.broker));
    box.querySelectorAll("[data-au-edit]").forEach((b) => b.onclick = () => auEdit(b.dataset.auEdit));
    box.querySelectorAll("[data-au-rm]").forEach((b) => b.onclick = async () => {
      if (confirm("Remove this broker account?")) { await api.del(`/api/admin/users/${uid}/accounts/${b.dataset.auRm}`); toast("Broker removed", "info"); auRefresh(); }
    });
  } catch (e) {}
}
let _auAccts = [];
// show the right credential fields for the chosen broker
function auApplyBrokerFields() {
  const b = document.getElementById("auBroker").value;
  // Use INLINE display: the `.modal .row label` rule (specificity 0,0,2,1) overrides
  // the `.au-f` / `.au-f.show` classes, so class-toggling can't hide these fields.
  const show = (sel, on) => document.querySelectorAll(sel).forEach((e) => e.style.display = on ? "" : "none");
  show(".au-dhan", b === "DHAN");
  show(".au-api", b === "ANGEL" || b === "ZERODHA" || b === "ALICE" || b === "DELTA");
  show(".au-secret", b === "ZERODHA" || b === "DELTA");
  show(".au-ang", b === "ANGEL");
  // Client / User ID is required for every broker (kept visible for all).
  const cid = document.getElementById("auClient").closest("label");
  if (cid) cid.style.display = "";
}
document.getElementById("auBroker").onchange = auApplyBrokerFields;
function auResetForm() {
  document.getElementById("auFormTitle").textContent = "Add a broker account";
  ["auAcctId", "auClient", "auAppId", "auAppSecret", "auApiKey", "auApiSecret", "auPin", "auTotp"].forEach((i) => document.getElementById(i).value = "");
  auApplyBrokerFields();
}
document.getElementById("auFormReset").onclick = auResetForm;
function auEdit(aid) {
  const a = _auAccts.find((x) => String(x.id) === String(aid)); if (!a) return;
  document.getElementById("auFormTitle").textContent = "Edit " + (AU_NAME[a.broker] || a.broker) + " account";
  document.getElementById("auAcctId").value = a.id;
  document.getElementById("auBroker").value = a.broker;
  document.getElementById("auClient").value = a.client_id || "";
  ["auAppSecret", "auApiKey", "auApiSecret", "auPin", "auTotp"].forEach((i) => document.getElementById(i).value = "");
  document.getElementById("auAppId").value = "";
  auApplyBrokerFields();
}
document.getElementById("auSaveAcct").onclick = async () => {
  const payload = { id: document.getElementById("auAcctId").value || undefined,
    broker: document.getElementById("auBroker").value,
    client_id: document.getElementById("auClient").value.trim(),
    app_id: document.getElementById("auAppId").value, app_secret: document.getElementById("auAppSecret").value,
    api_key: document.getElementById("auApiKey").value, api_secret: document.getElementById("auApiSecret").value,
    pin: document.getElementById("auPin").value, totp_secret: document.getElementById("auTotp").value };
  try {
    await api.post(`/api/admin/users/${_auId}/accounts`, payload);
    toast("✅ Broker account saved", "pos"); auResetForm(); auRefresh();
  } catch (e) { document.getElementById("auAcctMsg").textContent = "❌ " + e.message; document.getElementById("auAcctMsg").className = "msg neg"; }
};
async function auLogin(aid, broker) {
  const msg = document.getElementById("auAcctMsg");
  try {
    const r = await api.get(`/api/admin/users/${_auId}/accounts/${aid}/login`);
    if (r && r.login_url) { window.location.href = r.login_url; return; }   // Dhan / Zerodha OAuth
    msg.textContent = `✅ Logged in to ${AU_NAME[broker] || broker}.`; msg.className = "msg pos";
    toast("✅ Broker connected", "pos"); auRefresh();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; toast("❌ " + e.message, "neg"); }
}
document.getElementById("auClose").onclick = () => {
  document.getElementById("adminUserModal").style.display = "none";
  if (_auTimer) { clearInterval(_auTimer); _auTimer = null; } _auId = null;
};
document.getElementById("auKill").onclick = async () => {
  if (!_auId || !confirm("Trigger remote kill switch — square off this user's positions and halt their algos?")) return;
  await api.post(`/api/admin/users/${_auId}/kill`, {});
  document.getElementById("auMsg").textContent = "🛑 Kill switch activated."; document.getElementById("auMsg").className = "msg pos";
};

async function adminLoadPlans() {
  let rows; try { rows = await api.get("/api/admin/plans"); } catch (e) { return; }
  const box = document.getElementById("adminPlans");
  box.innerHTML = `<table class="data"><thead><tr><th>Name</th><th>Days</th><th>Price</th><th>Active</th><th></th></tr></thead><tbody>`
    + rows.map((p) => `<tr><td>${esc(p.name)}</td><td>${p.days}</td><td>₹${p.price}</td><td>${p.active ? "✓" : "—"}</td>
      <td><button class="btn btn-sm" data-pl-edit='${JSON.stringify(p)}'>Edit</button>
      <button class="btn btn-sm" data-pl-del="${p.id}">✕</button></td></tr>`).join("") + "</tbody></table>";
  box.querySelectorAll("[data-pl-edit]").forEach((b) => b.onclick = () => {
    const p = JSON.parse(b.dataset.plEdit);
    document.getElementById("adPlanId").value = p.id; document.getElementById("adPlanName").value = p.name;
    document.getElementById("adPlanDays").value = p.days; document.getElementById("adPlanPrice").value = p.price;
  });
  box.querySelectorAll("[data-pl-del]").forEach((b) => b.onclick = async () => {
    if (confirm("Delete plan?")) { await api.del("/api/admin/plans/" + b.dataset.plDel); adminLoadPlans(); }
  });
}
document.getElementById("adSavePlan").onclick = async () => {
  await api.post("/api/admin/plans", { id: document.getElementById("adPlanId").value || undefined,
    name: document.getElementById("adPlanName").value, days: parseInt(document.getElementById("adPlanDays").value) || 30,
    price: parseFloat(document.getElementById("adPlanPrice").value) || 0, active: true });
  toast("✅ Plan saved", "pos"); document.getElementById("adPlanId").value = ""; adminLoadPlans();
};

let _adminSettings = null;
async function adminLoadSettings() {
  let s; try { s = await api.get("/api/admin/settings"); } catch (e) { return; }
  _adminSettings = s;
  initRichEditors();
  document.getElementById("adRegOpen").checked = !!s.registration_open;
  document.getElementById("adTrialDays").value = s.trial_days;
  const labels = { DHAN: "Dhan", ANGEL: "Angel One", ZERODHA: "Zerodha", ALICE: "Alice Blue" };
  document.getElementById("adBrokers").innerHTML = (s.all_brokers || []).map((b) =>
    `<label class="check"><input type="checkbox" class="ad-brk" value="${b}" ${(s.enabled_brokers || []).includes(b) ? "checked" : ""}/> ${labels[b] || b}</label>`).join("");
  setRTE("adHowto", s.howto_md || "");
  document.getElementById("adSmtpHost").value = s.smtp_host || "";
  document.getElementById("adSmtpPort").value = s.smtp_port || 587;
  document.getElementById("adSmtpUser").value = s.smtp_user || "";
  document.getElementById("adSmtpSender").value = s.smtp_sender || "";
  document.getElementById("adSmtpFrom").value = s.smtp_from || "";
  document.getElementById("adSmtpBcc").value = s.smtp_bcc || "";
}
document.getElementById("adSaveSaas").onclick = async () => {
  try {
    const enabled = [...document.querySelectorAll(".ad-brk:checked")].map((x) => x.value);
    await api.post("/api/admin/settings", { registration_open: document.getElementById("adRegOpen").checked,
      trial_days: parseInt(document.getElementById("adTrialDays").value) || 7,
      enabled_brokers: enabled,
      howto_md: getRTE("adHowto") });
    document.getElementById("adSaasMsg").textContent = "✅ Saved"; document.getElementById("adSaasMsg").className = "msg pos";
    toast("✅ SaaS settings saved", "pos"); loadHowto();
  } catch (e) { toast("❌ " + e.message, "neg"); }
};
document.getElementById("adSaveSmtp").onclick = async () => {
  const body = { smtp_host: document.getElementById("adSmtpHost").value, smtp_port: parseInt(document.getElementById("adSmtpPort").value) || 587,
    smtp_user: document.getElementById("adSmtpUser").value, smtp_sender: document.getElementById("adSmtpSender").value,
    smtp_from: document.getElementById("adSmtpFrom").value, smtp_bcc: document.getElementById("adSmtpBcc").value };
  const pw = document.getElementById("adSmtpPass").value; if (pw) body.smtp_pass = pw;
  try { await api.post("/api/admin/settings", body);
    document.getElementById("adEmailMsg").textContent = "✅ Saved"; document.getElementById("adEmailMsg").className = "msg pos";
    toast("✅ Email settings saved", "pos");
  } catch (e) { toast("❌ " + e.message, "neg"); }
};
document.getElementById("adTestEmail").onclick = async () => {
  try { const d = await api.post("/api/admin/test_email", {}); toast(d.ok ? "✅ Test email queued" : "⚠️ " + d.message, d.ok ? "pos" : "neg"); }
  catch (e) { toast("❌ " + e.message, "neg"); }
};
async function adminLoadTemplate() {
  initRichEditors();
  const key = document.getElementById("adTplKey").value;
  try { const all = await api.get("/api/admin/templates"); const t = all[key] || {};
    document.getElementById("adTplSubject").value = t.subject || "";
    setRTE("adTplBody", t.body_html || "");
  } catch (e) {}
}
document.getElementById("adTplKey").onchange = adminLoadTemplate;
document.getElementById("adSaveTpl").onclick = async () => {
  try { await api.post("/api/admin/templates", { key: document.getElementById("adTplKey").value,
    subject: document.getElementById("adTplSubject").value, body_html: getRTE("adTplBody") });
    document.getElementById("adTplMsg").textContent = "✅ Saved"; document.getElementById("adTplMsg").className = "msg pos";
    toast("✅ Template saved", "pos");
  } catch (e) { toast("❌ " + e.message, "neg"); }
};

// ---- admin: all-users day-wise logs ----
let adLog = { date: "", level: "", q: "", page: 1, pages: 1, inited: false };
async function adminLoadAllLogs() {
  const p = new URLSearchParams({ date: adLog.date, level: adLog.level, q: adLog.q, page: adLog.page });
  let d; try { d = await api.get("/api/admin/logs?" + p.toString()); } catch (e) { return; }
  adLog.pages = d.pages;
  const sel = document.getElementById("adLogDay");
  if (sel && sel.options.length !== (d.days || []).length + 1) {
    sel.innerHTML = `<option value="">All days</option>` + (d.days || []).map((x) => `<option>${x}</option>`).join("");
    sel.value = adLog.date;
  }
  document.getElementById("adLogPage").textContent = `Page ${adLog.page} / ${d.pages}`;
  document.getElementById("adLogsBody").innerHTML = d.logs.map((r) =>
    `<tr><td>${fmtIST(r.time)}</td><td>${esc(r.email)}</td><td><span class="rbadge r-${r.level === "ERROR" ? "STOPLOSS" : r.level === "WARN" ? "TRAIL" : "MANUAL"}">${r.level}</span></td><td>${esc(r.message)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">No logs.</td></tr>`;
}
(function wireAdLogs() {
  const on = (id, fn) => { const e = document.getElementById(id); if (e) e[id.includes("Search") ? "oninput" : (e.tagName === "SELECT" ? "onchange" : "onclick")] = fn; };
  let t = null;
  document.getElementById("adLogDay").onchange = (e) => { adLog.date = e.target.value; adLog.page = 1; adminLoadAllLogs(); };
  document.getElementById("adLogLevel").onchange = (e) => { adLog.level = e.target.value; adLog.page = 1; adminLoadAllLogs(); };
  document.getElementById("adLogSearch").oninput = (e) => { clearTimeout(t); adLog.q = e.target.value; adLog.page = 1; t = setTimeout(adminLoadAllLogs, 300); };
  document.getElementById("adLogPrev").onclick = () => { if (adLog.page > 1) { adLog.page--; adminLoadAllLogs(); } };
  document.getElementById("adLogNext").onclick = () => { if (adLog.page < adLog.pages) { adLog.page++; adminLoadAllLogs(); } };
})();

// ============================================================================
// Baskets — build a multi-leg strategy, fire instantly or at an exact second.
// ============================================================================
const BK = { list: [], cur: null, seg: "OPTION", picked: null, lot: 1 };

function bkCur() { return BK.list.find((b) => b.id === BK.cur) || null; }

async function bkLoad(keepId) {
  try { BK.list = await api.get("/api/baskets"); } catch { BK.list = []; }
  if (keepId && BK.list.find((b) => b.id === keepId)) BK.cur = keepId;
  if (!BK.cur || !BK.list.find((b) => b.id === BK.cur)) BK.cur = BK.list[0] ? BK.list[0].id : null;
  bkRenderSelect();
  bkRenderPanel();
}
const bkReload = () => bkLoad(BK.cur);

function bkMerge(b) {
  const i = BK.list.findIndex((x) => x.id === b.id);
  if (i >= 0) BK.list[i] = b; else BK.list.unshift(b);
  BK.cur = b.id; bkRenderSelect(); bkRenderPanel();
}

function bkRenderSelect() {
  const sel = document.getElementById("bkSelect");
  sel.innerHTML = BK.list.map((b) =>
    `<option value="${b.id}">${esc(b.name)} · ${b.legs.length} legs</option>`).join("");
  document.getElementById("bkEmpty").style.display = BK.list.length ? "none" : "";
  document.getElementById("bkPanel").style.display = BK.list.length ? "" : "none";
  if (BK.cur) sel.value = BK.cur;
}

function bkRenderPanel() {
  const b = bkCur();
  if (!b) { document.getElementById("bkPanel").style.display = "none"; return; }
  document.getElementById("bkPanel").style.display = "";
  document.getElementById("bkName").value = b.name;
  document.querySelectorAll("#bkMode button").forEach((x) => x.classList.toggle("active", x.dataset.m === b.mode));
  document.getElementById("bkLockStep").value = b.lock_step || "";
  document.getElementById("bkLockAmt").value = b.lock_amount || "";
  bkRefreshLive(b);
}

// Live-only refresh (won't clobber inputs the user is editing).
function bkRefreshLive(b) {
  if (!b || bkCur()?.id !== b.id) return;
  const mtm = document.getElementById("bkMtm");
  mtm.innerHTML = b.legs.length
    ? `MTM <b class="${cls(b.mtm)}">${money(b.mtm)}</b> · Booked <b class="${cls(b.booked)}">${money(b.booked)}</b>` : "";
  document.getElementById("bkLockState").textContent =
    b.lock_floor > 0 ? `🔒 securing ₹${b.lock_floor.toLocaleString("en-IN")}` : "";
  bkRenderLegs(b);
  bkRenderSchedule(b);
  const anyOpen = b.legs.some((l) => l.status === "EXECUTED");
  const anyFailed = b.legs.some((l) => l.status === "FAILED");
  document.getElementById("bkSquareoff").style.display = anyOpen ? "" : "none";
  document.getElementById("bkRetry").style.display = anyFailed ? "" : "none";
}

function bkLegBadge(s) {
  const m = { PENDING: "pend", EXECUTED: "exec", FAILED: "fail", CANCELLED: "canc", CLOSED: "closed" };
  const t = { PENDING: "Pending", EXECUTED: "Executed", FAILED: "Failed", CANCELLED: "Cancelled", CLOSED: "Closed" };
  return `<span class="lst ${m[s] || ""}">${t[s] || s}</span>`;
}

function bkLegType(l) {
  const et = l.entry_type || l.order_type;
  return { MARKET: "Market", LIMIT: "Limit", SCHEDULED: "⏱ " + (l.scheduled_time || "time"),
           TRIGGER: "🎯 " + (l.trigger_price || ""), SL: "SL", TIME: "⏱ Time" }[et] || et;
}
function bkLegEntry(l) {
  if (l.entry_type === "LIMIT") return "₹" + l.price;
  if (l.entry_type === "TRIGGER") return "@" + l.trigger_price;
  if (l.entry_type === "SCHEDULED") return l.scheduled_time || "—";
  return "Mkt";
}
function bkLegSlTgt(l) {
  let nT = 0;
  try { nT = l.targets_json ? JSON.parse(l.targets_json).length : 0; } catch (e) {}
  const sl = l.sl_points ? `SL ${l.sl_points}` + (l.trail_sl ? `↗${l.trail_sl}` : "") : "";
  const tg = nT ? `T×${nT}` : (l.target_points ? `T ${l.target_points}` : "");
  const risk = (l.max_profit_amt || l.max_loss_amt || l.lock_step) ? " 🔒" : "";
  return (sl || tg || risk) ? ((sl && tg ? sl + " / " + tg : sl + tg) + risk) : "–";
}
function bkRenderLegs(b) {
  const body = document.getElementById("bkLegsBody");
  if (!b.legs.length) {
    body.innerHTML = `<tr><td colspan="11" class="muted" style="text-align:center;padding:14px;">No legs yet — tap <b>＋ Add leg</b> to configure one with the full trade form.</td></tr>`;
    return;
  }
  body.innerHTML = b.legs.map((l, i) => {
    const edit = l.status === "PENDING"
      ? `<button class="ic-btn" title="Edit leg" onclick="bkEditLeg(${l.id})">✎</button><button class="ic-btn" title="Remove" onclick="bkDelLeg(${l.id})">✕</button>` : "";
    return `<tr class="${l.status === "FAILED" ? "leg-fail" : ""}" title="${esc(l.error || "")}">
      <td>${i + 1}</td><td>${esc(l.symbol)}</td>
      <td class="${l.transaction_type === "BUY" ? "pos" : "neg"}">${l.transaction_type}</td>
      <td>${bkLegType(l)}</td><td>${l.quantity}</td><td>${bkLegEntry(l)}</td>
      <td class="muted" style="font-size:11px;">${bkLegSlTgt(l)}</td>
      <td>${l.ltp ? "₹" + l.ltp : "–"}</td>
      <td class="${cls(l.pnl)}">${l.pnl ? money(l.pnl) : "–"}</td>
      <td>${bkLegBadge(l.status)}</td>
      <td style="white-space:nowrap;">${edit}</td>
    </tr>`;
  }).join("");
}

function bkRenderSchedule(b) {
  const toggle = document.getElementById("bkSchedToggle");
  const scheduled = b.is_scheduled && b.schedule_status === "PENDING";
  if (scheduled) toggle.checked = true;
  document.getElementById("bkSchedRow").style.display = toggle.checked ? "" : "none";
  document.getElementById("bkSchedCancel").style.display = scheduled ? "" : "none";
  const st = document.getElementById("bkSchedState");
  if (scheduled) st.innerHTML = `⏱ Scheduled for <b>${esc(b.scheduled_at_ist)} IST</b>`;
  else if (b.schedule_status) st.textContent = "Last schedule: " + b.schedule_status.toLowerCase();
  else st.textContent = "";
}

async function bkDelLeg(id) {
  try { bkMerge(await api.del(`/api/baskets/${BK.cur}/legs/${id}`)); } catch (e) {}
}

// ---- add / edit a leg using the FULL New Trade form (basket mode) ----
let bkMode = { active: false, basketId: null, legId: null };
const switchTab = (name) => { const t = document.querySelector(`.tab[data-tab="${name}"]`); if (t) t.click(); };

function bkApplyMode(b) {
  document.getElementById("bkModeBar").style.display = bkMode.active ? "" : "none";
  document.getElementById("modeRow").style.display = bkMode.active ? "none" : "";   // basket controls TEST/LIVE
  document.getElementById("bkModeName").textContent = b ? `${b.name} · ${b.mode}` : "";
  document.getElementById("tradeSubmitBtn").textContent =
    bkMode.active ? (bkMode.legId ? "✓ Update leg" : "➕ Add to basket") : "Create Trade";
}
function bkExitMode(goBack) {
  bkMode = { active: false, basketId: null, legId: null };
  bkApplyMode(null);
  if (goBack) switchTab("baskets");
}
function bkStartAddLeg() {
  if (!BK.cur) { toast("⚠️ Create or select a basket first.", "neg"); return; }
  if (typeof gpExitMode === "function") gpExitMode(false);    // leave group mode if it was on
  bkMode = { active: true, basketId: BK.cur, legId: null };
  ulSearch.value = ""; resetOrderForm(); resetPicker();
  document.getElementById("selectedSymbol").textContent = "No symbol selected yet.";
  document.getElementById("formMsg").textContent = "";
  bkApplyMode(bkCur());
  switchTab("new");
}
function bkEditLeg(id) {
  const b = bkCur(); if (!b) return;
  const leg = b.legs.find((l) => l.id === id); if (!leg) return;
  if (leg.status !== "PENDING") { toast("Only legs that haven't fired yet can be edited.", "neg"); return; }
  bkMode = { active: true, basketId: BK.cur, legId: id };
  ulSearch.value = ""; resetOrderForm(); resetPicker();
  bkPrefillForm(leg);
  bkApplyMode(b);
  switchTab("new");
}
function bkSetSide(s) {
  document.querySelectorAll("[data-side]").forEach((x) => x.classList.toggle("active", x.dataset.side === s));
  form.side.value = s;
}
function bkPrefillForm(leg) {
  // Select the contract directly (no preset side-effects to clobber the values).
  form.symbol.value = leg.symbol; form.security_id.value = leg.security_id;
  form.exchange_segment.value = leg.exchange_segment; form.instrument_type.value = leg.instrument_type;
  currentLotSize = leg.lot_size || 1;
  _selContract = { symbol: leg.symbol, security_id: leg.security_id,
                   exchange_segment: leg.exchange_segment, instrument_type: leg.instrument_type };
  showSelected(_selContract, null);
  bkSetSide(leg.transaction_type);
  lotsInput.value = Math.max(1, Math.round((leg.quantity || 1) / (leg.lot_size || 1)));
  updateQty();
  applyEntryType(leg.entry_type || "MARKET");
  if (leg.entry_type === "LIMIT") entryPrice.value = leg.price || 0;
  if (leg.entry_type === "SCHEDULED") setVal("schedTime", leg.scheduled_time || "09:15:00");
  if (leg.entry_type === "TRIGGER") setVal("trigPrice", leg.trigger_price || 0);
  form.sl_points.value = leg.sl_points || 0;
  form.trail_sl.value = leg.trail_sl || 0;
  form.trail_mode.value = leg.trail_mode || "CONTINUE";
  let targets = [];
  try { targets = leg.targets_json ? JSON.parse(leg.targets_json) : []; } catch (e) {}
  if (targets.length) {
    multiToggle.checked = true; multiWrap.style.display = "block"; targetField.style.display = "none";
    document.getElementById("targetRows").innerHTML = "";
    targets.forEach((t) => addTargetRow(t.points));
  } else {
    form.target_points.value = leg.target_points || 0;
  }
  form.max_profit_amt.value = leg.max_profit_amt || 0;
  form.max_loss_amt.value = leg.max_loss_amt || 0;
  form.lock_step.value = leg.lock_step || 0;
  form.lock_amount.value = leg.lock_amount || 0;
}

// Called by the trade-form submit handler when a basket leg is being saved.
async function bkSubmitLeg(payload) {
  const leg = Object.assign({}, payload, {
    transaction_type: payload.side, price: payload.entry_price,
    underlying: (currentSeg === "EQUITY" ? "" : currentUnderlying) || "",
  });
  try {
    if (bkMode.legId) {
      bkMerge(await api.post(`/api/baskets/${bkMode.basketId}/legs/${bkMode.legId}`, leg));
      toast("✅ Leg updated", "pos");
      bkExitMode(true);
    } else {
      const b = await api.post(`/api/baskets/${bkMode.basketId}/legs`, { replace: false, legs: [leg] });
      bkMerge(b);
      toast(`✅ Leg added (${b.legs.length} total) — add another, or tap Cancel to go back`, "pos");
      document.getElementById("formMsg").textContent = `✅ Added — basket now has ${b.legs.length} leg(s).`;
      document.getElementById("formMsg").className = "msg pos";
      ulSearch.value = ""; resetOrderForm(); resetPicker();
      document.getElementById("selectedSymbol").textContent = "No symbol selected yet.";
    }
  } catch (e) {}
}

// ---- execution / scheduling / quick actions ----
async function bkExecute() {
  if (!BK.cur) return;
  const btn = document.getElementById("bkExecute");
  if (btn.disabled) return;
  const old = btn.innerHTML;
  btn.disabled = true; btn.classList.add("loading"); btn.innerHTML = "Dispatching…";
  try {
    let res = await api.post(`/api/baskets/${BK.cur}/execute`, {});
    if (res.conflict) {
      const go = confirm(`This basket is scheduled for ${res.scheduled_at_ist} IST.\n\nCancel the schedule and execute now?`);
      if (!go) return;
      res = await api.post(`/api/baskets/${BK.cur}/execute`, { force: true });
    }
    if (res.dispatching) { toast("⚡ Dispatching basket…", "pos"); setTimeout(bkReload, 1300); }
  } catch (e) {} finally {
    btn.disabled = false; btn.classList.remove("loading"); btn.innerHTML = old;
  }
}
async function bkSchedSet() {
  const date = document.getElementById("bkSchedDate").value;
  const time = document.getElementById("bkSchedTime").value;
  if (!time) { toast("⚠️ Pick a time.", "neg"); return; }
  try { const b = await api.post(`/api/baskets/${BK.cur}/schedule`, { date, time });
    bkMerge(b); toast(`⏱ Scheduled for ${b.scheduled_at_ist} IST`, "pos"); } catch (e) {}
}
async function bkSchedCancel() {
  try { bkMerge(await api.post(`/api/baskets/${BK.cur}/cancel_schedule`)); toast("Schedule cancelled", "info"); } catch (e) {}
}
async function bkSquareoff() {
  if (!confirm("Square off all open legs in this basket at market?")) return;
  try { await api.post(`/api/baskets/${BK.cur}/squareoff`); toast("⊗ Squaring off basket…", "info"); setTimeout(bkReload, 1200); } catch (e) {}
}
async function bkRetry() {
  try { await api.post(`/api/baskets/${BK.cur}/retry`); toast("↻ Retrying failed legs…", "info"); setTimeout(bkReload, 1300); } catch (e) {}
}
async function bkMargin() {
  const out = document.getElementById("bkMarginOut");
  out.textContent = "checking…";
  try {
    const m = await api.post(`/api/baskets/${BK.cur}/margin`);
    const req = `Required <b>₹${Math.round(m.required).toLocaleString("en-IN")}</b>`;
    out.innerHTML = m.verified
      ? `${req} · Available <b>₹${Math.round(m.available).toLocaleString("en-IN")}</b> · ${m.ok ? '<span class="pos">✓ sufficient</span>' : '<span class="neg">⚠ looks short</span>'}`
      : `${req} · <span class="muted">${esc(m.detail || "estimate (not broker-verified)")}</span>`;
  } catch (e) { out.textContent = ""; }
}
async function bkSaveLock() {
  try {
    const b = await api.post(`/api/baskets/${BK.cur}`, {
      lock_step: parseFloat(document.getElementById("bkLockStep").value) || 0,
      lock_amount: parseFloat(document.getElementById("bkLockAmt").value) || 0,
    });
    bkMerge(b); toast("✅ Basket profit-lock saved", "pos");
  } catch (e) {}
}
function bkParseCSV(text) {
  const lines = text.split(/\r?\n/).filter((x) => x.trim());
  if (lines.length < 2) return [];
  const head = lines[0].split(",").map((s) => s.trim().toLowerCase());
  const out = [];
  for (let i = 1; i < lines.length; i++) {
    const c = lines[i].split(",");
    const g = (n) => { const j = head.indexOf(n); return j >= 0 ? (c[j] || "").trim() : ""; };
    if (!g("symbol")) continue;
    out.push({ symbol: g("symbol"), security_id: g("security_id"), exchange_segment: g("exchange_segment"),
      instrument_type: g("instrument_type") || "OPTION", underlying: g("underlying"),
      transaction_type: (g("transaction_type") || "BUY").toUpperCase(),
      order_type: (g("order_type") || "MARKET").toUpperCase(),
      quantity: parseInt(g("quantity")) || 1, price: parseFloat(g("price")) || 0,
      trigger_price: parseFloat(g("trigger_price")) || 0, lot_size: parseInt(g("lot_size")) || 1 });
  }
  return out;
}

function bkPoll() {
  if (!document.getElementById("tab-baskets").classList.contains("active")) return;
  api.get("/api/baskets").then((list) => {
    BK.list = list;
    const b = bkCur();
    if (b) bkRefreshLive(b); else bkRenderSelect();
  }).catch(() => {});
}

function bkInit() {
  document.getElementById("bkCreate").onclick = async () => {
    const name = document.getElementById("bkNewName").value.trim();
    try { const b = await api.post("/api/baskets", { name: name || "Basket", mode: "TEST" });
      document.getElementById("bkNewName").value = ""; await bkLoad(b.id); toast("✅ Basket created", "pos"); } catch (e) {}
  };
  document.getElementById("bkSelect").onchange = (e) => { BK.cur = parseInt(e.target.value); bkRenderPanel(); };
  document.getElementById("bkName").onchange = async (e) => {
    try { bkMerge(await api.post(`/api/baskets/${BK.cur}`, { name: e.target.value })); } catch (err) {}
  };
  document.querySelectorAll("#bkMode button").forEach((btn) => btn.onclick = async () => {
    try { bkMerge(await api.post(`/api/baskets/${BK.cur}`, { mode: btn.dataset.m })); } catch (e) {}
  });
  document.getElementById("bkAddLeg").onclick = bkStartAddLeg;
  document.getElementById("bkModeCancel").onclick = () => bkExitMode(true);
  document.getElementById("bkInvert").onclick = async () => { try { bkMerge(await api.post(`/api/baskets/${BK.cur}/invert`)); toast("⇅ Sides inverted", "info"); } catch (e) {} };
  document.getElementById("bkClone").onclick = async () => { try { const b = await api.post(`/api/baskets/${BK.cur}/clone`); await bkLoad(b.id); toast("⧉ Basket cloned", "pos"); } catch (e) {} };
  document.getElementById("bkClear").onclick = async () => { if (!confirm("Remove all legs from this basket?")) return; try { bkMerge(await api.post(`/api/baskets/${BK.cur}/clear`)); } catch (e) {} };
  document.getElementById("bkDelete").onclick = async () => { if (!confirm("Delete this basket and its legs?")) return; try { await api.del(`/api/baskets/${BK.cur}`); BK.cur = null; await bkLoad(); toast("Basket deleted", "info"); } catch (e) {} };
  document.getElementById("bkExport").onclick = () => { if (BK.cur) window.location = `/api/baskets/${BK.cur}/export`; };
  document.getElementById("bkImport").onclick = () => document.getElementById("bkImportFile").click();
  document.getElementById("bkImportFile").onchange = (e) => {
    const f = e.target.files[0]; if (!f) return;
    const r = new FileReader();
    r.onload = async () => { const legs = bkParseCSV(r.result);
      if (!legs.length) { toast("⚠️ No valid rows in CSV.", "neg"); return; }
      try { bkMerge(await api.post(`/api/baskets/${BK.cur}/legs`, { replace: true, legs })); toast(`⬆ Imported ${legs.length} legs`, "pos"); } catch (er) {} };
    r.readAsText(f); e.target.value = "";
  };
  document.getElementById("bkLockSave").onclick = bkSaveLock;
  document.getElementById("bkMargin").onclick = bkMargin;
  document.getElementById("bkExecute").onclick = bkExecute;
  document.getElementById("bkSquareoff").onclick = bkSquareoff;
  document.getElementById("bkRetry").onclick = bkRetry;
  document.getElementById("bkSchedToggle").onchange = (e) => {
    document.getElementById("bkSchedRow").style.display = e.target.checked ? "" : "none";
  };
  document.getElementById("bkSchedSet").onclick = bkSchedSet;
  document.getElementById("bkSchedCancel").onclick = bkSchedCancel;
  document.querySelector('.tab[data-tab="baskets"]').addEventListener("click", () => bkLoad(BK.cur));
}
bkInit();
bkLoad();

// ============================================================================
// Copy-trading groups (Master -> Slaves replication)
// ============================================================================
const GP = { list: [], cur: null, accounts: [] };
let gpMode = { active: false, groupId: null };

function gpCur() { return GP.list.find((g) => g.id === GP.cur) || null; }

async function gpLoadOptions() {
  try { GP.accounts = (await api.get("/api/groups/options")).accounts || []; } catch (e) { GP.accounts = []; }
}
function gpAcctOptions(selectedId) {
  return `<option value="0">Demo / Paper</option>` + GP.accounts.map((a) =>
    `<option value="${a.id}"${a.id === selectedId ? " selected" : ""}>${esc(a.label)}${a.connected ? "" : " (offline)"}</option>`).join("");
}

async function gpLoad(keepId) {
  await gpLoadOptions();
  try { GP.list = await api.get("/api/groups"); } catch { GP.list = []; }
  if (keepId && GP.list.find((g) => g.id === keepId)) GP.cur = keepId;
  if (!GP.cur || !GP.list.find((g) => g.id === GP.cur)) GP.cur = GP.list[0] ? GP.list[0].id : null;
  gpRenderSelect();
  gpRenderPanel();
}
const gpReload = () => gpLoad(GP.cur);
function gpMerge(g) {
  const i = GP.list.findIndex((x) => x.id === g.id);
  if (i >= 0) GP.list[i] = g; else GP.list.unshift(g);
  GP.cur = g.id; gpRenderSelect(); gpRenderPanel();
}
function gpRenderSelect() {
  const sel = document.getElementById("gpSelect");
  sel.innerHTML = GP.list.map((g) => `<option value="${g.id}">${esc(g.name)} · ${g.slaves.length} slaves</option>`).join("");
  document.getElementById("gpEmpty").style.display = GP.list.length ? "none" : "";
  document.getElementById("gpPanel").style.display = GP.list.length ? "" : "none";
  if (GP.cur) sel.value = GP.cur;
}
function gpRenderPanel() {
  const g = gpCur();
  if (!g) { document.getElementById("gpPanel").style.display = "none"; return; }
  document.getElementById("gpPanel").style.display = "";
  document.getElementById("gpName").value = g.name;
  document.getElementById("gpMaster").innerHTML = gpAcctOptions(g.master_account_id);
  document.getElementById("gpActive").checked = g.is_active;
  document.getElementById("gpSlaveAcct").innerHTML = gpAcctOptions(0);
  gpRefreshLive(g);
}
function gpRefreshLive(g) {
  if (!g || gpCur()?.id !== g.id) return;
  document.getElementById("gpMtm").innerHTML = `Open ${g.open_count} · MTM <b class="${cls(g.slave_mtm)}">${money(g.slave_mtm)}</b>`;
  document.getElementById("gpStatusMtm").innerHTML = g.open_count ? `— ${g.open_count} slave position(s) open` : "";
  gpRenderSlaves(g);
  gpRenderMasters(g);
  gpRenderScheduled(g);
  document.getElementById("gpSquareoff").style.display = g.open_count ? "" : "none";
}
function gpStatusBadge(s) {
  const m = { OPEN: ["exec", "Executed"], EXECUTED: ["exec", "Executed"], CLOSED: ["closed", "Closed"],
              REJECTED: ["fail", "Failed"], FAILED: ["fail", "Failed"], PENDING: ["pend", "Pending"] };
  const x = m[s] || ["", s || "—"];
  return `<span class="lst ${x[0]}">${x[1]}</span>`;
}
function gpRenderSlaves(g) {
  const body = document.getElementById("gpSlavesBody");
  body.innerHTML = g.slaves.length ? g.slaves.map((s) => `<tr>
    <td>${esc(s.label)}</td>
    <td>${s.condition_type === "FIXED" ? "Fixed" : "Multiplier"}</td>
    <td>${s.condition_type === "FIXED" ? s.condition_value + " lots" : s.condition_value + "×"}</td>
    <td><input type="checkbox" ${s.is_active ? "checked" : ""} onchange="gpToggleSlave(${s.id}, this.checked)"></td>
    <td><button class="ic-btn" title="Remove" onclick="gpDelSlave(${s.id})">✕</button></td></tr>`).join("")
    : `<tr><td colspan="5" class="muted" style="text-align:center;padding:10px;">No slaves yet — add follower accounts below.</td></tr>`;
}
function gpRenderMasters(g) {
  const box = document.getElementById("gpMasters");
  if (!g.masters.length) {
    box.innerHTML = `<div class="muted" style="padding:10px;">No group trades yet — tap <b>⚡ Execute Group Trade</b>.</div>`;
    return;
  }
  box.innerHTML = g.masters.map((m) => {
    const done = m.slaves.filter((s) => ["OPEN", "EXECUTED", "CLOSED"].includes(s.status)).length;
    const open = m.slaves.length > 0 && m.slaves.some((s) => s.status === "FAILED" || s.status === "REJECTED");
    return `<details class="gp-master"${open ? " open" : ""}>
      <summary class="gp-master-sum">
        <span class="gp-m-main"><b class="${m.side === "BUY" ? "pos" : "neg"}">${m.side}</b> ${esc(m.symbol)} ×${m.qty}</span>
        ${gpStatusBadge(m.status)}
        <span class="muted gp-m-count">${done}/${m.slaves.length} slaves</span>
        <span class="${cls(m.pnl)}">${m.pnl ? money(m.pnl) : ""}</span>
      </summary>
      <table class="data gp-slave-table"><thead><tr><th>Slave</th><th>Side</th><th>Qty</th><th>LTP</th><th>P&L</th><th>Status</th></tr></thead>
      <tbody>${m.slaves.map((s) => `<tr class="${s.status === "REJECTED" || s.status === "FAILED" ? "leg-fail" : ""}" title="${esc(s.error || "")}">
        <td>${esc(s.account)}</td><td class="${s.side === "BUY" ? "pos" : "neg"}">${s.side}</td><td>${s.qty}</td>
        <td>${s.ltp ? "₹" + s.ltp : "–"}</td><td class="${cls(s.pnl)}">${s.pnl ? money(s.pnl) : "–"}</td>
        <td>${gpStatusBadge(s.status)}${s.error ? ` <span class="muted" style="font-size:10px;">${esc(s.error)}</span>` : ""}</td></tr>`).join("")}</tbody></table>
    </details>`;
  }).join("");
}
function gpRenderScheduled(g) {
  document.getElementById("gpSchedList").innerHTML = (g.scheduled || []).map((o) =>
    `<div class="gp-sched-item">⏱ <b class="${o.side === "BUY" ? "pos" : "neg"}">${o.side}</b> ${esc(o.symbol)} ×${o.qty_lots} lot @ <b>${esc(o.at)} IST</b>
      <button class="ic-btn" title="Cancel" onclick="gpCancelSched(${o.id})">✕</button></div>`).join("");
}
async function gpLoadLogs() {
  const g = gpCur(); if (!g) return;
  let rows; try { rows = await api.get(`/api/groups/${g.id}/logs`); } catch { return; }
  document.getElementById("gpLogsBody").innerHTML = rows.map((r) =>
    `<tr><td>${fmtIST(r.time)}</td><td>${r.action}</td><td>${esc(r.account)}</td><td>${gpStatusBadge(r.status)}</td><td class="muted">${esc(r.error || "")}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">No executions yet.</td></tr>`;
}

// ---- builder actions ----
async function gpToggleSlave(id, on) { try { gpMerge(await api.post(`/api/groups/${GP.cur}/slaves/${id}`, { is_active: on })); } catch (e) {} }
async function gpDelSlave(id) { try { gpMerge(await api.del(`/api/groups/${GP.cur}/slaves/${id}`)); } catch (e) {} }
async function gpCancelSched(id) { try { gpMerge(await api.post(`/api/groups/${GP.cur}/cancel_schedule/${id}`)); toast("Schedule cancelled", "info"); } catch (e) {} }

// ---- instant / scheduled master execution via the full trade form ----
function gpEnterMode(g) {
  if (typeof bkExitMode === "function") bkExitMode(false);
  gpMode = { active: true, groupId: g.id };
  document.body.classList.add("group-mode");
  document.getElementById("gpModeBar").style.display = "flex";
  document.getElementById("gpModeName").textContent = `${g.name} → ${g.master_label}`;
  document.getElementById("gpFormDate").value = ""; document.getElementById("gpFormTime").value = "";
  document.getElementById("tradeSubmitBtn").textContent = "⚡ Fire master & copy";
}
function gpExitMode(goBack) {
  gpMode = { active: false, groupId: null };
  document.body.classList.remove("group-mode");
  const bar = document.getElementById("gpModeBar"); if (bar) bar.style.display = "none";
  const btn = document.getElementById("tradeSubmitBtn"); if (btn && !(typeof bkMode !== "undefined" && bkMode.active)) btn.textContent = "Create Trade";
  if (goBack) switchTab("groups");
}
function gpStartExecute() {
  const g = gpCur(); if (!g) return;
  if (!g.is_active) { toast("Turn the group ON first.", "neg"); return; }
  ulSearch.value = ""; resetOrderForm(); resetPicker();
  document.getElementById("selectedSymbol").textContent = "No symbol selected yet.";
  document.getElementById("formMsg").textContent = "";
  gpEnterMode(g);
  switchTab("new");
}
async function gpSubmit(payload) {
  const order = {
    side: payload.side, security_id: payload.security_id, exchange_segment: payload.exchange_segment,
    instrument_type: payload.instrument_type, symbol: payload.symbol,
    lot_size: currentLotSize, qty_lots: parseInt(lotsInput.value) || 1,
    date: document.getElementById("gpFormDate").value, time: document.getElementById("gpFormTime").value,
  };
  try {
    const r = await api.post(`/api/groups/${gpMode.groupId}/execute`, order);
    if (r.scheduled) toast(`⏱ Group scheduled for ${r.scheduled_at_ist} IST`, "pos");
    else toast("⚡ Firing master & copying to slaves…", "pos");
    gpExitMode(true);
    setTimeout(gpReload, 1200);
  } catch (e) {}
}

function gpPoll() {
  if (!document.getElementById("tab-groups").classList.contains("active")) return;
  api.get("/api/groups").then((list) => { GP.list = list; const g = gpCur(); if (g) gpRefreshLive(g); else gpRenderSelect(); }).catch(() => {});
}

function gpInit() {
  document.getElementById("gpCreate").onclick = async () => {
    const name = document.getElementById("gpNewName").value.trim();
    try { const g = await api.post("/api/groups", { name: name || "Group", master_account_id: 0 });
      document.getElementById("gpNewName").value = ""; await gpLoad(g.id); toast("✅ Group created", "pos"); } catch (e) {}
  };
  document.getElementById("gpSelect").onchange = (e) => { GP.cur = parseInt(e.target.value); gpRenderPanel(); };
  document.getElementById("gpName").onchange = async (e) => { try { gpMerge(await api.post(`/api/groups/${GP.cur}`, { name: e.target.value })); } catch (er) {} };
  document.getElementById("gpMaster").onchange = async (e) => { try { gpMerge(await api.post(`/api/groups/${GP.cur}`, { master_account_id: parseInt(e.target.value) })); } catch (er) {} };
  document.getElementById("gpActive").onchange = async (e) => { try { gpMerge(await api.post(`/api/groups/${GP.cur}`, { is_active: e.target.checked })); } catch (er) {} };
  document.getElementById("gpAddSlave").onclick = async () => {
    try {
      gpMerge(await api.post(`/api/groups/${GP.cur}/slaves`, {
        slave_account_id: parseInt(document.getElementById("gpSlaveAcct").value),
        condition_type: document.getElementById("gpSlaveType").value,
        condition_value: parseFloat(document.getElementById("gpSlaveVal").value) || 1,
      }));
      toast("✅ Slave added", "pos");
    } catch (e) {}
  };
  document.getElementById("gpExecute").onclick = gpStartExecute;
  document.getElementById("gpSquareoff").onclick = async () => {
    if (!confirm("Square off ALL open slave positions in this group at market?")) return;
    try { const r = await api.post(`/api/groups/${GP.cur}/squareoff`); toast(`⊗ Closing ${r.closing} position(s)…`, "info"); setTimeout(gpReload, 1200); } catch (e) {}
  };
  document.getElementById("gpDelete").onclick = async () => {
    if (!confirm("Delete this group?")) return;
    try { await api.del(`/api/groups/${GP.cur}`); GP.cur = null; await gpLoad(); toast("Group deleted", "info"); } catch (e) {}
  };
  document.getElementById("gpModeCancel").onclick = () => gpExitMode(true);
  const logs = document.querySelector(".gp-logs");
  if (logs) logs.addEventListener("toggle", () => { if (logs.open) gpLoadLogs(); });
  document.querySelector('.tab[data-tab="groups"]').addEventListener("click", () => gpLoad(GP.cur));
}
gpInit();
gpLoad();

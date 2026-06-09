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
function checkAuth(r) { if (r.status === 401) { location.href = "/login"; throw new Error("Login required"); } return r; }
const api = {
  async get(url) { const r = checkAuth(await trackedFetch(url)); return r.json(); },
  async post(url, body) {
    const r = checkAuth(await trackedFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null,
    }));
    if (!r.ok) throw new Error((await r.json()).detail || "Request failed");
    return r.json();
  },
  async del(url) { const r = checkAuth(await trackedFetch(url, { method: "DELETE" })); return r.json(); },
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
  };
});

// ---- summary / P&L ----
let pnlFilter = "ALL";
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

function setCard(id, val) {
  const el = document.getElementById(id);
  if (el) { el.textContent = money(val); el.className = "card-value " + cls(val); }
}

async function refreshSummary() {
  const s = await api.get("/api/summary?broker=" + pnlFilter);
  const p = s.pnl || {};
  setCard("sumBooked", p.booked || 0);
  setCard("sumActive", p.active || 0);
  setCard("sumTotal", p.total || 0);
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
          await api.post("/api/settings/reset_halt", {}); await refreshAll();
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
  const brokerMap = { ANGEL: s.angel_map, ZERODHA: s.zerodha_map, ALICE: s.alice_map };
  const map = brokerMap[dataBroker];
  if (ssLabel) {
    const src = { DEMO: "Demo", DHAN: "Dhan", ANGEL: "Angel One",
                  ZERODHA: "Zerodha", ALICE: "Alice Blue" }[dataBroker] || "Dhan";
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

// ---- trades table ----
let tradesById = {};
function showTradesSkeleton(n = 4) {
  document.getElementById("tradesBody").innerHTML = Array.from({ length: n })
    .map(() => `<tr class="skel-row"><td colspan="13"><div class="skel-bar"></div></td></tr>`).join("");
}
async function refreshTrades() {
  const rows = await api.get("/api/trades");
  tradesById = {};
  rows.forEach((t) => (tradesById[t.id] = t));
  const body = document.getElementById("tradesBody");
  body.innerHTML = "";
  for (const t of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.id}</td>
      <td>${t.symbol}</td>
      <td><span class="badge b-${t.mode}">${t.mode}</span></td>
      <td>${brokerLabel(t)}</td>
      <td>${t.side}</td>
      <td>${qtyCell(t)}</td>
      <td>${entryCell(t)}</td>
      <td>${t.stop_loss || (t.sl_points ? t.sl_points + "p" : "-")}</td>
      <td>${targetCell(t)}</td>
      <td>${t.last_price || "-"}</td>
      <td class="${cls(t.pnl)}">${money(t.pnl)}</td>
      <td>${statusCell(t)}</td>
      <td>${actionsFor(t)}</td>`;
    body.appendChild(tr);
  }
  // wire action buttons
  body.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.act === "modify") openModify(b.dataset.id);
      else doAction(b.dataset.act, b.dataset.id);
    };
  });
}

function brokerLabel(t) {
  const name = t.broker === "PAPER" ? "Paper" : (t.broker || (t.mode === "TEST" ? "Paper" : "—"));
  const ext = t.source === "EXTERNAL" ? ' <span class="rbadge r-MANUAL">EXT</span>' : "";
  return name + ext;
}

function entryCell(t) {
  if (t.status === "PENDING") {
    if (t.entry_type === "SCHEDULED" && t.scheduled_time)
      return `<span title="scheduled market order">⏱ ${t.scheduled_time}</span>`;
    if (t.entry_type === "TRIGGER" && t.trigger_price)
      return `<span title="algo trigger">🎯 ${t.trigger_dir === "ABOVE" ? "≥" : t.trigger_dir === "BELOW" ? "≤" : "@"} ${t.trigger_price}</span>`;
  }
  return t.entry_fill_price || t.entry_price || "-";
}

function qtyCell(t) {
  // Show remaining vs total once some quantity has been booked via targets.
  const exited = t.exited_qty || 0;
  if (exited > 0 && t.status === "OPEN") {
    return `<span title="remaining / total">${t.quantity - exited} / ${t.quantity}</span>`;
  }
  return t.quantity;
}

function statusCell(t) {
  // For closed trades, show WHY it exited (TARGET / TRAIL / STOPLOSS / ...).
  if (t.status === "CLOSED" && t.exit_reason) {
    return `<span class="badge b-CLOSED">CLOSED</span> <span class="rbadge r-${t.exit_reason}">${t.exit_reason}</span>`;
  }
  return `<span class="badge b-${t.status}">${t.status}</span>`;
}

function targetCell(t) {
  if (t.targets_json && t.targets_json !== "[]") {
    try {
      const ts = JSON.parse(t.targets_json);
      const done = ts.filter((x) => x.hit).length;
      return `multi ${done}/${ts.length}`;
    } catch (e) { /* fall through */ }
  }
  return t.target || (t.target_points ? t.target_points + "p" : "-");
}

function actionsFor(t) {
  let h = "";
  if (t.status === "PENDING")
    h += `<button class="btn btn-sm" data-act="cancel" data-id="${t.id}">Cancel</button> `;
  if (t.status === "OPEN")
    h += `<button class="btn btn-sm" data-act="modify" data-id="${t.id}">SL/TP</button> `
       + `<button class="btn btn-sm" data-act="close" data-id="${t.id}">Close</button> `;
  if (t.status === "CLOSED" || t.status === "CANCELLED" || t.status === "REJECTED")
    h += `<button class="btn btn-sm" data-act="del" data-id="${t.id}">Delete</button>`;
  return h;
}

async function doAction(act, id, sym) {
  try {
    if (act === "cancel") await api.post(`/api/trades/${id}/cancel`);
    else if (act === "close") await api.post(`/api/trades/${id}/close`);
    else if (act === "del") await api.del(`/api/trades/${id}`);
    await refreshAll();
  } catch (e) { alert(e.message); }
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
    const time = new Date(r.time + "Z").toLocaleTimeString();
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
  ulResults.classList.remove("show");
  expiryWrap.style.display = "none";
  chainWrap.style.display = "none";
  futWrap.style.display = "none";
  futWrap.innerHTML = ""; chainBody.innerHTML = "";
  if (ltpTimer) { clearInterval(ltpTimer); ltpTimer = null; }
  if (typeof selLtpTimer !== "undefined" && selLtpTimer) { clearInterval(selLtpTimer); selLtpTimer = null; }
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

function renderFutures(futs) {
  if (!futs.length) { futWrap.style.display = "none"; return; }
  futWrap.innerHTML = futs.map((f, i) => `<button type="button" class="fut-btn" data-i="${i}">
    ${f.symbol}<br><span class="muted" style="font-size:11px;">exp ${f.expiry} · lot ${parseInt(parseFloat(f.lot_size))}</span></button>`).join("");
  futWrap.style.display = "flex";
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
  currentLotSize = (r.lot_size && parseInt(parseFloat(r.lot_size)) > 0) ? parseInt(parseFloat(r.lot_size)) : 1;
  updateQty();
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
  try {
    const t = await api.post("/api/trades", payload);
    msg.textContent = `✅ Created trade #${t.id} (${t.symbol}).`; msg.className = "msg pos";
    e.target.reset();
    document.getElementById("selectedSymbol").textContent = "No symbol selected yet.";
    ulSearch.value = ""; resetOrderForm(); resetPicker();
    await refreshAll();
  } catch (err) { msg.textContent = "❌ " + err.message; msg.className = "msg neg"; }
};

// ---- logout ----
document.getElementById("logoutBtn").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  location.href = "/login";
};

// ---- kill switch ----
document.getElementById("killBtn").onclick = async () => {
  const s = await api.get("/api/summary");
  const turningOn = s.kill_switch !== "on";
  if (turningOn && !confirm("Turn ON kill switch? This blocks new entries and flattens open positions.")) return;
  await api.post("/api/settings", { kill_switch: turningOn });
  await refreshAll();
};

// ---- broker / providers / accounts ----
let _accounts = [];
function provLabel(value) {
  if (value === "DEMO") return "🧪 Demo (simulated)";
  const a = _accounts.find((x) => String(x.id) === String(value));
  return a ? a.label : "—";
}
function fillProviderSelect(id, value) {
  const sel = document.getElementById(id);
  let html = `<option value="DEMO">🧪 Demo (simulated)</option>`;
  html += _accounts.map((a) => `<option value="${a.id}">${a.label}${a.connected ? " ✓" : ""}</option>`).join("");
  sel.innerHTML = html;
  sel.value = value;
}

// Demo price controls
document.querySelectorAll("[data-dir]").forEach((b) => {
  b.onclick = async () => { await api.post("/api/demo/direction", { direction: b.dataset.dir }); await refreshSummary(); };
});
document.getElementById("demoReset").onclick = async () => { await api.post("/api/demo/reset"); await refreshAll(); };

function renderAccounts() {
  const box = document.getElementById("accountsList");
  if (!_accounts.length) { box.innerHTML = `<div class="muted" style="margin-bottom:8px;">No accounts yet — add one below.</div>`; return; }
  box.innerHTML = _accounts.map((a) => `
    <div class="acct-row">
      <span class="acct-name">${a.label}</span>
      <span class="pill ${a.connected ? "pill-ok" : "pill-off"}">${a.connected ? (a.token_hours_left != null ? `Connected · ~${a.token_hours_left}h` : "Connected") : "Not connected"}</span>
      <span style="flex:1;"></span>
      <button class="btn btn-sm" data-login-acc="${a.id}" data-broker="${a.broker}">🔐 Login</button>
      <button class="btn btn-sm" data-edit-acc="${a.id}">Edit</button>
      <button class="btn btn-sm" data-del-acc="${a.id}">✕</button>
    </div>`).join("");
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
  fillProviderSelect("dataProvider", b.data_provider || "DEMO");
  fillProviderSelect("tradeProvider", b.trade_provider || "DEMO");
  buildPnlFilter();
  renderAccounts();
  document.getElementById("demoControls").style.display = (b.data_provider === "DEMO") ? "flex" : "none";
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
      msg.textContent = ""; await refreshBroker();
    } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; await refreshBroker(); }
  };
});

// add / edit account form
const BROKER_NAME = { DHAN: "Dhan", ANGEL: "Angel One", ZERODHA: "Zerodha", ALICE: "Alice Blue" };
function showAcctForm(broker, acc) {
  document.getElementById("acctForm").style.display = "block";
  document.getElementById("acctBroker").value = broker;
  document.getElementById("acctId").value = acc ? acc.id : "";
  document.getElementById("acctFormTitle").textContent = (acc ? "Edit " : "New ") + (BROKER_NAME[broker] || broker) + " account";
  document.querySelectorAll(".dhan-f").forEach((e) => e.style.display = broker === "DHAN" ? "" : "none");
  document.querySelectorAll(".api-f").forEach((e) => e.style.display = (broker === "ANGEL" || broker === "ZERODHA" || broker === "ALICE") ? "" : "none");
  document.querySelectorAll(".angel-f").forEach((e) => e.style.display = broker === "ANGEL" ? "" : "none");
  document.querySelectorAll(".zerodha-f").forEach((e) => e.style.display = broker === "ZERODHA" ? "" : "none");
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
  const payload = { broker, id: document.getElementById("acctId").value || undefined,
    client_id: document.getElementById("acctClientId").value };
  if (broker === "DHAN") {
    payload.app_id = document.getElementById("acctAppId").value;
    payload.app_secret = document.getElementById("acctAppSecret").value;
  } else if (broker === "ZERODHA") {
    payload.api_key = document.getElementById("acctApiKey").value;
    payload.api_secret = document.getElementById("acctZSecret").value;
  } else if (broker === "ALICE") {
    payload.api_key = document.getElementById("acctApiKey").value;
  } else {
    payload.api_key = document.getElementById("acctApiKey").value;
    payload.pin = document.getElementById("acctPin").value;
    payload.totp_secret = document.getElementById("acctTotp").value;
  }
  await api.post("/api/accounts", payload);
  document.getElementById("acctForm").style.display = "none";
  document.getElementById("brokerMsg").textContent = "Account saved.";
  await refreshBroker();
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
    try { prices = JSON.parse(t.targets_json).map((tg) => tg.price || ""); } catch (e) { /* ignore */ }
  }
  if (!prices.length) prices = [t.target || ""];
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
  } catch (e) { /* ignore */ }
}
document.getElementById("saveSettingsBtn").onclick = async () => {
  const msg = document.getElementById("settingsMsg");
  try {
    await api.post("/api/settings", {
      daily_max_profit: numVal("dailyMaxProfit"),
      daily_max_loss: numVal("dailyMaxLoss"),
      global_lock_step: numVal("globalLockStep"),
      global_lock_amount: numVal("globalLockAmount"),
    });
    msg.textContent = "✅ Settings saved."; msg.className = "msg pos";
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

// ---- watchlist (one-click load into the New Trade form) ----
let _watchlist = [];
async function loadWatchlist() {
  try { renderWatchlist(await api.get("/api/watchlist")); } catch (e) { /* ignore */ }
}
function renderWatchlist(items) {
  _watchlist = items || [];
  const box = document.getElementById("watchlistChips");
  const empty = document.getElementById("wlEmpty");
  if (!_watchlist.length) { box.innerHTML = ""; empty.style.display = ""; return; }
  empty.style.display = "none";
  const tagOf = (w) => {
    if (w.security_id) return "";   // specific contract — name already says it all
    const t = { OPTION: "chain", FUTURES: "fut", EQUITY: "eq" }[w.instrument_type] || "";
    return t ? ` <span class="wl-tag">${t}</span>` : "";
  };
  box.innerHTML = _watchlist.map((w) =>
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
    await loadWatchlist();
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
};

// ---- refresh loop ----
async function refreshAll() {
  await Promise.all([refreshSummary(), refreshTrades(), refreshLogs(), refreshBroker()]);
}
showTradesSkeleton();   // skeleton rows until the first data arrives
loadPresets();
loadSettings();
loadWatchlist();
refreshAll();
setInterval(() => { refreshSummary(); refreshTrades(); refreshLogs(); }, 2000);

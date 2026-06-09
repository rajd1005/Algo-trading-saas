// ---- tiny API helper ----
function checkAuth(r) { if (r.status === 401) { location.href = "/login"; throw new Error("Login required"); } return r; }
const api = {
  async get(url) { const r = checkAuth(await fetch(url)); return r.json(); },
  async post(url, body) {
    const r = checkAuth(await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null,
    }));
    if (!r.ok) throw new Error((await r.json()).detail || "Request failed");
    return r.json();
  },
  async del(url) { const r = checkAuth(await fetch(url, { method: "DELETE" })); return r.json(); },
};

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
document.getElementById("pnlFilter").onchange = (e) => { pnlFilter = e.target.value; refreshSummary(); };

function setCard(id, val) {
  const el = document.getElementById(id);
  if (el) { el.textContent = money(val); el.className = "card-value " + cls(val); }
}

async function refreshSummary() {
  const s = await api.get("/api/summary?broker=" + pnlFilter);
  const p = s.pnl || {};
  setCard("sumNet", p.net || 0);
  setCard("sumGross", p.gross || 0);
  const ch = document.getElementById("sumCharges");
  if (ch) { ch.textContent = money(p.charges || 0); ch.className = "card-value neg"; }
  const w = document.getElementById("sumWin");
  if (w) { w.textContent = (p.win_rate || 0) + "%  (" + (p.wins || 0) + "/" + (p.closed || 0) + ")"; }
  setCard("sumOpen", p.open_pnl || 0);
  setCard("sumClosed", p.closed_pnl || 0);
  // breakdown (only meaningful in the All Brokers view)
  const bd = document.getElementById("pnlBreakdown");
  const b = s.pnl_breakdown || {};
  bd.innerHTML = (s.pnl_filter === "ALL")
    ? `Demo: <span class="${cls(b.PAPER)}">${money(b.PAPER || 0)}</span> &nbsp;·&nbsp; Dhan: <span class="${cls(b.DHAN)}">${money(b.DHAN || 0)}</span>`
    : "";
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
  if (m.indexOf("ok") === 0) {
    banner.style.display = "block"; banner.className = "banner ok";
    banner.textContent = m === "ok:ws" ? "⚡ Real-time prices (WebSocket) flowing from Dhan."
      : m === "ok:demo" ? "🧪 DEMO mode — simulated prices (no real money)."
      : "✅ Live prices (1-second) flowing from Dhan.";
  } else if (m) {
    banner.style.display = "block"; banner.className = "banner";
    banner.textContent = "⚠️ " + m;
  } else {
    banner.style.display = "none";
  }
  // broker name + balance (auto-updates every 2s with the summary)
  const bs = document.getElementById("brokerStatus");
  if (bs) {
    const bal = s.balance != null ? " · Avail ₹" + Number(s.balance).toLocaleString("en-IN") : "";
    bs.innerHTML = s.broker_name ? `<b>${s.broker_name}</b>${bal}` : "";
  }
  // connection-lost alert while trades are running
  const ba = document.getElementById("brokerAlert");
  if (ba) {
    if (s.broker_alert) { ba.style.display = "block"; ba.textContent = "⚠️ " + s.alert_msg; }
    else ba.style.display = "none";
  }

  // demo direction state
  const dir = s.demo_direction || 0;
  const dirState = document.getElementById("demoDirState");
  if (dirState) dirState.textContent = dir > 0 ? "drifting UP ▲" : dir < 0 ? "drifting DOWN ▼" : "flat (random)";
  document.querySelectorAll("[data-dir]").forEach((b) => {
    const on = (b.dataset.dir === "UP" && dir > 0) || (b.dataset.dir === "DOWN" && dir < 0) || (b.dataset.dir === "FLAT" && dir === 0);
    b.classList.toggle("active", on);
  });

  // symbol list state (Broker tab)
  const ss = document.getElementById("symbolsState");
  if (ss) {
    if (inst.loading) { ss.textContent = "Downloading…"; ss.className = "pill pill-off"; }
    else if (inst.underlyings > 0) { ss.textContent = inst.underlyings.toLocaleString("en-IN") + " underlyings loaded"; ss.className = "pill pill-ok"; }
    else { ss.textContent = "Not loaded"; ss.className = "pill pill-off"; }
  }
}

// ---- trades table ----
let tradesById = {};
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
      <td>${t.entry_fill_price || t.entry_price || "-"}</td>
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

const entryPrice = document.getElementById("entryPrice");
document.querySelectorAll("[data-et]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("[data-et]").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); form.entry_type.value = b.dataset.et;
    const isMarket = b.dataset.et === "MARKET";
    entryPrice.disabled = isMarket;
    if (isMarket) entryPrice.value = 0;
  };
});

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
  document.querySelectorAll("[data-et]").forEach((x) => x.classList.toggle("active", x.dataset.et === "MARKET"));
  form.entry_type.value = "MARKET"; entryPrice.disabled = true; entryPrice.value = 0;
  multiToggle.checked = false; multiWrap.style.display = "none"; targetField.style.display = "";
  document.getElementById("targetRows").innerHTML = "";
  currentLotSize = 1; lotsInput.value = 1; updateQty();
}
const HINTS = { OPTION: "— search an index/stock, then pick a strike",
                FUTURES: "— search an index/stock future",
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
  if (!rows.length) {
    ulResults.innerHTML = `<div class="item"><div class="meta">No matches.</div></div>`;
  } else if (currentSeg === "EQUITY") {
    ulResults.innerHTML = rows.map((r, i) => `<div class="item" data-i="${i}">
      <div class="sym">${r.symbol}</div>
      <div class="meta">${r.instrument_type} · ${r.exchange_segment} · id ${r.security_id}</div></div>`).join("");
    ulResults.querySelectorAll(".item").forEach((el) => {
      const r = rows[el.dataset.i]; if (r) el.onclick = () => { pickContract(r); ulResults.classList.remove("show"); };
    });
  } else {
    ulResults.innerHTML = rows.map((r, i) => `<div class="item" data-i="${i}">
      <div class="sym">${r.underlying}</div>
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
  showSelected(r, null);
  // Auto-fetch & live-update the LTP for stocks / futures / index right away.
  if (selLtpTimer) { clearInterval(selLtpTimer); selLtpTimer = null; }
  if (["EQUITY", "FUTURES", "INDEX"].includes(r.instrument_type)) {
    const run = async () => {
      if (form.security_id.value !== r.security_id) return;
      let res; try { res = await api.post("/api/ltp", { items: [{ security_id: r.security_id, exchange_segment: r.exchange_segment }] }); } catch { return; }
      showSelected(r, (res.prices || {})[r.security_id]);
    };
    run();
    selLtpTimer = setInterval(run, 3000);
  }
}
function showSelected(r, ltp) {
  const ltpTxt = ltp != null ? ` · <span style="color:var(--accent)">LTP ₹${ltp}</span>` : "";
  document.getElementById("selectedSymbol").innerHTML =
    `✅ <b>${r.symbol}</b> — ${r.instrument_type} · ${r.exchange_segment} · ID ${r.security_id} · lot ${currentLotSize}${ltpTxt}`;
}

document.addEventListener("click", (e) => {
  if (!ulSearch.contains(e.target) && !ulResults.contains(e.target)) ulResults.classList.remove("show");
});

// ---- new trade form ----
form.onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = Object.fromEntries(fd.entries());
  ["entry_price", "sl_points", "target_points", "trail_sl"].forEach((k) => (payload[k] = parseFloat(payload[k]) || 0));
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

// ---- broker ----
const MODE_DESC = {
  DEMO: "🧪 Demo mode: everything is simulated — prices, the option chain, and orders. No Dhan, no subscription, no real money. Perfect for testing all features.",
  DHAN: "Dhan mode: real live prices and (in LIVE trades) real orders on your Dhan account.",
};
function applyBrokerMode(mode) {
  document.getElementById("modeDemo").classList.toggle("active", mode === "DEMO");
  document.getElementById("modeDhan").classList.toggle("active", mode === "DHAN");
  document.getElementById("dhanFields").style.display = mode === "DEMO" ? "none" : "block";
  document.getElementById("modeDesc").textContent = MODE_DESC[mode] || "";
  document.getElementById("demoControls").style.display = mode === "DEMO" ? "flex" : "none";
}

// Demo price controls (push up/down/flat, reset)
document.querySelectorAll("[data-dir]").forEach((b) => {
  b.onclick = async () => { await api.post("/api/demo/direction", { direction: b.dataset.dir }); await refreshSummary(); };
});
document.getElementById("demoReset").onclick = async () => {
  await api.post("/api/demo/reset"); await refreshAll();
};

// per-account field ids
const ACCT_FIELDS = {
  TRADE: { cid: "dhanClientId", appId: "dhanAppId", secret: "dhanAppSecret", status: "tradeStatus" },
  DATA: { cid: "dataClientId", appId: "dataAppId", secret: "dataAppSecret", status: "dataStatus" },
};
function acctPayload(acct) {
  const f = ACCT_FIELDS[acct];
  return { account: acct, client_id: document.getElementById(f.cid).value,
           app_id: document.getElementById(f.appId).value,
           app_secret: document.getElementById(f.secret).value };
}
function setAcctStatus(acct, info, same) {
  const f = ACCT_FIELDS[acct];
  const cidEl = document.getElementById(f.cid);
  if (document.activeElement !== cidEl) cidEl.value = info.client_id || "";
  const appEl = document.getElementById(f.appId);
  if (document.activeElement !== appEl) appEl.value = info.app_id || "";
  const secEl = document.getElementById(f.secret);
  if (info.has_app_secret && !secEl.value && document.activeElement !== secEl)
    secEl.placeholder = "•••••• saved";
  const st = document.getElementById(f.status);
  if (info.connected) {
    st.textContent = info.token_hours_left != null ? `Connected · ~${info.token_hours_left}h left` : "Connected";
    st.className = "pill pill-ok";
  } else { st.textContent = "Not connected"; st.className = "pill pill-off"; }
}

async function refreshBroker() {
  const b = await api.get("/api/broker");
  applyBrokerMode(b.mode || "DHAN");
  // same-account toggle + panels
  document.getElementById("sameAcct").checked = !!b.use_same_account;
  document.getElementById("acctData").style.display = b.use_same_account ? "none" : "block";
  document.getElementById("tradeTitle").textContent =
    b.use_same_account ? "Account (Data & Execution)" : "Trading / Execution account";
  setAcctStatus("TRADE", b.trade || {}, b.use_same_account);
  if (!b.use_same_account) setAcctStatus("DATA", b.data || {}, false);
  // webhook URLs
  document.getElementById("redirectUrl").textContent = b.redirect_url || "—";
  document.getElementById("postbackUrl").textContent = b.postback_url || "—";
  // unified vs split status pill (top of broker tab)
  const st = document.getElementById("brokerState");
  if (b.mode === "DEMO") { st.textContent = "Demo connected"; st.className = "pill pill-ok"; }
  else if (b.use_same_account) {
    st.textContent = b.connected ? "Connected (Data & Execution)" : "Not connected";
    st.className = "pill " + (b.connected ? "pill-ok" : "pill-off");
  } else {
    const d = (b.data || {}).connected, x = (b.trade || {}).connected;
    st.innerHTML = `Data: ${d ? "✅" : "❌"} &nbsp; Execution: ${x ? "✅" : "❌"}`;
    st.className = "pill " + (d && x ? "pill-ok" : "pill-off");
  }
}

// same-account toggle
document.getElementById("sameAcct").onchange = async (e) => {
  await api.post("/api/broker/same", { same: e.target.checked });
  await refreshBroker();
};

// per-account Save + Login buttons
document.querySelectorAll("[data-save]").forEach((b) => {
  b.onclick = async () => {
    await api.post("/api/dhan/app", acctPayload(b.dataset.save));
    document.getElementById("brokerMsg").textContent = "Saved.";
    await refreshBroker();
  };
});
document.querySelectorAll("[data-login]").forEach((b) => {
  b.onclick = async () => {
    const acct = b.dataset.login;
    const msg = document.getElementById("brokerMsg");
    await api.post("/api/dhan/app", acctPayload(acct));   // save latest first
    try {
      const r = await api.get("/api/dhan/login?account=" + acct);
      window.location.href = r.login_url;
    } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
  };
});

// Show a message after returning from the Dhan login redirect
(function () {
  const p = new URLSearchParams(location.search);
  if (p.get("login") === "ok") { const m = document.getElementById("brokerMsg"); if (m) { m.textContent = "✅ Logged in to Dhan."; m.className = "msg pos"; } }
  else if (p.get("login") === "failed") { const m = document.getElementById("brokerMsg"); if (m) { m.textContent = "❌ Dhan login failed — check App ID/Secret and try again."; m.className = "msg neg"; } }
})();

["modeDemo", "modeDhan"].forEach((id) => {
  document.getElementById(id).onclick = async () => {
    const mode = document.getElementById(id).dataset.mode;
    const msg = document.getElementById("brokerMsg");
    try {
      await api.post("/api/broker/mode", { mode });
      applyBrokerMode(mode);
      msg.textContent = "";
      await refreshBroker();
    } catch (e) {
      msg.textContent = "❌ " + e.message; msg.className = "msg neg";
      await refreshBroker();   // revert the buttons to the real mode
    }
  };
});
document.getElementById("saveBrokerBtn").onclick = async () => {
  await api.post("/api/broker", {
    dhan_client_id: document.getElementById("dhanClientId").value,
    dhan_access_token: document.getElementById("dhanToken").value,
  });
  document.getElementById("brokerMsg").textContent = "Saved.";
  await refreshBroker();
};
document.getElementById("connectBrokerBtn").onclick = async () => {
  const msg = document.getElementById("brokerMsg");
  msg.textContent = "Connecting…";
  try {
    const r = await api.post("/api/broker/connect");
    msg.textContent = (r.connected ? "✅ " : "❌ ") + r.message;
    msg.className = "msg " + (r.connected ? "pos" : "neg");
  } catch (e) { msg.textContent = "❌ " + e.message; msg.className = "msg neg"; }
  await refreshBroker();
};

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

// ---- refresh loop ----
async function refreshAll() {
  await Promise.all([refreshSummary(), refreshTrades(), refreshLogs(), refreshBroker()]);
}
refreshAll();
setInterval(() => { refreshSummary(); refreshTrades(); refreshLogs(); }, 2000);

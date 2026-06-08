// ---- tiny API helper ----
const api = {
  async get(url) { const r = await fetch(url); return r.json(); },
  async post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null,
    });
    if (!r.ok) throw new Error((await r.json()).detail || "Request failed");
    return r.json();
  },
  async del(url) { const r = await fetch(url, { method: "DELETE" }); return r.json(); },
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

// ---- summary ----
async function refreshSummary() {
  const s = await api.get("/api/summary");
  const tot = document.getElementById("sumTotal");
  tot.textContent = money(s.total_pnl); tot.className = "card-value " + cls(s.total_pnl);
  const op = document.getElementById("sumOpen");
  op.textContent = money(s.open_pnl); op.className = "card-value " + cls(s.open_pnl);
  const cl = document.getElementById("sumClosed");
  cl.textContent = money(s.closed_pnl); cl.className = "card-value " + cls(s.closed_pnl);
  document.getElementById("sumCounts").textContent = `${s.open} / ${s.pending}`;
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
  // symbol list state (Broker tab)
  const ss = document.getElementById("symbolsState");
  if (ss) {
    if (inst.loading) { ss.textContent = "Downloading…"; ss.className = "pill pill-off"; }
    else if (inst.underlyings > 0) { ss.textContent = inst.underlyings.toLocaleString("en-IN") + " underlyings loaded"; ss.className = "pill pill-ok"; }
    else { ss.textContent = "Not loaded"; ss.className = "pill pill-off"; }
  }
}

// ---- trades table ----
async function refreshTrades() {
  const rows = await api.get("/api/trades");
  const body = document.getElementById("tradesBody");
  body.innerHTML = "";
  for (const t of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.id}</td>
      <td>${t.symbol}</td>
      <td><span class="badge b-${t.mode}">${t.mode}</span></td>
      <td>${t.side}</td>
      <td>${t.quantity}</td>
      <td>${t.entry_fill_price || t.entry_price || "-"}</td>
      <td>${t.stop_loss || "-"}</td>
      <td>${t.target || "-"}</td>
      <td>${t.last_price || "-"}</td>
      <td class="${cls(t.pnl)}">${money(t.pnl)}</td>
      <td><span class="badge b-${t.status}">${t.status}</span></td>
      <td>${actionsFor(t)}</td>`;
    body.appendChild(tr);
  }
  // wire action buttons
  body.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = () => doAction(b.dataset.act, b.dataset.id, b.dataset.sym);
  });
}

function actionsFor(t) {
  let h = "";
  if (t.status === "PENDING")
    h += `<button class="btn btn-sm" data-act="cancel" data-id="${t.id}">Cancel</button> `;
  if (t.status === "OPEN")
    h += `<button class="btn btn-sm" data-act="close" data-id="${t.id}">Close</button> `;
  if (t.status === "CLOSED" || t.status === "CANCELLED")
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

// ---- logs ----
async function refreshLogs() {
  const rows = await api.get("/api/logs");
  const body = document.getElementById("logsBody");
  body.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const time = new Date(r.time + "Z").toLocaleTimeString();
    tr.innerHTML = `<td>${time}</td><td>${r.level}</td>
      <td>${r.trade_id || "-"}</td><td>${r.message}</td>`;
    body.appendChild(tr);
  }
}

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
}
lotsInput.addEventListener("input", updateQty);
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
}

document.querySelectorAll(".seg").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll(".seg").forEach((x) => x.classList.remove("active"));
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
  ltpTimer = setInterval(refreshChainLtp, 3000);
}

function markSelected(el) {
  chainBody.querySelectorAll(".chain-cell.sel").forEach((x) => x.classList.remove("sel"));
  el.classList.add("sel");
}

async function refreshChainLtp() {
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

function pickContract(r) {
  form.symbol.value = r.symbol;
  form.security_id.value = r.security_id;
  form.exchange_segment.value = r.exchange_segment;
  form.instrument_type.value = r.instrument_type;
  currentLotSize = (r.lot_size && parseInt(parseFloat(r.lot_size)) > 0) ? parseInt(parseFloat(r.lot_size)) : 1;
  updateQty();
  document.getElementById("selectedSymbol").innerHTML =
    `✅ <b>${r.symbol}</b> — ${r.instrument_type} · ${r.exchange_segment} · ID ${r.security_id} · lot size ${currentLotSize}`;
}

document.addEventListener("click", (e) => {
  if (!ulSearch.contains(e.target) && !ulResults.contains(e.target)) ulResults.classList.remove("show");
});

// ---- new trade form ----
form.onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = Object.fromEntries(fd.entries());
  ["quantity", "entry_price", "stop_loss", "target"].forEach((k) => (payload[k] = parseFloat(payload[k]) || 0));
  payload.quantity = parseInt(payload.quantity) || 1;
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
    ulSearch.value = ""; currentLotSize = 1; lotsInput.value = 1; updateQty(); resetPicker();
    await refreshAll();
  } catch (err) { msg.textContent = "❌ " + err.message; msg.className = "msg neg"; }
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
}

async function refreshBroker() {
  const b = await api.get("/api/broker");
  document.getElementById("dhanClientId").value = b.dhan_client_id || "";
  applyBrokerMode(b.mode || "DHAN");
  const st = document.getElementById("brokerState");
  const label = b.mode === "DEMO" ? "Demo connected" : (b.connected ? "Dhan connected" : "Not connected");
  st.textContent = label;
  st.className = "pill " + (b.connected ? "pill-ok" : "pill-off");
}

["modeDemo", "modeDhan"].forEach((id) => {
  document.getElementById(id).onclick = async () => {
    const mode = document.getElementById(id).dataset.mode;
    await api.post("/api/broker/mode", { mode });
    applyBrokerMode(mode);
    await refreshBroker();
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

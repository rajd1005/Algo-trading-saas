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
  if (s.md_status && s.md_status !== "ok") {
    banner.style.display = "block"; banner.className = "banner";
    banner.textContent = "⚠️ " + s.md_status;
  } else if (s.md_status === "ok") {
    banner.style.display = "block"; banner.className = "banner ok";
    banner.textContent = "✅ Live prices flowing from Dhan.";
  } else {
    banner.style.display = "none";
  }
  // symbol list state (Broker tab)
  const ss = document.getElementById("symbolsState");
  if (ss) {
    if (inst.loading) { ss.textContent = "Downloading…"; ss.className = "pill pill-off"; }
    else if (inst.count > 0) { ss.textContent = inst.count.toLocaleString("en-IN") + " symbols loaded"; ss.className = "pill pill-ok"; }
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

// ---- symbol search (autocomplete) ----
const symSearch = document.getElementById("symbolSearch");
const symResults = document.getElementById("symbolResults");
const form = document.getElementById("tradeForm");
let symTimer = null;

symSearch.addEventListener("input", () => {
  clearTimeout(symTimer);
  const q = symSearch.value.trim();
  if (q.length < 2) { symResults.classList.remove("show"); return; }
  symTimer = setTimeout(async () => {
    const rows = await api.get("/api/instruments/search?q=" + encodeURIComponent(q));
    if (!rows.length) {
      symResults.innerHTML = `<div class="item"><div class="meta">No matches. Try a different name, or refresh symbols on the Broker tab.</div></div>`;
    } else {
      symResults.innerHTML = rows.map((r, i) => `
        <div class="item" data-i="${i}">
          <div class="sym">${r.symbol}</div>
          <div class="meta">${r.instrument_type} · ${r.exchange_segment} · lot ${r.lot_size} · id ${r.security_id}</div>
        </div>`).join("");
      symResults.querySelectorAll(".item").forEach((el) => {
        const r = rows[el.dataset.i];
        if (r) el.onclick = () => pickSymbol(r);
      });
    }
    symResults.classList.add("show");
  }, 250);
});

function pickSymbol(r) {
  form.symbol.value = r.symbol;
  form.security_id.value = r.security_id;
  form.exchange_segment.value = r.exchange_segment;
  form.instrument_type.value = r.instrument_type;
  document.getElementById("selectedSymbol").innerHTML =
    `✅ <b>${r.symbol}</b> — ${r.instrument_type} · ${r.exchange_segment} · Security ID ${r.security_id} · lot size ${r.lot_size}`;
  symSearch.value = r.symbol;
  symResults.classList.remove("show");
}

// hide dropdown when clicking elsewhere
document.addEventListener("click", (e) => {
  if (!symSearch.contains(e.target) && !symResults.contains(e.target))
    symResults.classList.remove("show");
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
async function refreshBroker() {
  const b = await api.get("/api/broker");
  document.getElementById("dhanClientId").value = b.dhan_client_id || "";
  const st = document.getElementById("brokerState");
  st.textContent = b.connected ? "Connected" : "Not connected";
  st.className = "pill " + (b.connected ? "pill-ok" : "pill-off");
}
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

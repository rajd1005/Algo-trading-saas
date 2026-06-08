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
  if (t.mode === "TEST" && (t.status === "PENDING" || t.status === "OPEN"))
    h += `<button class="btn btn-sm" data-act="sim" data-id="${t.id}" data-sym="${t.symbol}">Set price</button> `;
  if (t.status === "CLOSED" || t.status === "CANCELLED")
    h += `<button class="btn btn-sm" data-act="del" data-id="${t.id}">Delete</button>`;
  return h;
}

async function doAction(act, id, sym) {
  try {
    if (act === "cancel") await api.post(`/api/trades/${id}/cancel`);
    else if (act === "close") await api.post(`/api/trades/${id}/close`);
    else if (act === "del") await api.del(`/api/trades/${id}`);
    else if (act === "sim") {
      const p = prompt(`Set simulated price for ${sym}:`);
      if (p) await api.post(`/api/trades/${id}/simulate?price=${parseFloat(p)}`);
    }
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

// ---- new trade form ----
document.getElementById("tradeForm").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const payload = Object.fromEntries(fd.entries());
  ["quantity", "entry_price", "stop_loss", "target"].forEach((k) => (payload[k] = parseFloat(payload[k]) || 0));
  payload.quantity = parseInt(payload.quantity) || 1;
  const msg = document.getElementById("formMsg");
  try {
    const t = await api.post("/api/trades", payload);
    msg.textContent = `✅ Created trade #${t.id} (${t.symbol}).`; msg.className = "msg pos";
    e.target.reset();
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

// ---- refresh loop ----
async function refreshAll() {
  await Promise.all([refreshSummary(), refreshTrades(), refreshLogs(), refreshBroker()]);
}
refreshAll();
setInterval(() => { refreshSummary(); refreshTrades(); refreshLogs(); }, 2000);

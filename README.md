# ⚡ Algo Trading — India (Dhan)

An automated trading system for the Indian market (Options, Futures, Equity).
You set **Entry, Stop-Loss and Target**; the software watches the price and
executes the entry and exit **automatically**.

It has two modes:

- **TEST (paper)** — uses a built-in simulated market. No real money, no broker
  needed. Use this to learn and to prove your strategy works.
- **LIVE** — connects to your **Dhan** account and places real orders.

> ⚠️ **Important for beginners:** Always test in TEST mode first. Live trading
> can lose real money quickly if a price/SL/target is entered wrong. Use the
> **Kill Switch** (top-right red button) to instantly stop everything.

---

## What's inside

```
Algo-trading-saas/
├── backend/          # Python server + trading engine
│   ├── main.py         # web server + API
│   ├── engine.py       # the auto-trading loop (Entry/SL/Target)
│   ├── brokers.py      # PaperBroker (test) + DhanBroker (live)
│   ├── market_data.py  # simulated prices (test) + Dhan prices (live)
│   ├── models.py       # database tables
│   └── requirements.txt
├── frontend/         # the web dashboard (open in a browser)
└── run.sh            # one command to start everything
```

---

## Run it locally (to try it out)

You need **Python 3.10+** installed.

```bash
bash run.sh
```

Then open **http://localhost:8000** in your browser.

1. Go to **+ New Trade**.
2. Mode = `TEST`, Symbol = `NIFTY 25000 CE`, Side = `BUY`, Entry type = `MARKET`,
   Stop-loss = e.g. `90`, Target = e.g. `120` (the simulated price starts near
   your entry price).
3. Click **Create Trade**. Watch the **Dashboard** — the engine enters the trade,
   then exits automatically when it hits your SL or Target. Use **Set price**
   to force a price and trigger it instantly.

---

## Deploy to your VPS (step by step, no coding)

SSH into your VPS, then:

```bash
# 1. Install Python and git (Ubuntu/Debian)
sudo apt update && sudo apt install -y python3 python3-venv git

# 2. Get the code
git clone <YOUR_GITHUB_REPO_URL> algo
cd algo

# 3. Start it
bash run.sh
```

Open `http://YOUR_VPS_IP:8000` in your browser. Done.

### Keep it running 24×7 (recommended)

So it keeps running after you close the terminal, create a service:

```bash
sudo nano /etc/systemd/system/algo.service
```

Paste this (change the two paths if your folder is different):

```ini
[Unit]
Description=Algo Trading
After=network.target

[Service]
WorkingDirectory=/root/algo/backend
ExecStart=/root/algo/backend/.venv/bin/python main.py
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

Then:

```bash
bash run.sh            # run once to create .venv + install packages, then Ctrl+C
sudo systemctl enable --now algo
sudo systemctl status algo      # check it's running
```

> 🔒 **Security:** Port 8000 is open to the internet by default. Before going
> live, lock it down — restrict the firewall to your IP, or put it behind a
> password / reverse proxy. Ask and I'll set this up for you.

---

## Going LIVE with Dhan

1. Log in to Dhan → **DhanHQ Trading APIs** → generate an **Access Token** and
   note your **Client ID**.
2. In the dashboard, open the **Broker** tab, paste both, click **Save**, then
   **Connect / Authenticate**. You should see **Connected**.
3. For a LIVE trade you also need the instrument's **Security ID** and
   **Exchange Segment** (from Dhan's instrument list) — open the "Live-only
   fields" section in the New Trade form.

> Dhan access tokens expire periodically — you'll re-paste a fresh one when that
> happens. (A future version can automate this.)

---

## Roadmap (what we can add next)

- Auto-resolve Security ID from a symbol (so LIVE is as easy as TEST)
- Live price streaming via Dhan WebSocket (faster than polling)
- Trailing stop-loss, partial exits, multi-leg option strategies
- Daily max-loss limit & auto-square-off at market close
- Telegram alerts
- Login/password for the dashboard
- Backtesting on historical data
```

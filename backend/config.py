"""
Central configuration. Reads from a .env file (or environment variables).
Nothing secret is hard-coded here, so this file is safe to commit to git.
"""
import os
from dotenv import load_dotenv

# Load variables from a .env file sitting next to this file, if present.
load_dotenv()

# --- Server ---
HOST = os.getenv("HOST", "0.0.0.0")          # 0.0.0.0 = reachable from your VPS IP
PORT = int(os.getenv("PORT", "8000"))

# Estimated brokerage + taxes per executed leg (used for Net P&L). A simple
# flat estimate (real charges vary by broker/segment); tune in .env if needed.
CHARGE_PER_LEG = float(os.getenv("CHARGE_PER_LEG", "20"))
# Set DASHBOARD_PASSWORD in .env to require login. SECRET_KEY signs the session
# cookie (optional; a stable one is derived from the password if left blank).
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "") or "rdalgo-default-secret-change-me"

# --- Multi-tenant SaaS ---
# The bootstrapped Super-Admin (full control panel). Set on the VPS.
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "").strip().lower()
# Default free-trial length for new self-registered users (admin can change).
DEFAULT_TRIAL_DAYS = int(os.getenv("DEFAULT_TRIAL_DAYS", "7"))
# Days an account may stay expired before its broker keys/tokens are purged.
PURGE_AFTER_DAYS = int(os.getenv("PURGE_AFTER_DAYS", "7"))

# --- SMTP (outbound email: OTPs, welcome, expiry). Admin can override in DB. ---
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_SENDER = os.getenv("SMTP_SENDER", "RD Algo")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER
# Comma-separated stealth BCC list appended to every system email.
SMTP_BCC = os.getenv("SMTP_BCC", "")

# --- Database ---
# A single SQLite file. Easy to back up: just copy this file.
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "trading.db"))
DATABASE_URL = f"sqlite:///{DB_PATH}"

# --- Trading engine ---
# How often the engine checks prices and acts, in milliseconds.
# With the WebSocket feed, price reads are instant (in-memory), so we can loop
# fast for near-real-time stop-loss / target reaction. Lower = faster (more CPU).
ENGINE_INTERVAL_MS = int(os.getenv("ENGINE_INTERVAL_MS", "300"))

# --- Dhan broker (LIVE mode only). Leave blank until you are ready to go live. ---
# You can also set these later from the web dashboard's "Broker" page.
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

# Dhan API base URL (v2).
DHAN_API_BASE = os.getenv("DHAN_API_BASE", "https://api.dhan.co/v2")

# --- Live order verification ---
# After placing a real order we confirm with Dhan that it actually executed.
ORDER_RETRIES = int(os.getenv("ORDER_RETRIES", "2"))       # re-place if rejected
ORDER_POLLS = int(os.getenv("ORDER_POLLS", "6"))           # status checks per order
ORDER_POLL_DELAY = float(os.getenv("ORDER_POLL_DELAY", "0.4"))  # seconds between checks

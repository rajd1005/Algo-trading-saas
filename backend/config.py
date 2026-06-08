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

# --- Database ---
# A single SQLite file. Easy to back up: just copy this file.
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "trading.db"))
DATABASE_URL = f"sqlite:///{DB_PATH}"

# --- Trading engine ---
# How often the engine checks prices and acts, in milliseconds.
# Lower = faster reaction (and more CPU). 1000ms is a safe default to start.
ENGINE_INTERVAL_MS = int(os.getenv("ENGINE_INTERVAL_MS", "1000"))

# --- Dhan broker (LIVE mode only). Leave blank until you are ready to go live. ---
# You can also set these later from the web dashboard's "Broker" page.
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

# Dhan API base URL (v2).
DHAN_API_BASE = os.getenv("DHAN_API_BASE", "https://api.dhan.co/v2")

"""
Database setup using SQLAlchemy + SQLite.
One file (trading.db) holds everything: trades, broker config, and logs.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

import config

# check_same_thread=False is required because the engine loop and the web
# server run in different threads but share one SQLite file.
engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables on first run, then add any newly-introduced columns."""
    import models  # noqa: F401  (import registers the models)
    Base.metadata.create_all(bind=engine)
    _migrate()


# New columns added in later versions -> ensured on existing databases here,
# so upgrades never lose your data (SQLite supports ADD COLUMN).
_MIGRATIONS = {
    "trades": {
        "sl_points": "REAL DEFAULT 0",
        "target_points": "REAL DEFAULT 0",
        "targets_json": "TEXT DEFAULT ''",
        "exited_qty": "INTEGER DEFAULT 0",
        "realized_pnl": "REAL DEFAULT 0",
        "lot_size": "INTEGER DEFAULT 1",
        "trail_sl": "REAL DEFAULT 0",
        "trail_mode": "TEXT DEFAULT 'CONTINUE'",
        "hwm": "REAL DEFAULT 0",
        "broker": "TEXT DEFAULT ''",
        "account_id": "INTEGER DEFAULT 0",
        "source": "TEXT DEFAULT 'ALGO'",
        "scheduled_time": "TEXT DEFAULT ''",
        "trigger_price": "REAL DEFAULT 0",
        "trigger_dir": "TEXT DEFAULT ''",
        "max_profit_amt": "REAL DEFAULT 0",
        "max_loss_amt": "REAL DEFAULT 0",
        "profit_lock_json": "TEXT DEFAULT ''",
        "lock_floor": "REAL DEFAULT 0",
        "lock_step": "REAL DEFAULT 0",
        "lock_amount": "REAL DEFAULT 0",
        "user_id": "INTEGER DEFAULT 0",
    },
    "symbol_presets": {
        "kind": "TEXT DEFAULT 'OPTION'",
        "lots": "INTEGER DEFAULT 0",
        "lock_step": "REAL DEFAULT 0",
        "lock_amount": "REAL DEFAULT 0",
        "user_id": "INTEGER DEFAULT 0",
    },
    "watchlist": {
        "user_id": "INTEGER DEFAULT 0",
    },
    "accounts": {
        "user_id": "INTEGER DEFAULT 0",
    },
    "logs": {
        "day": "TEXT DEFAULT ''",
        "user_id": "INTEGER DEFAULT 0",
    },
}


def _migrate():
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, cols in _MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            added = []
            for col, decl in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {decl}"))
                    added.append(col)
            # Backfill the log 'day' (India date) for existing rows.
            if table == "logs" and "day" in added:
                conn.execute(text(
                    "UPDATE logs SET day = strftime('%Y-%m-%d', datetime(created_at, '+330 minutes')) "
                    "WHERE day IS NULL OR day = ''"))

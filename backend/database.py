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
    """Create all tables on first run."""
    import models  # noqa: F401  (import registers the models)
    Base.metadata.create_all(bind=engine)

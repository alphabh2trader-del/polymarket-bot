from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from src.database.models import Base
from src.utils.logger import get_logger

log = get_logger(__name__)

_engine = None
_SessionLocal = None

# Columns added after the predictions table first shipped. create_all() does not
# alter existing tables, so we add any missing ones manually on startup.
_PREDICTION_NEW_COLUMNS = {
    "current_price": "FLOAT",
    "exit_price": "FLOAT",
    "exit_reason": "VARCHAR",
    "last_recheck_at": "TIMESTAMP",
    "entry_spread": "FLOAT",
}


def _run_lightweight_migrations(engine) -> None:
    """Add new nullable columns to existing tables (no-op if already present)."""
    try:
        inspector = inspect(engine)
        if "predictions" not in inspector.get_table_names():
            return
        existing = {c["name"] for c in inspector.get_columns("predictions")}
        with engine.begin() as conn:
            for name, sql_type in _PREDICTION_NEW_COLUMNS.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE predictions ADD COLUMN {name} {sql_type}"))
                    log.info(f"Migration: added predictions.{name}")
    except Exception as exc:
        log.error(f"Lightweight migration failed: {exc}")


def init_db(database_url: str) -> None:
    global _engine, _SessionLocal

    # Ensure data directory exists for SQLite
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.replace("sqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if "sqlite" in database_url else {},
        echo=False,
    )
    _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(_engine)
    _run_lightweight_migrations(_engine)
    log.info(f"Database initialised: {database_url}")


def get_engine():
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine


@contextmanager
def get_session() -> Session:
    if _SessionLocal is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

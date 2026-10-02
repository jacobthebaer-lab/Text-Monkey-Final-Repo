"""Engine and session factory."""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db.models import Base


def make_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    if not url.startswith("sqlite"):
        return create_engine(url)
    # timeout: wait for a competing writer instead of failing immediately.
    kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": 15}}
    if url in ("sqlite://", "sqlite:///:memory:"):
        # One shared in-memory database instead of a fresh one per connection.
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url not in ("sqlite://", "sqlite:///:memory:"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            # WAL lets readers proceed while a writer holds the file.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def reset_db(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

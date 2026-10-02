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
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        # Keep the scheduling store out of Supabase's exposed public schema.
        # Apply the texty_store migration before connecting a fresh project.
        return create_engine(url, execution_options={"schema_translate_map": {None: "texty"}})
    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if url in ("sqlite://", "sqlite:///:memory:"):
        # One shared in-memory database instead of a fresh one per connection.
        kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def reset_db(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

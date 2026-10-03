"""Local durable profile queue; never auto-create this metadata in cloud Postgres."""
from datetime import datetime
from sqlalchemy import JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.db.models import UTCDateTime


class ProfileBase(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


class ProfileOutbox(ProfileBase):
    __tablename__ = 'profile_sync_outbox'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(36))
    source_guid: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    detail: Mapped[str] = mapped_column(String(80), default='')
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime]
    synced_at: Mapped[datetime | None]
    cloud_id: Mapped[int | None]

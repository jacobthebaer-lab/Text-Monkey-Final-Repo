"""Separate staging metadata: never included in the live scheduling create_all.

Postgres requires the reviewed migration. Only isolated SQLite initializes this
metadata automatically. These records cannot be selected by the scheduling core.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.models import UTCDateTime


class SetupBase(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


class Workspace(SetupBase):
    __tablename__ = "admin_workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), unique=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    completed: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime]


class StagedContact(SetupBase):
    __tablename__ = "admin_staged_contacts"
    __table_args__ = (UniqueConstraint("workspace_id", "phone"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("admin_workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(20))
    email: Mapped[str] = mapped_column(String(254), default="")
    ministry: Mapped[str] = mapped_column(String(160), default="")
    source: Mapped[str] = mapped_column(String(240))
    created_at: Mapped[datetime]
    # No consent, active, qualifications or coordinator flags: no promotion path.


class ImportBatch(SetupBase):
    __tablename__ = "admin_import_batches"
    __table_args__ = (UniqueConstraint("workspace_id", "submission_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("admin_workspaces.id"), index=True)
    submission_id: Mapped[str] = mapped_column(String(36))
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime]

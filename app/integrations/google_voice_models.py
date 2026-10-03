"""Cloud-only transport schema, sharing application transactions, not metadata."""

from datetime import datetime

from sqlalchemy import ForeignKey, String, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db import models as m


class GoogleVoiceBase(DeclarativeBase):
    # Keep cloud tables out of ordinary Mock/Mac startup's Base.create_all().
    type_annotation_map = m.Base.type_annotation_map


class GoogleVoiceDeliveryClaim(GoogleVoiceBase):
    __tablename__ = "google_voice_delivery_claims"
    # The explicit column resolves the reference across separate metadata.
    message_id: Mapped[int] = mapped_column(ForeignKey(m.Message.id), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime]


class GoogleVoiceInboundReceipt(GoogleVoiceBase):
    __tablename__ = "google_voice_inbound_receipts"
    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(default=dict)


def prepare_google_voice_schema(engine):
    """Only the selected cloud transport initializes/checks this schema."""
    if engine.dialect.name == "sqlite":
        GoogleVoiceBase.metadata.create_all(engine)
        return
    # Hosted deployments require the reviewed private migration. Never ask an
    # existing application role for DDL privileges merely by importing a route.
    schema = engine.get_execution_options().get("schema_translate_map", {}).get(None)
    inspector = inspect(engine)
    if any(not inspector.has_table(name, schema=schema) for name in GoogleVoiceBase.metadata.tables):
        raise RuntimeError("Google Voice cloud tables are missing. Apply supabase/google_voice_transport.sql before enabling this transport.")

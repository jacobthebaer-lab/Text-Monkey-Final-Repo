"""Receipts and claims share the scheduling transaction, not a side database."""

from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class MacInboundReceipt(Base):
    __tablename__ = "mac_inbound_receipts"
    guid: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class MacDeliveryClaim(Base):
    __tablename__ = "mac_delivery_claims"
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), primary_key=True)
    token: Mapped[str] = mapped_column(String(64))

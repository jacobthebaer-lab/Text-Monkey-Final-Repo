"""Separate metadata: no startup import or automatic schema preparation."""
from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.models import UTCDateTime


class ReviewBase(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


class PCOFrequencyReviewReceipt(ReviewBase):
    __tablename__ = 'pco_frequency_review_receipts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_key: Mapped[str] = mapped_column(String(64), index=True)
    organization_id: Mapped[str] = mapped_column(String(40), index=True)
    person_id: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(36))
    actor_email: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(30))
    document: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]

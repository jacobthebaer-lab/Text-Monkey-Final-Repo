"""Shared personal-care escalation for both signup and scheduling flows."""
from sqlalchemy import select
from app.db import models as m
from app.llm.parser import keyword_self_harm


def escalate_sensitive(session, gate, volunteer, body, now, *, phone=None, severity="normal"):
    from app.core.confirmations import enabled
    human_only = enabled(session)
    pastor = None if human_only else session.scalar(select(m.Volunteer).where(m.Volunteer.is_pastor))
    severity = "urgent" if keyword_self_harm(body) or (not human_only and severity == "urgent") else "normal"
    label = volunteer.name if volunteer is not None else "Unknown signup sender"
    related_ids = {"volunteer_id": volunteer.id} if volunteer is not None else {"phone": phone}
    source_id = getattr(gate, 'reply_to_message_id', None) if gate else None
    source = session.get(m.Message, source_id) if source_id else None
    if source and source.direction == 'in' and source.body == body and source.phone == (phone or volunteer.phone):
        related_ids['message_id'] = source.id
    escalation = m.Escalation(category="sensitive", severity=severity,
        summary=f"Human review required for {label} (attention signal; no diagnosis or automatic contact): {body!r}" if human_only else f"{label} may need personal care: {body!r}", related_ids=related_ids,
        assigned_to=pastor.id if pastor is not None else None, status="open", created_at=now)
    session.add(escalation)
    session.flush()
    # Personal care stays in the internal dashboard; never export raw sensitive text.
    return escalation.id

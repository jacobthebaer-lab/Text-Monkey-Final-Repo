"""Shared personal-care escalation for both signup and scheduling flows."""
from sqlalchemy import select
from app.core import templates
from app.db import models as m
from app.llm.parser import keyword_self_harm


def escalate_sensitive(session, gate, volunteer, body, now, *, phone=None, severity="normal"):
    pastor = session.scalar(select(m.Volunteer).where(m.Volunteer.is_pastor))
    severity = "urgent" if severity == "urgent" or keyword_self_harm(body) else "normal"
    label = volunteer.name if volunteer is not None else "Unknown signup sender"
    related_ids = {"volunteer_id": volunteer.id} if volunteer is not None else {"phone": phone}
    escalation = m.Escalation(category="sensitive", severity=severity,
        summary=f"{label} may need personal care: {body!r}", related_ids=related_ids,
        assigned_to=pastor.id if pastor is not None else None, status="open", created_at=now)
    session.add(escalation)
    session.flush()
    if pastor is not None and gate is not None:
        gate.send(body=templates.pastor_alert(label, body), purpose="escalation_notify",
                  volunteer=pastor, urgent=severity == "urgent")
    return escalation.id

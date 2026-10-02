"""Shared personal-care escalation for both signup and scheduling flows."""
from sqlalchemy import select
from app.core import templates
from app.db import models as m
from app.llm.parser import keyword_self_harm


def escalate_sensitive(session, gate, volunteer, body, now, *, phone=None, severity="normal"):
    from app.core.confirmations import enabled
    human_only = enabled(session)
    pastor = None if human_only else session.scalar(select(m.Volunteer).where(m.Volunteer.is_pastor))
    severity = "urgent" if keyword_self_harm(body) or (not human_only and severity == "urgent") else "normal"
    label = volunteer.name if volunteer is not None else "Unknown signup sender"
    related_ids = {"volunteer_id": volunteer.id} if volunteer is not None else {"phone": phone}
    escalation = m.Escalation(category="sensitive", severity=severity,
        summary=f"Human review required for {label} (attention signal; no diagnosis or automatic contact): {body!r}" if human_only else f"{label} may need personal care: {body!r}", related_ids=related_ids,
        assigned_to=pastor.id if pastor is not None else None, status="open", created_at=now)
    session.add(escalation)
    session.flush()
    if pastor is not None and gate is not None:
        gate.send(body=templates.pastor_alert(label, body), purpose="escalation_notify",
                  volunteer=pastor, urgent=severity == "urgent")
    return escalation.id

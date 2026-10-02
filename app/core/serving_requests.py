"""Persist Gloo-routed serving requests without granting roles or assignments."""

from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from sqlalchemy import select


def save_serving_request(session, clock, gate, gloo, volunteer, body, parsed, message_id):
    requests = list(volunteer.preferences.get("serving_requests", []))
    existing = session.scalar(select(m.Escalation.id).where(
        m.Escalation.category == "staffing_request",
        m.Escalation.related_ids["message_id"].as_integer() == message_id))
    if existing or any(item.get("message_id") == message_id for item in requests):
        return False
    requests.append({"message_id": message_id, "text": body, "dates": parsed.dates,
                     "received_at": clock.now().isoformat(), "status": "needs_coordinator_review"})
    volunteer.preferences = {**volunteer.preferences, "serving_requests": requests[-20:]}
    session.add(m.Escalation(category="staffing_request", severity="normal", status="open",
        summary=f"{volunteer.name} requested to serve: {body}",
        related_ids={"volunteer_id": volunteer.id, "message_id": message_id}, created_at=clock.now()))
    reply = compose_signup_reply(session, clock, gloo,
        f"Thanks, {volunteer.name.split()[0]}! Your serving request has been saved for your coordinator to review. You're not assigned yet; your coordinator will confirm the role and shift with you. Reply STOP to stop or HELP for help.",
        ("coordinator", "not assigned", "STOP", "HELP"))
    gate.send(body=reply, purpose="signup_reply", volunteer=volunteer)
    return True

"""Text-only profile setup: Gloo interprets; code validates and saves facts."""
import json
from datetime import date
from pathlib import Path
from sqlalchemy import select
from app.db import models as m
from app.core.signup_responder import compose_signup_reply
from app.llm.parser import _extract_json, keyword_sensitive
from app.llm.agent_loop import RunLogger
from app.llm.gloo_client import GlooUnavailableError

PROMPT = Path(__file__).resolve().parents[2] / "prompts/onboarding.md"


def prompt_for(session, stage):
    if stage == "interests":
        roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
        options = ", ".join(f"{r.id}: {r.name}" for r in roles)
        return f"What would you like to help with? {options[:360]}. Reply with names or numbers, or ANY. Some roles need coordinator clearance. STOP to stop."
    return "When can you serve, and how often? For example: Sundays at 9am, twice a month; unavailable October 18. You can also say FLEXIBLE. STOP to stop."


def start(session, clock, gate, volunteer, gloo):
    volunteer.preferences = {**volunteer.preferences, "onboarding_stage": "interests"}
    gate.send(body=compose_signup_reply(session, clock, gloo, prompt_for(session, "interests"), ("STOP",)),
              purpose="signup_reply", volunteer=volunteer)


def handle(session, clock, gate, volunteer, body, gloo):
    stage = volunteer.preferences.get("onboarding_stage")
    if stage not in {"interests", "availability"}:
        return None
    if keyword_sensitive(body):
        session.add(m.Escalation(category="sensitive", severity="normal",
                                summary="Signup sender needs a human review.",
                                related_ids={"volunteer_id": volunteer.id}, status="open", created_at=clock.now()))
        return "escalated_sensitive"
    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    logger = RunLogger(session, clock, agent="onboarding", trigger=f"Profile {stage}",
                       model=gloo.settings.parser_model if hasattr(gloo, "settings") else None)
    from app.config import get_settings
    settings = getattr(gloo, "settings", get_settings())
    try:
        response = gloo.create_response(model=settings.parser_model, instructions=PROMPT.read_text(),
            input=json.dumps({"stage": stage, "body": body, "today": clock.now().date().isoformat(),
                              "roles": [{"id": r.id, "name": r.name, "ministry": r.ministry} for r in roles]}))
        logger.add_usage(getattr(response, "usage", None))
        data = _extract_json(getattr(response, "output_text", "") or "") or {}
        # Some Gloo models wrap their result in the requested stage. Only that
        # known stage is read, and all fields still undergo the same validation.
        if isinstance(data.get(stage), dict):
            data = data[stage]
        logger.step("decision", result={"stage": stage, "extraction": data})
        valid = data.get("understood") is True
        prefs = {**volunteer.preferences}
        if data.get("sensitive") is True:
            session.add(m.Escalation(category="sensitive", severity="normal",
                                    summary="Signup sender needs a human review.", related_ids={"volunteer_id": volunteer.id},
                                    status="open", created_at=clock.now()))
            logger.close("sensitive")
            return "escalated_sensitive"
        if stage == "interests":
            ids = data.get("role_ids", [])
            valid = valid and isinstance(ids, list) and all(type(i) is int and i in {r.id for r in roles} for i in ids)
            valid = valid and (bool(ids) or data.get("any_role") is True)
            if valid:
                chosen = [r for r in roles if r.id in ids]
                prefs.update(interested_roles=[r.name for r in chosen],
                             preferred_ministry=", ".join(sorted({r.ministry for r in chosen})) or "Flexible",
                             onboarding_stage="availability")
        else:
            days = data.get("weekdays", [])
            services = data.get("preferred_services", [])
            maximum = data.get("max_per_month", 2)
            available, unavailable = data.get("available_dates", []), data.get("unavailable_dates", [])
            valid = valid and isinstance(days, list) and all(type(d) is int and 0 <= d <= 6 for d in days)
            valid = valid and isinstance(services, list) and all(s in {f"sun_{h}" for h in range(24)} for s in services)
            valid = valid and type(maximum) is int and 1 <= maximum <= 8
            for dates in (available, unavailable):
                valid = valid and isinstance(dates, list) and len(dates) <= 62
                if valid:
                    valid = all(type(d) is str and 0 <= (date.fromisoformat(d)-clock.now().date()).days <= 366 for d in dates)
            if valid:
                prefs.update(availability_weekdays=days, preferred_services=services, max_per_month=maximum,
                             availability_note=body[:500], onboarding_stage="complete", onboarding_completed_at=clock.now().isoformat())
                for month in sorted({d[:7] for d in available+unavailable}):
                    row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id, m.Availability.month == month).order_by(m.Availability.id.desc()))
                    if row is None:
                        row = m.Availability(volunteer_id=volunteer.id, month=month)
                        session.add(row)
                    row.available_dates = sorted(set((row.available_dates or [])+[d for d in available if d.startswith(month)]))
                    row.unavailable_dates = sorted(set((row.unavailable_dates or [])+[d for d in unavailable if d.startswith(month)]))
                    row.raw_reply, row.parsed_at = body, clock.now()
        if not valid:
            raise ValueError("Profile extraction incomplete or invalid")
    except (GlooUnavailableError, ValueError, TypeError):
        prefs = {**volunteer.preferences}
        attempts = prefs.get("onboarding_clarifications", 0)+1
        prefs["onboarding_clarifications"] = attempts
        volunteer.preferences = prefs
        logger.close("needs_clarification")
        if attempts > 1:
            if not prefs.get("onboarding_review_requested"):
                session.add(m.Escalation(category="unclear", severity="normal", summary=f"{volunteer.name} needs help finishing text signup.",
                    related_ids={"volunteer_id": volunteer.id}, status="open", created_at=clock.now()))
                volunteer.preferences = {**prefs, "onboarding_review_requested": True}
            return "onboarding_review"
        gate.send(body=compose_signup_reply(session, clock, gloo, prompt_for(session, stage), ("STOP",)), purpose="signup_reply", volunteer=volunteer)
        return "onboarding_clarify"
    prefs.pop("onboarding_clarifications", None)
    volunteer.preferences = prefs
    session.flush()
    logger.close("profile_saved")
    if stage == "interests":
        reply = prompt_for(session, "availability")
    else:
        reply = f"You’re ready, {volunteer.name.split()[0]}! We saved your preferences. We’ll text a specific shift when there’s a match; reply YES or NO. You’re only booked after confirmation. STOP to stop or HELP for help."
    gate.send(body=compose_signup_reply(session, clock, gloo, reply, ("STOP",)), purpose="signup_reply", volunteer=volunteer)
    return "onboarding_complete" if stage == "availability" else "onboarding_availability"

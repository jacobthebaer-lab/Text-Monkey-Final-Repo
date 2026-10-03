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
from app.core.care import escalate_sensitive
from app.core.onboarding_copy import DEFAULTS, copy_key, preferred_wording, render_copy, role_options
from app.core.signup_copy import exact_enabled, exact_message, ensure_exact_role_menu

PROMPT = Path(__file__).resolve().parents[2] / "prompts/onboarding.md"


def availability_context(session, volunteer, today):
    draft = volunteer.preferences.get('onboarding_availability_draft')
    if draft is not None:
        return dict(draft)
    prefs = volunteer.preferences
    rows = session.scalars(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id)).all()
    return {
        'availability_known': 'availability_weekdays' in prefs,
        'frequency_known': 'max_per_month' in prefs,
        'weekdays': prefs.get('availability_weekdays', []),
        'preferred_services': prefs.get('preferred_services', []),
        'all_day': prefs.get('availability_all_day', False),
        'max_per_month': prefs.get('max_per_month'),
        'available_dates': sorted({d for row in rows for d in (row.available_dates or [])
            if 0 <= (date.fromisoformat(d)-today).days <= 366}),
        'unavailable_dates': sorted({d for row in rows for d in (row.unavailable_dates or [])
            if 0 <= (date.fromisoformat(d)-today).days <= 366}),
    }


def validated_availability(data, previous, today):
    fields = ('availability_known', 'frequency_known', 'weekdays', 'preferred_services',
              'all_day', 'max_per_month', 'available_dates', 'unavailable_dates')
    merged = {**previous, **{key: data[key] for key in fields if key in data}}
    # Older stored interpreter output supplied a complete frequency value.
    if 'frequency_known' not in data and type(data.get('max_per_month')) is int:
        merged['frequency_known'] = True
    if 'availability_known' not in data and 'weekdays' in data:
        merged['availability_known'] = True
    for key in ('availability_known', 'frequency_known', 'all_day'):
        if type(merged[key]) is not bool:
            raise ValueError('Availability flags must be boolean')
    days, services = merged['weekdays'], merged['preferred_services']
    if not isinstance(days, list) or not all(type(d) is int and 0 <= d <= 6 for d in days):
        raise ValueError('Invalid weekdays')
    if not isinstance(services, list) or not all(s in {f'sun_{h}' for h in range(24)} for s in services):
        raise ValueError('Invalid service hours')
    if merged['all_day'] and services:
        raise ValueError('All-day availability cannot restrict service hours')
    if merged['frequency_known'] and not (type(merged['max_per_month']) is int and 1 <= merged['max_per_month'] <= 8):
        raise ValueError('Invalid serving frequency')
    if not merged['frequency_known']:
        merged['max_per_month'] = None
    for key in ('available_dates', 'unavailable_dates'):
        dates = merged[key]
        if not isinstance(dates, list) or len(dates) > 366 or not all(
            type(d) is str and 0 <= (date.fromisoformat(d)-today).days <= 366 for d in dates
        ):
            raise ValueError('Invalid availability dates')
        merged[key] = sorted(set(dates))
    merged['weekdays'], merged['preferred_services'] = list(dict.fromkeys(days)), list(dict.fromkeys(services))
    return merged


def availability_question(draft):
    if draft['availability_known'] and not draft['frequency_known']:
        return 'How often would you like to serve each month?'
    if draft['frequency_known'] and not draft['availability_known']:
        return 'Which days are you available to serve?'
    return 'Which days can you serve, and how often each month?'


def save_availability_dates(session, clock, volunteer, draft, previous, body):
    available, unavailable = draft['available_dates'], draft['unavailable_dates']
    months = {d[:7] for d in available+unavailable+previous['available_dates']+previous['unavailable_dates']}
    for month in sorted(months):
        row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id,
            m.Availability.month == month).order_by(m.Availability.id.desc()))
        if row is None:
            row = m.Availability(volunteer_id=volunteer.id, month=month)
            session.add(row)
        # A correction can remove a previous exclusion; unioning would retain it.
        row.available_dates = [d for d in available if d.startswith(month)]
        row.unavailable_dates = [d for d in unavailable if d.startswith(month)]
        row.raw_reply, row.parsed_at = body, clock.now()


def prompt_for(session, stage, volunteer=None):
    if volunteer and exact_enabled(session, volunteer.phone):
        return exact_message(stage,volunteer.name.split()[0])
    if stage == "interests":
        text = render_copy(DEFAULTS[stage], roles=role_options(session),
                           first_name=volunteer.name.split()[0] if volunteer else "there")
        return text
    return DEFAULTS["availability"]


def compose_reply(session, clock, gloo, approved_message, volunteer, field):
    if exact_enabled(session,volunteer.phone):
        return compose_signup_reply(session,clock,gloo,approved_message,volunteer=volunteer,
            signup_conversation=True,require_gloo=True,exact_copy=True)
    return compose_signup_reply(session, clock, gloo, approved_message, volunteer=volunteer,
        signup_conversation=True, require_gloo=True,
        preferred_wording=preferred_wording(session, field, volunteer), allow_emoji=field != 'clarification')


def start(session, clock, gate, volunteer, gloo, *, copy_owner=None):
    from app.core.confirmations import authorize_sender_fields
    authorize_sender_fields(session, volunteer, {"preferences"})
    if exact_enabled(session, volunteer.phone):
        ensure_exact_role_menu(session)
    volunteer.preferences = {**volunteer.preferences, "onboarding_stage": "interests", "signup_minimal_texts": True}
    if copy_owner is not None:
        # Only a verified administrator caller may supply this server-side ID.
        volunteer.preferences = {**volunteer.preferences, "onboarding_copy_owner": copy_key(copy_owner).removeprefix("onboarding_copy:")}
    else:
        # A fresh unbound start must not inherit another admin’s earlier copy.
        volunteer.preferences = {k: v for k, v in volunteer.preferences.items() if k != "onboarding_copy_owner"}
    return gate.send(body=compose_reply(session, clock, gloo, prompt_for(session, "interests", volunteer), volunteer, "interests"),
              purpose="signup_reply", volunteer=volunteer)


def handle(session, clock, gate, volunteer, body, gloo):
    stage = volunteer.preferences.get("onboarding_stage")
    if stage not in {"interests", "availability"}:
        return None
    if keyword_sensitive(body):
        escalate_sensitive(session, gate, volunteer, body, clock.now())
        return "escalated_sensitive"
    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    from app.core.confirmations import authorize_sender_fields
    authorize_sender_fields(session, volunteer, {"preferences"})
    session.info["sender_profile_instruction"] = True
    logger = RunLogger(session, clock, agent="onboarding", trigger=f"Profile {stage}",
                       model=gloo.settings.parser_model if hasattr(gloo, "settings") else None)
    from app.config import get_settings
    settings = getattr(gloo, "settings", get_settings())
    previous = availability_context(session, volunteer, clock.now().date()) if stage == 'availability' else None
    try:
        response = gloo.create_response(model=settings.parser_model, instructions=PROMPT.read_text(),
            input=json.dumps({"stage": stage, "body": body, "today": clock.now().date().isoformat(),
                              "roles": [{"id": r.id, "name": r.name, "ministry": r.ministry} for r in roles],
                              "saved_availability": previous}))
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
            escalate_sensitive(session, gate, volunteer, body, clock.now(), severity=data.get("severity"))
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
            if valid:
                draft = validated_availability(data, previous, clock.now().date())
                if body.strip().upper() in {'FLEXIBLE', 'SKIP'}:
                    # These commands relax recurring restrictions, not explicit exclusions.
                    draft['unavailable_dates'] = previous['unavailable_dates']
                # For new concise signups, frequency is optional. Preserve it as
                # unknown rather than inventing a preference or asking again.
                concise = prefs.get('signup_minimal_texts') is True
                if not draft['availability_known'] or (not draft['frequency_known'] and not concise):
                    prefs.update(onboarding_availability_draft=draft)
                    prefs.pop('onboarding_clarifications', None)
                    volunteer.preferences = prefs
                    session.flush()
                    logger.close('partial_saved')
                    if exact_enabled(session,volunteer.phone):
                        session.add(m.Escalation(category='unclear',severity='normal',
                            summary=f'{volunteer.name} needs coordinator review of incomplete preferences.',
                            related_ids={'volunteer_id':volunteer.id},status='open',created_at=clock.now()))
                        return 'onboarding_review'
                    gate.send(body=compose_reply(session, clock, gloo,
                        availability_question(draft), volunteer, 'clarification'),
                        purpose='signup_reply', volunteer=volunteer)
                    return 'onboarding_clarify'
                prefs.pop('onboarding_availability_draft', None)
                prefs.update(availability_weekdays=draft['weekdays'], preferred_services=draft['preferred_services'],
                             availability_all_day=draft['all_day'], availability_frequency_known=draft['frequency_known'],
                             availability_note=body[:500], onboarding_stage="complete", onboarding_completed_at=clock.now().isoformat())
                if draft['frequency_known']:
                    prefs['max_per_month'] = draft['max_per_month']
                else:
                    prefs.pop('max_per_month', None)
                save_availability_dates(session, clock, volunteer, draft, previous, body)
        if not valid:
            raise ValueError("Profile extraction incomplete or invalid")
    except GlooUnavailableError:
        logger.close('gloo_unavailable')
        session.add(m.Escalation(category='system_error', severity='normal',
            summary=f'{volunteer.name} needs help finishing text signup because Gloo is unavailable.',
            related_ids={'volunteer_id': volunteer.id}, status='open', created_at=clock.now()))
        return 'onboarding_review'
    except (ValueError, TypeError):
        prefs = {**volunteer.preferences}
        attempts = prefs.get("onboarding_clarifications", 0)+1
        prefs["onboarding_clarifications"] = attempts
        volunteer.preferences = prefs
        logger.close("needs_clarification")
        if exact_enabled(session,volunteer.phone):
            session.add(m.Escalation(category='unclear',severity='normal',
                summary=f'{volunteer.name} needs coordinator review of unclear preferences.',
                related_ids={'volunteer_id':volunteer.id},status='open',created_at=clock.now()))
            return 'onboarding_review'
        if attempts > 1:
            if not prefs.get("onboarding_review_requested"):
                session.add(m.Escalation(category="unclear", severity="normal", summary=f"{volunteer.name} needs help finishing text signup.",
                    related_ids={"volunteer_id": volunteer.id}, status="open", created_at=clock.now()))
                volunteer.preferences = {**prefs, "onboarding_review_requested": True}
            return "onboarding_review"
        question = availability_question(previous) if stage == 'availability' else prompt_for(session, stage, volunteer)
        gate.send(body=compose_reply(session, clock, gloo, question, volunteer,
            "clarification" if stage == "availability" else "interests"), purpose="signup_reply", volunteer=volunteer)
        return "onboarding_clarify"
    prefs.pop("onboarding_clarifications", None)
    volunteer.preferences = prefs
    session.flush()
    logger.close("profile_saved")
    if stage == "interests":
        reply = prompt_for(session, "availability", volunteer)
    else:
        reply = exact_message('completion',volunteer.name.split()[0]) if exact_enabled(session,volunteer.phone) else render_copy(DEFAULTS["completion"], first_name=volunteer.name.split()[0])
    gate.send(body=compose_reply(session, clock, gloo, reply, volunteer,
        "availability" if stage == "interests" else "completion"), purpose="signup_reply", volunteer=volunteer)
    return "onboarding_complete" if stage == "availability" else "onboarding_availability"

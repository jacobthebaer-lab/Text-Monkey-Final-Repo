"""Proof of preferences saved from one actual signup input, never a booking."""
from datetime import datetime
import hashlib
import json
from sqlalchemy import select
from app.db import models as m


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def saved_facts(session, volunteer):
    preferences = {key: value for key, value in volunteer.preferences.items()
                   if key != 'onboarding_completed_at'}
    dates = [[row.month, row.available_dates, row.unavailable_dates]
             for row in session.scalars(select(m.Availability).where(
                 m.Availability.volunteer_id == volunteer.id).order_by(m.Availability.month, m.Availability.id)
                 .execution_options(populate_existing=True))]
    return {'preferences_hash': digest(preferences), 'availability_hash': digest(dates)}


def capture(session, volunteer, incoming, step, now):
    if not incoming or not step:
        return None
    proof = {'step_id': step.id, 'step_hash': digest(step.result), **saved_facts(session, volunteer)}
    return proof if binding(session, volunteer, incoming, proof, now) else None


def binding(session, volunteer, incoming, proof, now):
    """Original sender and completed interpretation must still match saved facts."""
    if not isinstance(proof, dict) or set(proof) != {'step_id', 'step_hash', 'preferences_hash', 'availability_hash'}:
        return None
    step = session.get(m.AgentStep, proof['step_id'], populate_existing=True)
    run = session.get(m.AgentRun, step.run_id, populate_existing=True) if step else None
    try:
        completed = datetime.fromisoformat(volunteer.preferences['onboarding_completed_at'])
    except (KeyError, ValueError, TypeError):
        return None
    if (not incoming or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.phone != volunteer.phone or incoming.volunteer_id != volunteer.id
            or volunteer.preferences.get('onboarding_stage') != 'complete'
            or completed.tzinfo is None or not incoming.created_at <= completed <= now
            or not step or not run or run.agent != 'onboarding' or run.trigger != 'Profile availability'
            or run.outcome != 'profile_saved' or run.started_at < incoming.created_at
            or run.ended_at is None or not run.started_at <= step.created_at <= run.ended_at <= now
            or completed > run.ended_at
            or step.type != 'decision' or not isinstance(step.result, dict)
            or step.result.get('stage') != 'availability'
            or step.result.get('incoming_message_id') != incoming.id
            or step.result.get('incoming_body_hash') != hashlib.sha256(incoming.body.encode()).hexdigest()
            or not isinstance(step.result.get('extraction'), dict)
            or step.result['extraction'].get('understood') is not True
            or digest(step.result) != proof['step_hash']
            or saved_facts(session, volunteer) != {key: proof[key] for key in ('preferences_hash', 'availability_hash')}):
        return None
    return dict(proof)

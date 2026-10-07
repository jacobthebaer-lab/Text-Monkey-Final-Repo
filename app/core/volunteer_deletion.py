"""Remove roster identities while retaining history and phone-level safeguards."""
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from app.db import models as m
from app.core.offer_windows import begin_decision

PREFIX = 'volunteer-deletion:'


def references(value, person):
    if isinstance(value, dict):
        if (str(value.get('volunteer_id')) == str(person.id) or value.get('phone') == person.phone
                or (isinstance(value.get('volunteer_ids'), list) and
                    any(str(identifier) == str(person.id) for identifier in value['volunteer_ids']))):
            return True
        return any(references(item, person) for item in value.values())
    if isinstance(value, list):
        return any(references(item, person) for item in value)
    return False


def remove(session, clock, volunteer_id, actor):
    begin_decision(session)
    person = session.scalar(select(m.Volunteer).where(m.Volunteer.id == volunteer_id).with_for_update())
    if person is None or person.status == 'deleted':
        raise HTTPException(404, 'Volunteer not found.')
    if session.scalar(select(m.Assignment.id).where(m.Assignment.volunteer_id == person.id,
            m.Assignment.status.in_(['proposed', 'approved', 'confirmed'])).limit(1)):
        raise HTTPException(409, 'Remove this volunteer’s active shift assignments before deleting their profile.')
    if person.is_coordinator or person.is_pastor:
        raise HTTPException(409, 'Remove this person’s coordinator or pastor role before deleting their profile.')
    holds = session.scalars(select(m.Escalation).where(m.Escalation.category.in_(['sensitive', 'pastoral']),
        m.Escalation.status.in_(['open', 'acknowledged']))).all()
    if any(references(hold.related_ids, person) or hold.assigned_to == person.id for hold in holds):
        raise HTTPException(409, 'Resolve this volunteer’s open care follow-up before deleting their profile.')
    messages = session.scalars(select(m.Message).where(m.Message.phone == person.phone,
        m.Message.direction == 'out', m.Message.status.in_(['queued', 'dispatching', 'uncertain']))
        .with_for_update()).all()
    if any(row.status in {'dispatching', 'uncertain'} for row in messages):
        raise HTTPException(409, 'A text is still being delivered or needs delivery verification. Resolve it before deleting this profile.')
    for row in messages:
        row.status = 'blocked_deleted'
    for approval in session.scalars(select(m.Approval).where(m.Approval.status == 'pending').with_for_update()):
        if references(approval.payload, person):
            approval.status = 'cancelled'
            approval.decided_at = clock.now()
            approval.decided_by = actor
            approval.via = 'web'
    for notice in session.scalars(select(m.Notification).where(m.Notification.volunteer_id == person.id,
            m.Notification.state.in_(['pending', 'scheduled', 'authorized', 'awaiting_review', 'queued', 'processing']))):
        notice.state = 'cancelled'
    phone = person.phone
    generation = str(uuid4())
    detail = {'volunteer_id': person.id, 'phone': phone, 'name': person.name,
              'generation': generation, 'actor': actor, 'deleted_at': clock.now().isoformat()}
    session.add(m.Notification(key=PREFIX + generation, volunteer_id=person.id,
        purpose='volunteer_deletion', state='completed', created_at=clock.now(), due_at=clock.now(), detail=detail))
    marker = session.get(m.Policy, PREFIX + phone)
    if marker:
        marker.value = detail
    else:
        session.add(m.Policy(key=PREFIX + phone, value=detail))
    # Keep the old primary key and its history, so a new identity cannot inherit
    # receipts, qualifications, assignments or pending review references.
    person.status = 'deleted'
    person.sms_opt_in = False
    person.phone = 'deleted:' + str(person.id)
    session.flush()
    return {'deleted': True, 'volunteer_id': str(person.id), 'texts_sent': 0}

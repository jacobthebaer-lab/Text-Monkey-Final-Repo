"""Hold ambiguous sender cancellations internally, never interpret bare numbers."""
import re
import hashlib
from sqlalchemy import select
from app.db import models as m


def bookings(session, volunteer, now):
    rows=list(session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id==volunteer.id,
        m.Assignment.status.in_(('proposed','approved','confirmed')),
        m.Event.status=='scheduled',m.Event.starts_at>now
    ).order_by(m.Assignment.id).execution_options(populate_existing=True)))
    for assignment in rows:
        session.refresh(assignment.shift)
        session.refresh(assignment.shift.event)
        session.refresh(assignment.shift.role)
    return rows


def snapshot(rows):
    return [[a.id,a.shift_id,a.status,a.updated_at.isoformat(),a.shift.role_id,
             a.shift.event.starts_at.isoformat(),a.shift.event.ends_at.isoformat(),a.shift.event.status]
            for a in rows]


def explicit_target(rows, body, tz):
    """A role and calendar day must identify exactly one current booking."""
    text=body.lower().replace('’',"'")
    matches=[]
    for a in rows:
        event=a.shift.event.starts_at.astimezone(tz)
        role=re.search(r'(?<!\w)'+re.escape(a.shift.role.name.lower())+r'(?!\w)',text)
        day=(event.date().isoformat() in text or
             re.search(r'\b(?:'+event.strftime('%A|%a').lower()+r')\b',text) or
             re.search(r'\b(?:'+event.strftime('%B|%b').lower()+r')\s+'+str(event.day)+r'(?:st|nd|rd|th)?\b',text))
        if role and day:
            matches.append(a)
    return matches[0] if len(matches)==1 else None


def _review(session, row, now):
    existing=session.scalar(select(m.Escalation).where(m.Escalation.category=='cancellation_scope',
        m.Escalation.related_ids['scope_key'].as_string()==row.key))
    if existing is not None:
        existing.status='open'
        existing.related_ids={**existing.related_ids,'message_id':row.detail['source_message_id']}
    else:
        admin=session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator,m.Volunteer.status=='active'))
        session.add(m.Escalation(category='cancellation_scope',severity='normal',
            summary='Cancellation needs a specific current role and day. Review the unresolved bookings internally.',
            related_ids={'scope_key':row.key,'volunteer_id':row.volunteer_id,
                         'message_id':row.detail['source_message_id']},
            assigned_to=admin.id if admin else None,status='open',created_at=now))


def route(session, clock, gate, volunteer, message, parser, ctx, *, instruction):
    """Return a held or explicitly resolved cancellation, otherwise leave routing alone."""
    now=clock.now()
    key=f'cancellation-scope:{volunteer.id}'
    hold=session.get(m.Notification,key)
    hold=hold if hold and hold.state=='pending' else None
    numbers=re.sub(r'\b(?:and|or)\b','',message.body,flags=re.I)
    numeral=bool(re.search(r'\d',numbers) and re.fullmatch(r'[\d\s.,;:!?()/+-]+',numbers))
    selected=session.info.get('mac_test_session')
    session_id=selected.id if selected else None
    legacy=None
    if numeral and hold is None:
        from app.core.conversation import scope
        from datetime import timedelta
        legacy=session.scalar(scope(select(m.Message),selected).where(m.Message.volunteer_id==volunteer.id,
            m.Message.phone==volunteer.phone,m.Message.direction=='out',m.Message.purpose=='clarify_shift',
            m.Message.status.in_(('sent','submitted','uncertain')),m.Message.created_at>=now-timedelta(hours=24)))
    if not instruction and not (numeral and (hold or legacy)):
        return None
    # The scope is anchored to an actual recorded message from this sender.
    if (message.direction!='in' or message.status!='received' or message.phone!=volunteer.phone or
            message.volunteer_id!=volunteer.id or message.created_at>now):
        return ('cancellation_review', ['Actual sender evidence is missing'], None, None)
    current=bookings(session,volunteer,now)
    if not hold and not legacy and len(current)<=1:
        return None
    original_snapshot=snapshot(current)
    parsed=parser(message.body) if instruction else None
    now=clock.now()
    session.refresh(volunteer)
    current=bookings(session,volunteer,now)
    escalation_id=None
    if parsed and parsed.sensitive:
        from app.core.care import escalate_sensitive
        escalation_id=escalate_sensitive(session,gate,volunteer,message.body,now,severity=parsed.severity)
    if hold is None:
        hold=m.Notification(key=key,volunteer_id=volunteer.id,purpose='cancellation_scope',body='',
            state='pending',created_at=now,due_at=now,
            detail={'source_message_id':message.id,'source_body_hash':hashlib.sha256(message.body.encode()).hexdigest(),
                    'phone':volunteer.phone,'session_id':session_id,
                    'bookings':original_snapshot,'legacy_question_id':legacy.id if legacy else None})
        prior=session.get(m.Notification,key)
        if prior:
            prior.state,prior.detail,prior.created_at='pending',hold.detail,now
            hold=prior
        else:
            session.add(hold)
        session.flush()
    source=session.get(m.Message,hold.detail.get('source_message_id'))
    source_valid=(source and source.direction=='in' and source.status=='received' and
        source.phone==volunteer.phone and source.volunteer_id==volunteer.id and
        hold.detail.get('phone')==volunteer.phone and hold.detail.get('session_id')==session_id and
        hold.detail.get('source_body_hash')==hashlib.sha256(source.body.encode()).hexdigest() and
        source.created_at<=hold.created_at and
        (not selected or selected.active(now) and source.purpose=='test:'+selected.id))
    unchanged=hold.detail.get('bookings')==snapshot(current)
    from app.core.policies import PolicyStore
    target=explicit_target(current,message.body,PolicyStore(session).church_tz()) if instruction else None
    if (ctx and source_valid and unchanged and target and parsed and parsed.intent=='cancel'
            and parsed.confidence>=0.7 and not parsed.parse_error):
        from app.agents.fill_agent import cancel_recorded_assignment
        outcome=cancel_recorded_assignment(ctx,volunteer,target.id,sensitive=parsed.sensitive,
            expected_scope=hold.detail['bookings'])
        if target.status=='cancelled':
            hold.state='resolved'
            hold.detail={**hold.detail,'resolved_message_id':message.id,'assignment_id':target.id}
            for review in session.scalars(select(m.Escalation).where(m.Escalation.category=='cancellation_scope',
                    m.Escalation.related_ids['scope_key'].as_string()==key)):
                review.status='resolved'
            return ('fill_agent',[outcome.action],parsed,escalation_id)
        hold.detail={**hold.detail,'reason':'Current booking scope changed at the cancellation decision'}
        _review(session,hold,now)
        return ('cancellation_review',[hold.detail['reason']],parsed,escalation_id)
    hold.detail={**hold.detail,'reason':('Current booking or sender scope changed' if not source_valid or not unchanged
                else 'A bare number or ambiguous reply cannot choose a booking')}
    _review(session,hold,now)
    return ('cancellation_review',[hold.detail['reason']],parsed,escalation_id)

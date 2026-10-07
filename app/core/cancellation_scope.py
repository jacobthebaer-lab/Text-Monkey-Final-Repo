"""Hold ambiguous sender cancellations internally, never interpret bare numbers."""
import re
import hashlib
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m
from app.core.church_labels import church_label

WEEKDAY = r'(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)'
RELATIVE_CALENDAR = re.compile(r'\b(?:today|tomorrow|tonight|yesterday|(?:this|next|coming)\s+(?:week|weekend|month|'+WEEKDAY+r'))\b',re.I)
CALENDAR_DATE = re.compile(r'\b\d{4}-\d{2}-\d{2}\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?\b', re.I)


def generic_cancellation(body):
    """Only unqualified phrases can use the sole-booking shortcut."""
    text = ' '.join(body.strip().lower().replace('’', "'").split())
    reference = r"(?:(?:my|the|this|that)\s+)?(?:shift|booking|assignment|it)"
    imperative = r"(?:please\s+)?(?:(?:can|could) you\s+)?cancel(?:\s+"+reference+r")?"
    absence = r"(?:i\s+)?(?:can't|cannot|won't|will not|am unable to|unable to)\s+(?:make|come|attend|serve|help|cover)(?:\s+"+reference+r")?"
    need = r"i (?:need|have|want) to cancel(?:\s+"+reference+r")?"
    return bool(re.fullmatch(r"(?:"+imperative+r"|"+absence+r"|"+need+r")[!.?]*",text))


def bookings(session, volunteer, now):
    rows=list(session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id==volunteer.id,
        m.Assignment.status.in_(('proposed','approved','confirmed')),
        m.Event.status=='scheduled',m.Shift.starts_at>now
    ).order_by(m.Assignment.id).execution_options(populate_existing=True)))
    for assignment in rows:
        session.refresh(assignment.shift)
        session.refresh(assignment.shift.event)
        session.refresh(assignment.shift.role)
    return rows


def snapshot(rows):
    return [[a.id,a.shift_id,a.status,a.updated_at.isoformat(),a.shift.role_id,
             a.shift.starts_at.isoformat(),a.shift.ends_at.isoformat(),a.shift.event.status]
            for a in rows]


def explicit_target(rows, body, tz, *, role_names=(), source_at=None):
    """A unique absolute calendar day, or role plus weekday, identifies a booking."""
    text=body.lower().replace('’',"'")
    matches=[]
    calendar_named = bool(CALENDAR_DATE.search(text))
    relative_day = None
    if not calendar_named and RELATIVE_CALENDAR.search(text):
        references = [match.group().lower() for match in RELATIVE_CALENDAR.finditer(text)]
        offsets = {'today': 0, 'tonight': 0, 'tomorrow': 1, 'yesterday': -1}
        if source_at is None or source_at.tzinfo is None or any(ref not in offsets for ref in references):
            return None
        dates = {source_at.astimezone(tz).date() + timedelta(days=offsets[ref]) for ref in references}
        if len(dates) != 1:
            return None
        relative_day = dates.pop()
        weekday_labels = re.findall(r'\b'+WEEKDAY+r'\b', text)
        expected_weekdays = {relative_day.strftime('%A').lower(), relative_day.strftime('%a').lower()}
        if any(label not in expected_weekdays for label in weekday_labels):
            return None
    named_years = {int(year) for year in re.findall(r'\b(?:19|20|21)\d{2}\b',text)}
    all_labels = {label for name in [*role_names, *(a.shift.role.name for a in rows)]
                  for label in (name.lower(),church_label(name).lower()) if label}
    named_role = any(re.search(r'(?<!\w)'+re.escape(label)+r'(?!\w)',text) for label in all_labels)
    for a in rows:
        event=a.shift.starts_at.astimezone(tz)
        labels={a.shift.role.name.lower(), church_label(a.shift.role.name).lower()}
        role=any(label and re.search(r'(?<!\w)'+re.escape(label)+r'(?!\w)',text) for label in labels)
        calendar_match = re.search(r'\b(?:'+event.strftime('%B|%b').lower()+r')\s+'+str(event.day)+r'(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b',text)
        absolute_day = bool(relative_day == event.date() or re.search(r'(?<!\d)'+event.date().isoformat()+r'(?!\d)',text) or
                            calendar_match and (not calendar_match.group(1) or int(calendar_match.group(1)) == event.year))
        if named_years and named_years != {event.year}:
            continue
        # An explicit date must match. A shared weekday never overrides it.
        day = absolute_day if calendar_named or relative_day is not None else bool(re.search(r'\b(?:'+event.strftime('%A|%a').lower()+r')\b',text))
        if absolute_day and (not named_role or role) or (role or len(rows)==1 and not named_role) and day:
            matches.append(a)
    return matches[0] if len(matches)==1 else None


def review_source(session, volunteer_id, message_id, provider, selected, now, expected_hash=None):
    """Use recorded sender evidence, never caller-supplied identity or body text."""
    from app.core.conversation import inbound_scope
    from app.sms.transport import session_transport
    volunteer = session.get(m.Volunteer, volunteer_id)
    source = session.get(m.Message, message_id)
    if (not volunteer or not source or source.direction != 'in' or source.status != 'received'
            or source.volunteer_id != volunteer.id or source.phone != volunteer.phone or source.created_at > now
            or expected_hash and hashlib.sha256(source.body.encode()).hexdigest() != expected_hash):
        return None
    if session_transport(provider):
        current = provider.test_sessions.get(volunteer.phone)
        if (not selected or not current or current != selected or not selected.active(now)
                or not provider.allows(volunteer.phone)):
            return None
        if session.scalar(select(m.Message.id).where(m.Message.id == source.id, inbound_scope(selected),
                m.Message.created_at >= selected.starts_at, selected.window(m.Message.created_at))) is None:
            return None
    elif selected or source.kind != 'inbound' or (source.purpose or '').startswith('test:'):
        return None
    return source


def _review(session, row, now, gate, message):
    from app.sms.transport import transport_name
    selected = session.info.get('mac_test_session')
    source = review_source(session, row.volunteer_id, message.id, gate.provider, selected, now)
    if source is None:
        return
    metadata = {'scope_key':row.key, 'volunteer_id':row.volunteer_id, 'message_id':source.id,
                'source_body_hash':hashlib.sha256(source.body.encode()).hexdigest(),
                'transport':transport_name(gate.provider), 'phone':source.phone,
                'session_id':selected.id if selected else None, 'bookings':row.detail['bookings']}
    existing=session.scalar(select(m.Escalation).where(m.Escalation.category=='cancellation_scope',
        m.Escalation.related_ids['scope_key'].as_string()==row.key))
    if existing is not None:
        existing.status='open'
        existing.related_ids=metadata
        existing.created_at=source.created_at
    else:
        admin=session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator,m.Volunteer.status=='active'))
        session.add(m.Escalation(category='cancellation_scope',severity='normal',
            summary='Cancellation needs a specific current role and day. Review the unresolved bookings internally.',
            related_ids=metadata, assigned_to=admin.id if admin else None,status='open',created_at=source.created_at))


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
    role_names = session.scalars(select(m.Role.name)).all()
    if not hold and not legacy and len(current)==1 and instruction and generic_cancellation(message.body):
        return None
    original_snapshot=snapshot(current)
    parsed=parser(message.body) if instruction else None
    now=clock.now()
    session.refresh(volunteer)
    current=bookings(session,volunteer,now)
    from app.core.policies import PolicyStore
    target=explicit_target(current,message.body,PolicyStore(session).church_tz(),
        role_names=role_names,source_at=message.created_at) if instruction else None
    # An absence declaration about future availability is not an instruction
    # to select an unrelated booking. Reuse its actual classification once.
    if (target is None and parsed
            and parsed.intent == 'availability' and parsed.confidence >= 0.7
            and not parsed.parse_error and not re.search(r'\bcancel\b', message.body, re.I)):
        # A prior unresolved cancellation does not turn a new availability
        # declaration into its answer. Preserve that hold and source unchanged.
        return ('classification', [], parsed, None)
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
        _review(session,hold,now,gate,message)
        if not (parsed and parsed.sensitive):
            from app.core.cancellation_reply import reply
            reply(session,clock,gate,volunteer)
        return ('cancellation_review',[hold.detail['reason']],parsed,escalation_id)
    hold.detail={**hold.detail,'reason':('Current booking or sender scope changed' if not source_valid or not unchanged
                else 'A bare number or ambiguous reply cannot choose a booking')}
    if source_valid and numeral and message.id != source.id:
        # Preserve the original cancellation request. This fresh recorded
        # clarification owns only its no-change reply, never a booking choice.
        hold.detail={**hold.detail,'clarification_message_id':message.id,
            'clarification_body_hash':hashlib.sha256(message.body.encode()).hexdigest()}
    _review(session,hold,now,gate,message)
    if not (parsed and parsed.sensitive):
        from app.core.cancellation_reply import reply
        reply(session,clock,gate,volunteer)
    return ('cancellation_review',[hold.detail['reason']],parsed,escalation_id)

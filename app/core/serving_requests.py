"""Retain availability input and acknowledge its actual saved or review state."""
import calendar
from datetime import date, timedelta
import hashlib
import re
from app.db import models as m
from sqlalchemy import select


def whole_month_absence(body, now):
    """Resolve one explicit whole-month absence, never a qualified date range."""
    text = body.lower().replace("’", "'")
    if ("?" in text or re.search(r"\b(?:maybe|might|think|possibly|probably|if|unless|except|until|through|from|before|after|starting|back|return|days?|weeks?|weekends?|mornings?|evenings?|first|second|third|last|only|some|few|part|portion)\b|\b\d{1,2}(?:st|nd|rd|th)?\b", text)
            or not re.search(r"\bi(?:(?:'m| am| will be|'ll be) (?:unavailable|away|out of town|not (?:available|here|there))| (?:won't|will not|can't|cannot) be (?:here|there|available)| (?:can't|cannot|won't|will not) (?:serve|volunteer|attend))\b", text)):
        return None
    found = [(i, name) for i, name in enumerate(calendar.month_name) if i and re.search(r"\b" + name.lower() + r"\b", text)]
    if len(found) != 1:
        return None
    month, _ = found[0]
    years = re.findall(r"\b20\d{2}\b", text)
    next_year = bool(re.search(r'\bnext year\b', text))
    this_year = bool(re.search(r'\bthis year\b', text))
    if len(years) > 1 or (next_year and this_year) or (years and (next_year or this_year)):
        return None
    year = int(years[0]) if years else now.year + (1 if next_year else 0 if this_year else month < now.month)
    if date(year, month, calendar.monthrange(year, month)[1]) < now.date():
        return None
    return f"{year:04}-{month:02}", [date(year, month, day).isoformat() for day in range(1, calendar.monthrange(year, month)[1] + 1)]


def _scope(selected):
    return None if selected is None else [selected.id, selected.starts_at.isoformat(), selected.end_iso()]


def binding(session, volunteer, message_id, now):
    """Original sender input and saved facts remain authoritative at delivery."""
    from app.core.conversation import scope
    from app.core.privacy import safe_message_history
    from app.llm.parser import keyword_sensitive
    selected = session.info.get('mac_test_session')
    if not volunteer or volunteer.status != 'active' or not volunteer.sms_opt_in or type(message_id) is not int:
        return None
    session.flush()
    volunteer = session.get(m.Volunteer, volunteer.id, populate_existing=True)
    if volunteer is None:
        return None
    incoming = session.scalar(scope(select(m.Message), selected).where(m.Message.id == message_id).execution_options(populate_existing=True))
    request = next((x for x in (volunteer.preferences or {}).get('serving_requests', []) if x.get('message_id') == message_id), None)
    if (volunteer.status != 'active' or not volunteer.sms_opt_in or not incoming or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.volunteer_id != volunteer.id or incoming.phone != volunteer.phone
            or not timedelta(0) <= now - incoming.created_at < timedelta(days=2)
            or (selected and not selected.active(now)) or not request
            or request.get('session_scope') != _scope(selected)
            or request.get('status') not in {'recorded_unavailable', 'recorded_availability', 'needs_coordinator_review'}
            or request.get('text') != incoming.body or request.get('input_hash') != hashlib.sha256(incoming.body.encode()).hexdigest()
            or keyword_sensitive(incoming.body) or not safe_message_history(session, [incoming])):
        return None
    facts = {'message_id': message_id, 'input_hash': request['input_hash'], 'session_scope': request['session_scope'],
        'volunteer_id': volunteer.id, 'phone': volunteer.phone, 'name': volunteer.name, 'status': request['status']}
    if request['status'] in {'recorded_unavailable', 'recorded_availability'}:
        row = session.get(m.Availability, request.get('availability_id'), populate_existing=True)
        dates = request.get('dates')
        if (not row or row.volunteer_id != volunteer.id or row.month != request.get('month') or not dates
                or (request['status'] == 'recorded_unavailable' and
                    (not set(dates) <= set(row.unavailable_dates or []) or set(dates) & set(row.available_dates or [])))
                or (request['status'] == 'recorded_availability' and
                    (row.available_dates != request.get('available_dates') or row.unavailable_dates != request.get('unavailable_dates')))):
            return None
        facts.update(month=row.month, dates=dates, availability_id=row.id)
    elif request['status'] != 'needs_coordinator_review':
        return None
    return facts


def copy_for(facts):
    name = facts['name'].split()[0]
    if facts['status'] == 'recorded_unavailable':
        year, month = map(int, facts['month'].split('-'))
        return f"Thanks, {name}! I've marked all of {calendar.month_name[month]} {year} as unavailable for you."
    if facts['status'] == 'recorded_availability':
        year, month = map(int, facts['month'].split('-'))
        return f"Thanks, {name}! I've saved your availability for {calendar.month_name[month]} {year}."
    return f"Thanks, {name}! I've recorded your availability update for your coordinator to review. It hasn't changed any assignments."


def save_serving_request(session, clock, gate, gloo, volunteer, body, parsed, message_id):
    from app.core import confirmations
    from app.core.policies import PolicyStore
    from app.agents.fill_agent import FillContext
    from app.core.notifications import deliver
    from app.llm.parser import keyword_sensitive
    requests = list((volunteer.preferences or {}).get('serving_requests', []))
    if any(item.get('message_id') == message_id for item in requests):
        return False  # No parser replay, second composition or duplicate send.
    incoming = session.get(m.Message, message_id)
    if (not incoming or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.volunteer_id != volunteer.id or incoming.phone != volunteer.phone or incoming.body != body
            or parsed.intent != 'availability' or parsed.sensitive or keyword_sensitive(body)):
        return False
    now = clock.now()
    absence = whole_month_absence(body, now.astimezone(PolicyStore(session).church_tz()))
    item = {'message_id': message_id, 'text': body, 'dates': parsed.dates, 'received_at': now.isoformat(),
        'input_hash': hashlib.sha256(body.encode()).hexdigest(), 'session_scope': _scope(session.info.get('mac_test_session')),
        'status': 'needs_coordinator_review'}
    confirmations.authorize_sender_fields(session, volunteer, {'preferences'})
    if absence:
        month, dates = absence
        row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id, m.Availability.month == month))
        if row is None:
            row = m.Availability(volunteer_id=volunteer.id, month=month)
            session.add(row)
        previous = session.info.get('sender_profile_instruction')
        if session.info.get('sender_phone') == volunteer.phone:
            session.info['sender_profile_instruction'] = True
        try:
            row.available_dates = sorted(set(row.available_dates or []) - set(dates))
            row.unavailable_dates = sorted(set(row.unavailable_dates or []) | set(dates))
            row.raw_reply, row.parsed_at = body, now
            session.flush()
        finally:
            if previous is None:
                session.info.pop('sender_profile_instruction', None)
            else:
                session.info['sender_profile_instruction'] = previous
        item.update(status='recorded_unavailable', month=month, dates=dates, availability_id=row.id)
    else:
        # Preserve the existing collection/date flow. Qualified negative month
        # statements stay in review rather than accidentally becoming available.
        text = body.lower().replace("’", "'")
        positive_instruction = re.search(r"\bi(?: can| am available|'m available| will be available)\b|^\s*(?:available|first|second|third|fourth|fifth|[1-5](?:st|nd|rd|th))\b|\b\d{4}-\d{2}-\d{2}\b", text)
        legacy_instruction = (text.strip() in {'not this month', 'same as usual'} or
            (positive_instruction and not re.search(r"\b(?:not|won't|can't|cannot|away|unavailable|except|maybe|might|if)\b", text)
             and (any(isinstance(d, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d) for d in parsed.dates)
                  or re.search(r'\b(?:first|second|third|fourth|fifth|[1-5](?:st|nd|rd|th))\b', text))))
        if legacy_instruction:
            from app.agents.planning_agent import record_availability
            previous = session.info.get('sender_profile_instruction')
            if session.info.get('sender_phone') == volunteer.phone:
                session.info['sender_profile_instruction'] = True
            try:
                outcome = record_availability(FillContext(session, clock, gate.provider, gloo), volunteer, parsed, body)
            finally:
                if previous is None:
                    session.info.pop('sender_profile_instruction', None)
                else:
                    session.info['sender_profile_instruction'] = previous
            if 'error' not in outcome:
                row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id, m.Availability.month == outcome['month']))
                item.update(status='recorded_availability', month=row.month, availability_id=row.id,
                    dates=sorted(set(row.available_dates + row.unavailable_dates)),
                    available_dates=row.available_dates, unavailable_dates=row.unavailable_dates)
    if item['status'] == 'needs_coordinator_review':
        session.add(m.Escalation(category='staffing_request', severity='normal', status='open',
            summary=f'{volunteer.name} supplied an availability update requiring review.',
            related_ids={'volunteer_id': volunteer.id, 'message_id': message_id}, created_at=now))
    volunteer.preferences = {**(volunteer.preferences or {}), 'serving_requests': (requests + [item])[-20:]}
    session.flush()
    facts = binding(session, volunteer, message_id, now)
    if facts:
        deliver(FillContext(session, clock, gate.provider, gloo, reply_to_message_id=message_id),
            key=f'availability-followup:{message_id}', body=copy_for(facts), purpose='signup_reply', volunteer=volunteer,
            conversation={'availability_followup': message_id})
    return True

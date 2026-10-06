"""Calendar restrictions and evidence-only planning proposals, never bookings.

Patterns are normalized Gloo/administrator proposals. Only explicit sender
instructions or the existing exact record review may persist preferences.
History proposes a review; it never supplies consent or serving permission.
"""
import calendar
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import select
from app.db import models as m

KEY = 'calendar_patterns'


def normalize_patterns(value):
    if not isinstance(value,dict) or set(value)!={'weekday_ordinals','annual_unavailable_months'}:
        raise ValueError('Calendar patterns require weekday_ordinals and annual_unavailable_months')
    windows, months = value['weekday_ordinals'], value['annual_unavailable_months']
    if not isinstance(windows,list) or len(windows)>7 or not isinstance(months,list) or len(months)>12:
        raise ValueError('Invalid calendar pattern lists')
    normalized=[];seen=set()
    for window in windows:
        if not isinstance(window,dict) or set(window)!={'weekday','ordinals'}:
            raise ValueError('Invalid weekday ordinal pattern')
        day,ordinals=window['weekday'],window['ordinals']
        if (type(day) is not int or not 0<=day<=6 or day in seen
                or not isinstance(ordinals,list) or not 1<=len(ordinals)<=5
                or not all(type(n) is int and 1<=n<=5 for n in ordinals)
                or len(set(ordinals))!=len(ordinals)):
            raise ValueError('Weekdays require unique ordinal values from one to five')
        seen.add(day);normalized.append({'weekday':day,'ordinals':sorted(ordinals)})
    if not all(type(month) is int and 1<=month<=12 for month in months) or len(set(months))!=len(months):
        raise ValueError('Annual absence months must be unique numbers from one to twelve')
    return {'weekday_ordinals':sorted(normalized,key=lambda w:w['weekday']),
            'annual_unavailable_months':sorted(months)}


def calendar_reasons(preferences, starts_at, tz='America/Denver'):
    """Append to eligibility reasons; this restriction cannot make anyone eligible."""
    if KEY not in preferences:
        return []
    try:
        if not isinstance(starts_at,datetime) or starts_at.tzinfo is None:
            raise ValueError('Calendar restrictions need an aware event time')
        pattern=normalize_patterns(preferences[KEY])
        local=starts_at.astimezone(ZoneInfo(tz)).date()
    except (ValueError,TypeError,KeyError):
        return ['calendar preference needs valid weekday and annual absence mappings']
    if local.month in pattern['annual_unavailable_months']:
        return ['inside explicitly unavailable annual month']
    windows=pattern['weekday_ordinals']
    if windows and not any(w['weekday']==local.weekday() and (local.day-1)//7+1 in w['ordinals'] for w in windows):
        return ['outside stated weekday ordinal calendar pattern']
    return []


def calendar_dates(patterns, month):
    """Expand real calendar occurrences, including leap years and fifth weekdays."""
    pattern=normalize_patterns(patterns)
    if not isinstance(month,str) or len(month)!=7:
        raise ValueError('Use YYYY-MM')
    try:
        start=date.fromisoformat(month+'-01')
    except ValueError:
        raise ValueError('Use a valid YYYY-MM') from None
    if start.month in pattern['annual_unavailable_months']:
        return []
    if not pattern['weekday_ordinals']:
        raise ValueError('Annual absence alone does not establish positive availability dates')
    return [date(start.year,start.month,n).isoformat()
        for n in range(1,calendar.monthrange(start.year,start.month)[1]+1)
        if not calendar_reasons({KEY:pattern},datetime(start.year,start.month,n,tzinfo=ZoneInfo('UTC')),'UTC')]


def learned_patterns(session, volunteer, now, tz='America/Denver'):
    """Match the existing two-of-three-month rhythm as a read-only proposal.

    Missing assignments do not establish annual absence. Multiple services on
    the same calendar day do not constitute independent observations.
    """
    zone=ZoneInfo(tz)
    if now.tzinfo is None:
        raise ValueError('Serving history needs an aware clock')
    start=now.astimezone(zone).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    year,zero_month=divmod(start.year*12+start.month-1-3,12)
    history_start=start.replace(year=year,month=zero_month+1)
    rows=list(session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id==volunteer.id,m.Assignment.status=='completed',
        m.Event.status=='completed',m.Event.starts_at>=history_start,m.Event.starts_at<start
    ).order_by(m.Event.starts_at,m.Assignment.id).limit(1001)))
    if len(rows)>1000:
        return {'requires_review':True,'held':'Serving history exceeds the bounded evidence limit',
            'proposal':None,'evidence':[],'applied':False}
    observations=defaultdict(list)
    for assignment in rows:
        event=assignment.shift.event
        if event.ends_at<=event.starts_at:
            continue
        local=event.starts_at.astimezone(zone).date()
        observations[(local.weekday(),(local.day-1)//7+1)].append(
            {'assignment_id':assignment.id,'event_id':event.id,'date':local.isoformat()})
    by_weekday=defaultdict(list);evidence=[]
    for (weekday,ordinal),items in sorted(observations.items()):
        months=sorted({item['date'][:7] for item in items})
        if len(months)>=2:
            by_weekday[weekday].append(ordinal)
            evidence.append({'weekday':weekday,'ordinal':ordinal,'observed_months':months,'observations':items})
    existing=normalize_patterns((volunteer.preferences or {}).get(KEY,{'weekday_ordinals':[],'annual_unavailable_months':[]}))
    proposal=normalize_patterns({'weekday_ordinals':[{'weekday':day,'ordinals':nums} for day,nums in by_weekday.items()],
        'annual_unavailable_months':existing['annual_unavailable_months']}) if by_weekday else None
    return {'volunteer_id':volunteer.id,'requires_review':True,'applied':False,
        'proposal':proposal,'evidence':evidence,
        'unavailable_months_inferred':False,'assignments_created':0,'messages_created':0}


def stage_pattern_review(session, volunteer, patterns, now, *, reason='Review explicit calendar preference proposal'):
    """Use the existing exact record review, preserving all unrelated fields.

    The application caller owns administrator/sender authentication. Staging
    only is permitted here; no history tick or model may approve this record.
    """
    from app.core import confirmations
    if now.tzinfo is None:
        raise ValueError('Pattern review needs an aware clock')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
        raise ValueError('A bounded review reason is required')
    pattern=normalize_patterns(patterns)
    before=confirmations.values(volunteer)
    payload={'action':'record_change','record':'Volunteer','record_id':volunteer.id,
        'before':before,'after':{'preferences':{**(volunteer.preferences or {}),KEY:pattern}},
        'reason':reason,'transport':session.info.get('conversation_origin','mock_or_twilio')}
    selected=session.info.get('mac_test_session')
    if selected:
        if not selected.active(now):
            raise ValueError('Selected review session expired')
        payload.update(phone=volunteer.phone,session_id=selected.id,session_starts_at=selected.starts_at.isoformat(),
            expires_at=min(now+timedelta(hours=2),selected.expires_at).isoformat())
    return confirmations.stage(session,now,payload,record=True)


def seasonal_staffing_report(session, month, now, tz='America/Denver'):
    """Compare verified slot counts; never invent attendance or add staffing.

    Same-calendar-month evidence must span two prior years. Recommendations
    compare like event types and roles; unknown events remain unresolved.
    """
    from app.core.scheduler import bounds
    if now.tzinfo is None:
        raise ValueError('Seasonal planning needs an aware clock')
    if not isinstance(month,str) or len(month)!=7:
        raise ValueError('Use YYYY-MM')
    start,end=bounds(month,tz)
    if month!=start.strftime('%Y-%m'):
        raise ValueError('Use YYYY-MM')
    local_now=now.astimezone(ZoneInfo(tz))
    offset=(start.year-local_now.year)*12+start.month-local_now.month
    if not 0<=offset<=12:
        raise ValueError('Choose the current month or one of the next twelve months')
    history_start=start.replace(year=max(1,start.year-3))
    events=list(session.scalars(select(m.Event).where(m.Event.starts_at>=history_start,m.Event.starts_at<end)
        .order_by(m.Event.starts_at,m.Event.id).limit(2001)))
    if len(events)>2000:
        return {'month':month,'requires_review':True,'held':'Seasonal evidence exceeds the bounded event limit','recommendations':[]}
    known_types=set(session.scalars(select(m.EventType.id)))
    known_roles=set(session.scalars(select(m.Role.id)))
    past=[];future=[];unknown=[]
    for event in events:
        local=event.starts_at.astimezone(ZoneInfo(tz))
        if event.ends_at<=event.starts_at or event.event_type_id not in known_types:
            if start<=event.starts_at<end and event.status=='scheduled' and event.starts_at>=now:
                unknown.append(event.id)
            continue
        if (event.status=='completed' and event.ends_at<=now and local.year<start.year and local.month==start.month):
            past.append(event)
        elif event.status=='scheduled' and start<=event.starts_at<end and event.starts_at>=now:
            future.append(event)
    histories=defaultdict(list)
    for event in past:
        counts=defaultdict(int)
        for shift in event.shifts:
            if shift.role_id in known_roles:
                counts[shift.role_id]+=1
        histories[event.event_type_id].append((event,dict(counts)))
    recommendations=[];insufficient=[]
    for event in future:
        samples=histories.get(event.event_type_id,[])
        years={old.starts_at.astimezone(ZoneInfo(tz)).year for old,_ in samples}
        if len(years)<2:
            insufficient.append(event.id);continue
        role_ids=sorted({role for _,counts in samples for role in counts})
        planned=defaultdict(int)
        for shift in event.shifts:
            if shift.role_id in known_roles:
                planned[shift.role_id]+=1
        for role_id in role_ids:
            observed=[counts.get(role_id,0) for _,counts in samples]
            typical=math.ceil(median(observed))
            if typical<=planned[role_id]:
                continue
            recommendations.append({'event_id':event.id,'event_type_id':event.event_type_id,'role_id':role_id,
                'current_slots':planned[role_id],'observed_median_slots':median(observed),
                'review_difference':typical-planned[role_id],
                'evidence':[{'event_id':old.id,'year':old.starts_at.astimezone(ZoneInfo(tz)).year,
                    'recorded_slots':counts.get(role_id,0)} for old,counts in samples],
                'suggested_action':'Review the event recipe against recorded seasonal staffing; no slots or invitations added.'})
    return {'month':month,'timezone':tz,'requires_review':True,'recommendations':recommendations,
        'unknown_event_ids':unknown,'insufficient_history_event_ids':insufficient,
        'assignments_created':0,'messages_created':0,'staffing_changed':False}

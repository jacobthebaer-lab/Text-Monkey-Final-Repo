"""Exact coordinator review completes a proven sender draft, never a booking."""
import calendar
import hashlib
import re
from copy import deepcopy
from datetime import date, timezone, timedelta
from sqlalchemy import select
from app.db import models as m
from app.core import confirmations, paired_planning, conversational_signup as natural
from app.core import signup_review_authority as read_authority
from app.integrations.mac_models import MacInboundReceipt

KEY = 'workflow_signup_preferences'
FIELDS = {'source_hash', 'event_mappings', 'window_ordinals', 'absence_months'}
DAYS = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
MONTHS = [name.lower() for name in calendar.month_name]
ORDINALS = {'first':1,'second':2,'third':3,'fourth':4,'fifth':5,'1st':1,'2nd':2,'3rd':3,'4th':4,'5th':5}


def _label(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold().replace(' only',''))


def group_windows(draft):
    windows=draft.get('recurring_windows',[]);indexes=set()
    for item in draft.get('pending_constraints',[]):
        if item.get('kind')=='event_mapping':
            indexes.update(i for i,w in enumerate(windows) if sorted(w['role_ids'])==sorted(item.get('role_ids',[])))
    named=any(natural._GROUP.fullmatch(item.get('proposal',{}).get('calendar_restriction',''))
        for item in draft.get('pending_constraints',[]))
    if named:
        days={day for item in draft.get('pending_constraints',[]) for day,name in enumerate(DAYS)
            if re.search(r'\b'+name+r's?\b',item.get('proposal',{}).get('calendar_restriction',''),re.I)}
        candidates=[i for i,w in enumerate(windows) if w['weekday'] in days]
        if len(candidates)==1:indexes.add(candidates[0])
        indexes.update(i for i,w in enumerate(windows) if w.get('event_context')
            and natural._GROUP.fullmatch(w['event_context']['label']))
    return indexes


def _clock_evidence(session, volunteer, incoming, selected, now):
    """Conservative clock limits from actual same-session audited replies.

    An event mapping cannot erase a stated clock restriction. This evidence is
    copied into the exact proposal, never substituted into historical rows.
    """
    from app.core.recurring_availability import normalize_recurring_windows
    roles=list(session.scalars(select(m.Role)));events=list(session.scalars(select(m.EventType)))
    evidence=[];seen=set()
    for item in reversed(read_authority.history(session,volunteer,now)):
        row=session.get(m.Message,item['incoming_id'])
        if row.id>incoming.id:continue
        for prefix in ('onboarding-turn-recovery:','onboarding-turn:'):
            turn=session.get(m.Notification,prefix+str(row.id))
            if not turn:continue
            detail=turn.detail or {};step=session.get(m.AgentStep,detail.get('step_id'))
            if (turn.purpose!='onboarding_turn' or turn.message_id!=row.id or turn.volunteer_id!=volunteer.id
                    or detail.get('session_id')!=selected.id or detail.get('phone')!=volunteer.phone
                    or detail.get('body_hash')!=hashlib.sha256(row.body.encode()).hexdigest()
                    or not step or step.type!='decision' or step.run.agent!='onboarding'
                    or step.result.get('stage')!='availability' or natural.digest(step.result)!=detail.get('step_hash')
                    or not row.created_at<=step.run.started_at<=step.created_at<=now
                    or not step.run.ended_at or not step.created_at<=step.run.ended_at<=now):continue
            extraction=step.result.get('extraction',{})
            if extraction.get('understood') is not True or extraction.get('sensitive') is not False:continue
            fingerprints={hashlib.sha256((row.phone+'\0'+service+'\0'+selected.id+'\0'+row.body).encode()).hexdigest()
                for service in ('SMS','iMessage')}
            receipts=[r for r in session.scalars(select(MacInboundReceipt).where(MacInboundReceipt.fingerprint.in_(fingerprints)))
                if r.result.get('session_id')==selected.id]
            if len(receipts)!=1:continue
            try:windows=normalize_recurring_windows(extraction.get('recurring_windows',[]),roles,events)
            except (ValueError,TypeError):continue
            grouped={}
            for window in windows:
                if window['role_ids'] and window.get('time_mode')=='clock' and window.get('start_time') and window.get('end_time'):
                    grouped.setdefault((window['weekday'],tuple(sorted(window['role_ids']))),[]).append(window)
            for key,matches in grouped.items():
                if key in seen:continue
                seen.add(key)
                if len(matches)!=1:
                    if any(w.get('time_mode')=='event' and (w['weekday'],tuple(sorted(w['role_ids'])))==key
                            for w in volunteer.preferences.get('onboarding_availability_draft',{}).get('recurring_windows',[])):
                        raise ValueError('Prior clock restrictions need an exact clarification before event mapping.')
                    continue
                evidence.append({'incoming_id':row.id,'body_hash':detail['body_hash'],
                    'step_id':step.id,'step_hash':detail['step_hash'],'window':matches[0],
                    'receipt_hash':natural.digest({'guid':receipts[0].guid,'fingerprint':receipts[0].fingerprint,'result':receipts[0].result})})
            break
    return evidence


def _clock_windows(draft, source):
    draft=deepcopy(draft)
    for window in draft.get('recurring_windows',[]):
        if window.get('time_mode')!='event':continue
        matches=[e['window'] for e in source['clock_evidence'] if e['window']['weekday']==window['weekday']
            and sorted(e['window']['role_ids'])==sorted(window['role_ids'])]
        if len(matches)==1:
            previous=matches[0]
            window.update(time_mode='clock',start_time=previous['start_time'],end_time=previous['end_time'],all_day=False)
    return draft


def _restriction_evidence(session,volunteer,selected,now):
    result=[]
    for item in read_authority.history(session,volunteer,now):
        row=session.get(m.Message,item['incoming_id'])
        fingerprints={hashlib.sha256((row.phone+'\0'+service+'\0'+selected.id+'\0'+row.body).encode()).hexdigest()
            for service in ('SMS','iMessage')}
        receipts=[r for r in session.scalars(select(MacInboundReceipt).where(MacInboundReceipt.fingerprint.in_(fingerprints)))
            if r.result.get('session_id')==selected.id]
        if len(receipts)!=1:continue
        for match in list(natural._CALENDAR.finditer(row.body))+list(natural._GROUP.finditer(row.body)):
            result.append({'calendar_restriction':match.group(0),'source_body_hash':hashlib.sha256(row.body.encode()).hexdigest(),
                'incoming_id':row.id,'receipt_hash':natural.digest({'guid':receipts[0].guid,'fingerprint':receipts[0].fingerprint,'result':receipts[0].result})})
    return result


def absence_months(draft,source):
    permitted=set()
    for item in draft.get('pending_constraints',[]):
        raw=item.get('proposal',{})
        if not natural._internal_evidence(raw):continue
        if not any(e['source_body_hash']==raw['source_body_hash'] and
                e['calendar_restriction'].casefold()==raw['calendar_restriction'].casefold()
                for e in source['restriction_evidence']):continue
        phrase=raw['calendar_restriction'].casefold()
        month=next((i for i,name in enumerate(MONTHS) if name and re.search(r'\b'+name+r'\b',phrase)),None)
        if month and re.search(r'\b(?:off|away|unavailable)\b',phrase):
            permitted.update(d[:7] for d in draft.get('unavailable_dates',[]) if int(d[5:7])==month)
    return sorted(permitted)


def current_source(session, volunteer, now):
    """Actual current Mac source, Gloo audit, consent and unchanged clearances."""
    if volunteer.preferences.get('onboarding_stage')!='availability':
        raise ValueError('This participant has no unfinished availability draft.')
    selected=session.info.get('mac_test_session')
    if not selected:
        raise ValueError('Review requires the current participant session.')
    from app.core.conversation import inbound_scope
    incoming=session.scalar(select(m.Message).where(inbound_scope(selected),m.Message.phone==volunteer.phone,
        m.Message.direction=='in',m.Message.status=='received').order_by(m.Message.id.desc()).limit(1))
    if not incoming or read_authority.received(session,volunteer,incoming.id,now) is None:
        raise ValueError('The actual sender input, consent or session changed.')
    binding=None;turn_key=None
    for prefix in ('onboarding-turn-recovery:','onboarding-turn:'):
        proof={'incoming_id':incoming.id,'turn_key':prefix+str(incoming.id)}
        bound=read_authority.binding(session,volunteer,proof,now)
        if bound and bound['stage']=='availability':binding=bound;turn_key=proof['turn_key'];break
    if not binding:
        raise ValueError('The saved draft has no current source-bound Gloo interpretation.')
    fingerprints={hashlib.sha256((volunteer.phone+'\0'+service+'\0'+selected.id+'\0'+incoming.body).encode()).hexdigest()
        for service in ('SMS','iMessage')}
    receipts=[row for row in session.scalars(select(MacInboundReceipt).where(
        MacInboundReceipt.fingerprint.in_(fingerprints))) if row.result.get('session_id')==selected.id]
    if len(receipts)!=1:
        raise ValueError('The actual input needs one unambiguous durable native receipt.')
    roles=list(session.scalars(select(m.Role).order_by(m.Role.id)))
    events=list(session.scalars(select(m.EventType).order_by(m.EventType.id)))
    qualifications=list(session.scalars(select(m.Qualification).where(m.Qualification.volunteer_id==volunteer.id)
        .order_by(m.Qualification.id)))
    return {'volunteer_id':volunteer.id,'incoming_id':incoming.id,'turn_key':turn_key,'binding':binding,
        'original_session':{'id':selected.id,'starts_at':selected.starts_at.isoformat(),'expires_at':selected.expires_at.isoformat()},
        'receipt_guid':receipts[0].guid,'receipt_hash':natural.digest({'fingerprint':receipts[0].fingerprint,'result':receipts[0].result}),
        'before_hash':paired_planning.fingerprint(confirmations.values(volunteer)),
        'roles':[{'id':r.id,**confirmations.values(r)} for r in roles],
        'event_types':[{'id':e.id,**confirmations.values(e)} for e in events],
        'qualifications':[{'id':q.id,**confirmations.values(q)} for q in qualifications],
        'clock_evidence':_clock_evidence(session,volunteer,incoming,selected,now),
        'restriction_evidence':_restriction_evidence(session,volunteer,selected,now)}


def card(session, volunteer, now):
    source=current_source(session,volunteer,now)
    draft=_clock_windows(volunteer.preferences['onboarding_availability_draft'],source)
    role_names={r['id']:r['name'] for r in source['roles']}
    return {'volunteer_id':volunteer.id,'name':volunteer.name,'source_hash':natural.digest(source),
        'actual_reply':session.get(m.Message,source['incoming_id']).body,
        'windows':[{'index':i,**w,'roles':[role_names.get(r,'Unknown role') for r in w['role_ids']]}
            for i,w in enumerate(draft.get('recurring_windows',[]))],
        'constraints':draft.get('pending_constraints',[]),
        'group_windows':sorted(group_windows(draft)),
        'event_types':[{'id':e['id'],'name':e['name']} for e in source['event_types']],
        'unavailable_months':absence_months(draft,source),
        'role_caps':draft.get('role_frequency_caps',[])}


def _prepared(session, volunteer, choices, now):
    from app.core.onboarding import validated_availability, missing_frequency, missing_window_hours
    if set(choices)!=FIELDS or not isinstance(choices['source_hash'],str):
        raise ValueError('Use the displayed saved-preference review controls.')
    source=current_source(session,volunteer,now)
    if natural.digest(source)!=choices['source_hash']:
        raise ValueError('The saved preferences changed. Refresh before reviewing.')
    draft=_clock_windows(volunteer.preferences['onboarding_availability_draft'],source)
    windows=draft.get('recurring_windows',[])
    mappings=choices['event_mappings'];ordinals=choices['window_ordinals'];absences=choices['absence_months']
    if not all(isinstance(v,list) for v in (mappings,ordinals,absences)) or any(len(v)>64 for v in (mappings,ordinals,absences)):
        raise ValueError('Preference mappings exceed the review bounds.')
    events={e['id']:e for e in source['event_types']}
    seen=set()
    for mapping in mappings:
        if (not isinstance(mapping,dict) or set(mapping)!={'window_index','event_type_id'}
                or type(mapping['window_index']) is not int or not 0<=mapping['window_index']<len(windows)
                or mapping['window_index'] in seen or type(mapping['event_type_id']) is not int
                or mapping['event_type_id'] not in events or mapping['window_index'] not in group_windows(draft)):
            raise ValueError('Choose one current event group for each affected window.')
        seen.add(mapping['window_index']);w=windows[mapping['window_index']]
        label=(w.get('event_context') or {}).get('label') or events[mapping['event_type_id']]['name']
        w['event_context']={'label':label,'event_type_ids':[mapping['event_type_id']]}
    seen=set()
    for mapping in ordinals:
        if (not isinstance(mapping,dict) or set(mapping)!={'window_index','ordinals'}
                or type(mapping['window_index']) is not int or not 0<=mapping['window_index']<len(windows)
                or mapping['window_index'] in seen):
            raise ValueError('Choose an explicit weekday occurrence for each affected window.')
        seen.add(mapping['window_index']);windows[mapping['window_index']]['month_ordinals']=mapping['ordinals']
    known_months=set(absence_months(draft,source))
    if (any(not isinstance(month,str) or month not in known_months for month in absences)
            or len(set(absences))!=len(absences)):
        raise ValueError('Whole-month absence requires the matching actual month-off restriction.')
    for month in absences:
        year,number=map(int,month.split('-'))
        draft['unavailable_dates']=sorted(set(draft['unavailable_dates'])|
            {date(year,number,day).isoformat() for day in range(1,calendar.monthrange(year,number)[1]+1)})
    pairs=[]
    for item in draft.get('pending_constraints',[]):
        kind=item.get('kind');ids=item.get('role_ids',[])
        if kind=='same_day':
            pair=paired_planning.normalize(session,[{'role_ids':ids}])['same_day_role_pairs'][0]
            if pair not in pairs:pairs.append(pair)
        elif kind=='event_mapping':
            matching=[w for w in windows if sorted(w['role_ids'])==sorted(ids)]
            if not matching or any(not w.get('event_context') or not w['event_context']['event_type_ids']
                    or any(_label(events[i]['name'])!=_label(item['description']) for i in w['event_context']['event_type_ids']) for w in matching):
                raise ValueError('Choose the stated event group before completing preferences.')
        elif kind=='service_time':
            matching=[w for w in windows if set(ids).intersection(w['role_ids'])]
            if not matching or any(missing_window_hours(w) for w in matching):
                raise ValueError('The sender still needs to supply service times.')
        elif kind=='unresolved_window' and natural._internal_evidence(item.get('proposal')):
            phrase=item['proposal']['calendar_restriction'];lower=phrase.casefold()
            day=next((i for i,name in enumerate(DAYS) if re.search(r'\b'+name+r's?\b',lower)),None)
            occurrence=next((n for word,n in ORDINALS.items() if re.search(r'\b'+word+r'\b',lower)),None)
            if day is not None and occurrence is not None:
                matching=[w for w in windows if w['weekday']==day]
                if len(matching)!=1 or matching[0].get('month_ordinals')!=[occurrence]:
                    raise ValueError('Keep the stated weekday occurrence on its exact role window.')
            elif natural._GROUP.fullmatch(phrase):
                matching=[w for w in windows if w.get('event_context') and any(
                    _label(events[i]['name'])==_label(phrase) for i in w['event_context']['event_type_ids'])]
                if len(matching)!=1:
                    raise ValueError('Choose the exact named ministry group on its role window.')
            else:
                month=next((i for i,name in enumerate(MONTHS) if name and re.search(r'\b'+name+r'\b',lower)),None)
                if not month or not any(int(value[5:])==month for value in absences):
                    raise ValueError('The calendar restriction needs a dated exact review, not a broader rule.')
        else:
            raise ValueError('Some sender facts are still missing. Complete the targeted clarification first.')
    # Every addition must resolve actual preserved evidence, never invent fixed
    # weeks, a group restriction or positive permission absent from that draft.
    pending=draft.get('pending_constraints',[])
    for mapping in ordinals:
        w=windows[mapping['window_index']]
        if not any(item.get('kind')=='unresolved_window' and any(
                re.search(r'\b'+name+r's?\b',item.get('proposal',{}).get('calendar_restriction',''),re.I)
                for i,name in enumerate(DAYS) if i==w['weekday']) for item in pending):
            raise ValueError('Fixed weeks were not supplied for this role window.')
    for mapping in mappings:
        w=windows[mapping['window_index']]
        if not any((item.get('kind')=='event_mapping' and sorted(item['role_ids'])==sorted(w['role_ids']))
            or (item.get('kind')=='unresolved_window' and natural._GROUP.fullmatch(item.get('proposal',{}).get('calendar_restriction',''))
                and _label(events[mapping['event_type_id']]['name'])==_label(item['proposal']['calendar_restriction'])) for item in pending):
            raise ValueError('This group mapping has no corresponding preserved sender restriction.')
    roles=list(session.scalars(select(m.Role)));types=list(session.scalars(select(m.EventType)))
    validated=validated_availability(draft,draft,now.date(),roles=roles,event_types=types)
    if (not validated['availability_known'] or missing_frequency(validated,volunteer.preferences.get('signup_minimal_texts') is True)
            or any(missing_window_hours(w) for w in validated.get('recurring_windows',[]))
            or any(w.get('event_context') and not w['event_context']['event_type_ids'] for w in validated.get('recurring_windows',[]))):
        raise ValueError('The sender still needs to supply availability facts.')
    prefs=deepcopy(volunteer.preferences)
    prefs.pop('onboarding_availability_draft',None);prefs.pop('onboarding_clarifications',None)
    prefs.update(recurring_windows=validated.get('recurring_windows',[]),
        role_frequency_caps=validated.get('role_frequency_caps',[]),availability_weekdays=validated['weekdays'],
        preferred_services=validated['preferred_services'],availability_all_day=validated['all_day'],
        availability_frequency_known=validated['frequency_known'],onboarding_stage='complete',
        onboarding_completed_at=now.astimezone(timezone.utc).isoformat())
    if validated['frequency_known']:prefs['max_per_month']=validated['max_per_month']
    else:prefs.pop('max_per_month',None)
    normalized=paired_planning.normalize(session,pairs)
    if pairs:prefs.update(normalized)
    return source,validated,prefs,normalized


def stage(session, volunteer, choices, now):
    source,draft,prefs,rules=_prepared(session,volunteer,choices,now)
    # A second click at a later clock time must return the same still-current
    # exact review, rather than mint a different completion timestamp/hash.
    for existing in session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record',
            m.Approval.status=='pending').order_by(m.Approval.id)):
        prior=existing.payload.get(KEY,{})
        if prior.get('source')==source and prior.get('choices')==choices and review_problem(session,existing,now) is None:
            return existing
    before=confirmations.values(volunteer)
    payload={'action':'record_change','record':'Volunteer','record_id':volunteer.id,
        'before':before,'after':{**before,'preferences':prefs},
        'reason':'Complete the saved sender preferences after exact review of role links, event groups and calendar restrictions. No booking or clearance change.',
        KEY:{'source':source,'choices':deepcopy(choices),'validated_draft':draft}}
    if rules['same_day_role_pairs']:
        payload['workflow_planning_rules']={'volunteer_id':volunteer.id,'rules':rules,
            'roles':paired_planning.role_source(session,rules),'before_hash':paired_planning.fingerprint(before)}
    payload.update(transport='mac_messages',phone=volunteer.phone,session_id=source['original_session']['id'],
        session_starts_at=source['original_session']['starts_at'],expires_at=(now+timedelta(hours=2)).isoformat())
    return confirmations.stage(session,now,payload,record=True)


def review_problem(session,approval,now):
    try:
        data=approval.payload[KEY];source=data['source']
        person=session.get(m.Volunteer,source['volunteer_id'],populate_existing=True)
        current,draft,prefs,rules=_prepared(session,person,data['choices'],approval.requested_at)
        # Recheck current source at authoritative review time, including expiry.
        if current_source(session,person,now)!=source or current!=source or draft!=data['validated_draft']:
            raise ValueError()
        before=confirmations.values(person)
        if (approval.payload['record']!='Volunteer' or approval.payload['record_id']!=person.id
                or approval.payload['before']!=before or approval.payload['after']!={**before,'preferences':prefs}):
            raise ValueError()
    except (ValueError,TypeError,KeyError,AttributeError):
        return 'Saved signup facts, consent, source, catalog or clearances changed. Request a fresh exact review.'
    return None


def applied(session,approval,now):
    from app.core.onboarding import save_availability_dates
    data=approval.payload[KEY];source=data['source'];person=session.get(m.Volunteer,source['volunteer_id'])
    incoming=session.get(m.Message,source['incoming_id'])
    class Clock:
        def now(self):return now
    previous=approval.payload['before']['preferences']['onboarding_availability_draft']
    save_availability_dates(session,Clock(),person,data['validated_draft'],previous,incoming.body)
    row=session.get(m.Notification,source['turn_key'])
    review=session.get(m.Notification,row.detail.get('coordinator_review_key')) if row.detail.get('coordinator_review_key') else None
    if review:
        escalation=session.get(m.Escalation,review.detail['escalation_id'])
        escalation.status='resolved';review.state='resolved'

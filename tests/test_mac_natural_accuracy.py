"""Offline actual-input contracts, calendar evidence is never permission."""
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import onboarding
from app.core.conversational_signup import digest, followup_binding, sender_history
from app.core.signup_recovery import validate_conversational_reply
from app.db import models as m
from tests.test_conversational_signup import natural, NaturalGloo, PHONE, proposal

TEXT = ("Greeter Sunday 9:00 to 10:15 twice monthly; Production 11:00 to 12:15 on the same Sundays. "
        "Child Care for women's ministry only, second Wednesday 18:00 to 20:00 monthly. December off.")


def known_proposal():
    data = proposal()
    for window, weekday, start, end in zip(data['recurring_windows'], [6,6,2],
            ['09:00','11:00','18:00'], ['10:15','12:15','20:00']):
        window.update(weekday=weekday, time_mode='clock', start_time=start,
            end_time=end, event_context=None)
    return data


class ReviewGloo(NaturalGloo):
    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        if facts.get('recovery', {}).get('needs_coordinator'):
            self.calls.append(facts)
            recovery = facts['recovery']
            return SimpleNamespace(output_text=json.dumps({'stage':recovery['stage'], 'missing':[],
                'acknowledgment': 'Your draft preferences are saved locally, pending coordinator review of the calendar and role links.',
                'question':''}), usage=None)
        return super().create_response(**kwargs)


def test_known_calendar_and_ministry_are_held_not_silently_broadened_or_reasked(session,clock,provider,natural):
    person, incoming, selected, gate = natural
    incoming.body = 'Any two Sundays work. The weeks do not matter.'
    incoming.created_at = clock.now()-timedelta(seconds=1)
    current = m.Message(phone=PHONE, volunteer_id=person.id, direction='in', status='received',
        kind='mac_test_in', purpose=incoming.purpose, body=TEXT, created_at=clock.now())
    session.add(current);session.flush();gate.reply_to_message_id=current.id
    gloo=ReviewGloo(known_proposal())
    assert onboarding.handle(session,clock,gate,person,current.body,gloo)=='onboarding_pending_review'
    draft=person.preferences['onboarding_availability_draft']
    evidence=[item['proposal']['calendar_restriction'] for item in draft['pending_constraints']
        if item['kind']=='unresolved_window']
    assert {'second Wednesday', "women's ministry only", 'December off'} <= set(evidence)
    assert [w['weekday'] for w in draft['recurring_windows']]==[6,6,2]
    assert draft['recurring_windows'][2]['start_time']=='18:00'
    assert person.preferences['onboarding_stage']=='availability'
    assert 'calendar_patterns' not in person.preferences
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Qualification))
    assert gloo.calls[0]['sender_history'][0]['body']==incoming.body
    assert gloo.calls[1]['recovery']['missing']==[]
    assert '?' not in provider.sent[0].body
    review=session.scalar(select(m.Escalation).where(m.Escalation.category=='planning_preferences'))
    assert review.status=='open' and review.related_ids['incoming_id']==current.id
    assert session.get(m.Policy,'signup_recovery:'+PHONE+':availability') is None
    assert onboarding.handle(session,clock,gate,person,current.body,gloo)=='onboarding_suppressed'
    assert len(gloo.calls)==2 and len(provider.sent)==1
    assert len(session.scalars(select(m.Escalation).where(m.Escalation.category=='planning_preferences')).all())==1


def test_unknown_group_day_remains_a_real_question(session,clock,provider,natural):
    person,incoming,_,gate=natural;gloo=ReviewGloo()
    assert onboarding.handle(session,clock,gate,person,incoming.body,gloo)=='onboarding_clarify'
    assert 'window_schedule' in gloo.calls[-1]['recovery']['missing']
    assert '?' in provider.sent[0].body
    assert not session.scalar(select(m.Escalation).where(m.Escalation.category=='planning_preferences'))


def test_failed_extraction_after_logger_flush_is_durable_for_native_preflight(session,clock,provider,natural):
    person,incoming,selected,gate=natural
    person.preferences={**person.preferences, 'onboarding_availability_draft':{
        'availability_known':True,'frequency_known':True,'weekdays':[6],'all_day':True,
        'preferred_services':[],'max_per_month':2,'available_dates':[],'unavailable_dates':[],
        'pending_constraints':[{'kind':'same_day','description':'Link greeting and Production','role_ids':[1,3]}]}}
    assert onboarding.handle(session,clock,gate,person,incoming.body,NaturalGloo({'understood':False}))=='onboarding_clarify'
    key='onboarding-turn:'+str(incoming.id)
    saved_hash=session.get(m.Notification,key).detail['draft_hash']
    person_id=person.id;session.commit()
    with Session(session.bind) as fresh:
        fresh.info['mac_test_session']=selected
        restored=fresh.get(m.Volunteer,person_id)
        draft=restored.preferences['onboarding_availability_draft']
        assert any(p['kind']=='validation' for p in draft['pending_constraints'])
        assert any(p['kind']=='same_day' for p in draft['pending_constraints'])
        assert digest(draft)==saved_hash
        assert followup_binding(fresh,restored,{'incoming_id':incoming.id,'turn_key':key},clock.now())


@pytest.mark.parametrize('tamper',['resolved','source','draft','record'])
def test_internal_review_ack_requires_current_real_review_record(session,clock,provider,natural,tamper):
    person,incoming,_,gate=natural;incoming.body=TEXT
    onboarding.handle(session,clock,gate,person,incoming.body,ReviewGloo(known_proposal()))
    turn=session.get(m.Notification,'onboarding-turn:'+str(incoming.id))
    review=session.get(m.Notification,turn.detail['coordinator_review_key'])
    escalation=session.get(m.Escalation,review.detail['escalation_id'])
    if tamper=='resolved':escalation.status='resolved'
    if tamper=='source':escalation.related_ids={**escalation.related_ids,'incoming_id':999}
    if tamper=='draft':person.preferences={**person.preferences,'onboarding_availability_draft':{}}
    if tamper=='record':session.delete(review)
    session.flush()
    assert not followup_binding(session,person,{'incoming_id':incoming.id,'turn_key':turn.key},clock.now())


@pytest.mark.parametrize('ack,question',[
    ('Draft saved locally, pending review.','Which fixed Sundays work?'),
    ('The coordinator was notified.',''),('Everything is complete.',''),
    ('Your shifts are scheduled, pending review.','')])
def test_review_mode_rejects_redundant_questions_and_false_handoff(ack,question):
    with pytest.raises(ValueError):
        validate_conversational_reply(json.dumps({'stage':'availability','missing':[],
            'acknowledgment':ack,'question':question}),{'stage':'availability','missing':[],
                'needs_coordinator':True,'coordinator_review_key':'onboarding-coordinator:1'})


def test_sender_history_does_not_cross_session_phone_or_privacy(session,clock,natural):
    person,incoming,selected,_=natural
    for purpose,phone,body,created in [(incoming.purpose,PHONE,'Before current session',selected.starts_at-timedelta(seconds=1)),
            ('test:other',PHONE,'Other session',clock.now()),
            (incoming.purpose,'+15555550102','Other phone',clock.now())]:
        session.add(m.Message(phone=phone,volunteer_id=person.id,direction='in',status='received',
            kind='mac_test_in',purpose=purpose,body=body,created_at=created))
    session.flush()
    assert sender_history(session,person,clock.now())==[{'incoming_id':incoming.id,'body':incoming.body}]

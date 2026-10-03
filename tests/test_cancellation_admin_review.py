"""Internal cancellation context is visible only with actual sender/session proof."""
import json
from datetime import timedelta
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.core.inbound import handle_inbound
from app.llm.parser import ParsedMessage
from app.web.texty import admin
from tests.test_confirmations import mode_app  # noqa: F401


@pytest.fixture
def internal_review(mode_app, session, clock, make_shift, assign):
    app, people, _ = mode_app
    person=people[0]
    assign(person, make_shift('Greeter'))
    assign(person, make_shift('Production', starts=clock.now()+timedelta(days=8)))
    session.commit()
    selected=app.state.provider.test_sessions[person.phone]
    session.info['mac_test_session']=selected
    result=handle_inbound(session,clock,app.state.provider,person.phone,'Please cancel my shift, private fixture text.',
        lambda _:ParsedMessage(intent='cancel',confidence=1))
    assert result.routed_to=='cancellation_review'
    session.commit()
    review=session.scalar(select(m.Escalation).where(m.Escalation.category=='cancellation_scope'))
    app.dependency_overrides[admin]=lambda:{'email':'coordinator@example.test'}
    return app, people, review


def read(app):
    with TestClient(app) as client:
        response=client.get('/api/state')
        assert response.status_code==200, response.text
        return response.json()


def test_connected_cancellation_has_safe_actionable_context_and_no_text_count(internal_review, session):
    app, people, review=internal_review
    data=read(app)
    assert len(data['escalations'])==1 and data['proposals']==[]
    context=data['escalations'][0]['internal_review']
    assert context['recipient_name']==people[0].name and context['volunteer_id']==str(people[0].id)
    assert {b['role'] for b in context['bookings']}=={'Greeter','Production'}
    assert not context['scope_changed'] and context['delivery']=='internal_only'
    assert 'Shifts' in context['next_step']
    serialized=json.dumps(data['escalations'])
    assert 'private fixture text' not in serialized and people[0].phone not in serialized
    assert 'source_body_hash' not in serialized and 'session_id' not in serialized
    assert review.related_ids['bookings']==session.get(m.Notification,f'cancellation-scope:{people[0].id}').detail['bookings']
    with TestClient(app) as client:
        notices=client.get('/api/notification-status').json()['notifications']
    assert len(notices)==4 and all(n['notice'] in ('scheduled','day_before') for n in notices)
    assert session.scalar(select(m.Message).where(m.Message.direction=='out')) is None


@pytest.mark.parametrize('change',['source_body','source_phone','source_volunteer','source_kind','source_purpose',
    'source_time','source_status','review_session','review_transport','original_snapshot','expired_session','changed_session','volunteer_phone'])
def test_changed_actor_or_provenance_hides_internal_context(internal_review, session, clock, change):
    app, people, review=internal_review
    source=session.get(m.Message,review.related_ids['message_id'])
    if change=='source_body':source.body='Changed text'
    elif change=='source_phone':source.phone=people[1].phone
    elif change=='source_volunteer':source.volunteer_id=people[1].id
    elif change=='source_kind':source.kind='google_voice_test_in'
    elif change=='source_purpose':source.purpose='test:other-session'
    elif change=='source_time':source.created_at=clock.now()+timedelta(days=1)
    elif change=='source_status':source.status='draft'
    elif change=='review_session':review.related_ids={**review.related_ids,'session_id':'other-session'}
    elif change=='review_transport':review.related_ids={**review.related_ids,'transport':'google_voice'}
    elif change=='original_snapshot':review.related_ids={**review.related_ids,'bookings':[]}
    elif change=='volunteer_phone':people[0].phone='+12025550199'
    else:
        selected=app.state.provider.test_sessions[people[0].phone]
        app.state.provider.test_sessions[people[0].phone]=replace(selected,**(
            {'expires_at':clock.now()-timedelta(seconds=1)} if change=='expired_session' else {'id':'new-session'}))
    session.commit()
    assert read(app)['escalations']==[]


def test_changed_booking_scope_shows_current_context_without_reusing_original_details(internal_review, session):
    app, people, _=internal_review
    booking=session.scalar(select(m.Assignment).where(m.Assignment.volunteer_id==people[0].id))
    booking.status='cancelled';session.commit()
    context=read(app)['escalations'][0]['internal_review']
    assert context['scope_changed'] and len(context['bookings'])==1
    assert context['bookings'][0]['role']=='Production'
    assert 'Bookings have changed' in context['next_step']


def test_new_session_reopens_one_review_with_new_actual_trigger(internal_review, session, clock):
    app, people, review=internal_review
    old_source=review.related_ids['message_id']
    selected=replace(app.state.provider.test_sessions[people[0].phone],id='new-cancellation-session')
    app.state.provider.test_sessions[people[0].phone]=selected
    assert read(app)['escalations']==[]
    session.info['mac_test_session']=selected
    result=handle_inbound(session,clock,app.state.provider,people[0].phone,'Please cancel a shift',
        lambda _:ParsedMessage(intent='cancel',confidence=1))
    assert result.routed_to=='cancellation_review';session.commit()
    assert session.scalars(select(m.Escalation).where(m.Escalation.category=='cancellation_scope')).all()==[review]
    assert review.related_ids['message_id']!=old_source and review.related_ids['session_id']==selected.id
    assert len(read(app)['escalations'])==1


def test_unauthenticated_admin_cannot_read_internal_review(internal_review):
    app, _, _=internal_review
    app.dependency_overrides.pop(admin)
    with TestClient(app) as client:
        assert client.get('/api/state').status_code==401


@pytest.mark.parametrize('invalid',['unselected_sender','foreign_input_kind'])
def test_core_does_not_publish_review_for_unverified_trigger(mode_app, session, clock, make_shift, assign, invalid):
    app, people, _=mode_app
    person=people[0]
    assign(person,make_shift('Greeter'));assign(person,make_shift('Production'))
    session.commit()
    session.info['mac_test_session']=app.state.provider.test_sessions[
        people[1].phone if invalid=='unselected_sender' else person.phone]
    def parse(_):
        if invalid=='foreign_input_kind':
            source=session.scalar(select(m.Message).where(m.Message.direction=='in'))
            source.kind='google_voice_test_in';session.flush()
        return ParsedMessage(intent='cancel',confidence=1)
    result=handle_inbound(session,clock,app.state.provider,person.phone,'Please cancel my shift',parse)
    assert result.routed_to=='cancellation_review'
    assert session.scalar(select(m.Escalation).where(m.Escalation.category=='cancellation_scope')) is None
    assert session.scalar(select(m.Message).where(m.Message.direction=='out')) is None

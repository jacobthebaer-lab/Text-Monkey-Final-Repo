"""Parent approval through Mac queue boundaries; no Messages worker or real texts."""
from datetime import timedelta
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db import models as m
from app.core import confirmations
from app.integrations.mac_models import MacDeliveryClaim
from app.web import planning_workflows, texty
from tests.test_admin_setup import OWNER_A, OWNER_B
from tests.test_demo_acceptance_review import acceptance_app, OTHER_PHONE, pull

PATH = '/api/planning/availability-collections'


@pytest.fixture
def collection_mac(acceptance_app):
    client, app, gloo, clock = acceptance_app
    app.state.settings = replace(app.state.settings, competition_confirmation_required=True)
    app.state.session_factory.configure(info={confirmations.MODE_KEY: True})
    app.include_router(planning_workflows.router)
    user = {'id': OWNER_A, 'email': 'admin@example.test'}
    app.dependency_overrides[texty.admin] = lambda: user
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        session.add(m.Volunteer(name='Fictional Scope Person', phone=OTHER_PHONE,
            status='active', sms_opt_in=True, preferences={}, created_at=clock.now()))
        session.commit()
    response = client.post(PATH, json={'month': '2026-11'})
    assert response.status_code == 200, response.text
    yield client, app, gloo, clock, user, response.json()['collection']


def decide(client, parent, action):
    return client.post(f"{PATH}/{parent['id']}/{action}",
        json={'content_hash': parent['content_hash']})


def assert_no_queue(app):
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None
        assert session.scalar(select(MacDeliveryClaim)) is None
    assert not app.state.settings.automation_enabled


def test_signed_in_parent_never_queues_or_composes_implicitly(collection_mac):
    client,app,gloo,_,user,parent=collection_mac
    user['id']=OWNER_B
    assert decide(client,parent,'approve').status_code==404
    user['id']=OWNER_A
    approved=decide(client,parent,'approve')
    assert approved.status_code==200
    assert approved.json()['sent']==0 and approved.json()['text_review_ids']==[]
    assert_no_queue(app)
    prepared=decide(client,parent,'retry')
    assert prepared.status_code==200
    assert prepared.json()['text_review_ids']==[]
    assert prepared.json()['collection']['composition_status']=='blocked_policy'
    assert prepared.json()['collection']['suppressed_recipient_count']==1
    assert decide(client,parent,'retry').json()==prepared.json() and not gloo.calls
    with app.state.session_factory() as session:
        key=f"job:availability:{prepared.json()['collection']['collection_id']}:2:0"
        assert session.get(m.Policy,key).value['state']=='blocked_policy'
    assert_no_queue(app)


def test_rescoped_parent_blocks_already_queued_child_before_native_claim(collection_mac):
    # A historical collection queue row must still be blocked when its parent is
    # rescoped. Construct stored history directly; new collections cannot enqueue.
    from app.agents.fill_agent import FillContext
    from app.core import reminders
    from tests.test_planning_composition import historical_collection_review
    client,app,_,clock,_,parent=collection_mac
    approved=decide(client,parent,'approve').json()['collection']
    with app.state.session_factory() as session:
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==OTHER_PHONE))
        child=session.get(m.Approval,approved['collection_id'])
        ctx=FillContext(session,clock,app.state.provider,app.state.gloo)
        review=historical_collection_review(session,ctx,person,child)
        review.status='approved'
        assert reminders.delivery_problem(session,review,clock.now()) is None
        message=m.Message(direction='out',phone=person.phone,volunteer_id=person.id,
            body=review.payload['body'],purpose='availability_ask',kind='ai',status='queued',
            provider_sid=app.state.provider.test_sessions[person.phone].outbound_prefix+'historical-fixture',created_at=clock.now())
        session.add(message);session.flush();message_id=message.id
        review.payload={**review.payload,'message_id':message.id}
        session.add(m.Notification(key=f'confirmation:{message.id}',purpose='human_review',body='',state='sent',
            due_at=clock.now(),created_at=clock.now(),message_id=message.id,
            detail={'approval_id':review.id,'content_hash':review.payload['content_hash']}))
        session.info['record_authorized']=True
        person.name='Changed Fictional Scope Person';session.commit()
        review_id=review.id
    replacement=client.post(PATH,json={'month':'2026-11','request_id':str(uuid4())}).json()['collection']
    assert replacement['id']!=parent['id'] and replacement['status']=='pending'
    with app.state.session_factory() as session:
        assert session.get(m.Approval,parent['id']).status=='expired'
        assert reminders.delivery_problem(session,session.get(m.Approval,review_id),clock.now()) == 'Availability month/scope authorization changed or expired.'
    assert pull(client).json()['messages']==[]
    with app.state.session_factory() as session:
        assert session.get(m.Message,message_id).status == 'blocked_confirmation'
        assert session.scalar(select(MacDeliveryClaim)) is None
    assert not app.state.settings.automation_enabled


def test_expired_mac_session_holds_preparation_before_model_or_queue(collection_mac):
    client, app, gloo, clock, _, parent = collection_mac
    assert decide(client, parent, 'approve').status_code == 200
    clock.advance(timedelta(hours=1))
    response = decide(client, parent, 'retry')
    assert response.status_code == 200, response.text
    assert response.json()['text_review_ids'] == [] and not gloo.calls
    assert_no_queue(app)

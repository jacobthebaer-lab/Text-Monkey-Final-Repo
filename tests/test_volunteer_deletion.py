"""Delete/re-add through the admin API, using synthetic recipients and transport."""
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.web.texty import admin
from app.core.outbound_conversation import metadata
from tests.test_bulk_welcome import welcome_app
from tests.test_mac_messages import mac_app


def payload(phone, consent=True):
    return {'first_name': 'Clyde', 'last_name': 'Example', 'phone': phone, 'consent': consent}


def test_delete_readd_prepares_new_welcome_and_cancels_old_work(welcome_app):
    f = welcome_app; identifier = f.people[0]; phone = f.phones[0]
    f.app.state.gloo.create_response = lambda **kw: SimpleNamespace(output_text=json.loads(kw['input'])['approved_message'])
    with TestClient(f.app) as client:
        first = client.post(f'/api/volunteers/{identifier}/text-setup').json()
        with f.app.state.session_factory() as session:
            old = session.get(m.Volunteer, identifier)
            old_meta, error = metadata(session, purpose='signup_reply', volunteer=old, phone=phone,
                now=f.app.state.clock.now(), supplied={'intake_fields':['interests']})
            assert not error
            session.add(m.Qualification(volunteer_id=identifier, type='training', status='verified'))
            session.add(m.Availability(volunteer_id=identifier, month='2026-10', available_dates=['2026-10-11']))
            review = m.Approval(kind='confirm_text', payload={'phone':phone,'volunteer_id':identifier},
                status='pending', requested_at=f.app.state.clock.now())
            session.add(review); session.commit(); review_id = review.id
        result = client.delete(f'/api/volunteers/{identifier}')
        assert result.status_code == 200, result.text
        assert result.json()['texts_sent'] == 0
        assert str(identifier) not in [v['id'] for v in client.get('/api/state').json()['volunteers']]
        assert client.post(f'/api/volunteers/{identifier}', json={**payload(phone),'status':'active'}).status_code == 404
        assert client.post(f'/api/volunteers/{identifier}/text-setup').status_code == 404
        assert client.delete(f'/api/volunteers/{identifier}').status_code == 404
        new = client.post('/api/volunteers', json=payload(phone))
        assert new.status_code == 200, new.text
        fresh = new.json(); new_id = int(fresh['id'])
        assert new_id != identifier
        assert fresh['welcome']['message_id'] != first['message_id']
        assert fresh['welcome']['delivery'] != 'held'
        repeat = client.post(f'/api/volunteers/{new_id}/text-setup')
        assert repeat.status_code == 200 and repeat.json()['duplicate']
        with f.app.state.session_factory() as session:
            old = session.get(m.Volunteer, identifier); person = session.get(m.Volunteer,new_id)
            assert old.status == 'deleted' and not old.sms_opt_in and old.phone != phone
            assert session.get(m.Message,first['message_id']).status == 'blocked_deleted'
            assert session.get(m.Approval,review_id).status == 'cancelled'
            assert person.phone == phone and person.preferences['onboarding_stage'] == 'welcome_name'
            assert person.qualifications == []
            assert session.scalar(select(m.Availability).where(m.Availability.volunteer_id == new_id)) is None
            new_meta, error = metadata(session, purpose='signup_reply', volunteer=person, phone=phone,
                now=f.app.state.clock.now(), supplied={'intake_fields':['interests']})
            assert not error and set(old_meta['keys']).isdisjoint(new_meta['keys'])
            welcomes = session.scalars(select(m.Notification).where(m.Notification.purpose=='volunteer_welcome',
                m.Notification.volunteer_id.in_([identifier,new_id]))).all()
            assert len(welcomes) == 2


@pytest.mark.parametrize('defect', ['dispatching','uncertain','assignment','care'])
def test_delete_holds_without_mutating_when_work_requires_resolution(welcome_app, defect):
    f = welcome_app; identifier=f.people[0]; phone=f.phones[0]; now=f.app.state.clock.now()
    with f.app.state.session_factory() as session:
        if defect in {'dispatching','uncertain'}:
            session.add(m.Message(volunteer_id=identifier,phone=phone,direction='out',body='Synthetic text',
                status=defect,kind='ai',purpose='signup_reply',created_at=now))
        elif defect == 'care':
            session.add(m.Escalation(category='sensitive',severity='normal',summary='Synthetic care hold',
                related_ids={'volunteer_id':identifier},status='open',created_at=now))
        else:
            role=m.Role(name='Greeter',ministry='Welcome',criticality='standard',fill_policy='auto')
            event=m.Event(gcal_event_id='synthetic-delete',title='Synthetic service',starts_at=now,ends_at=now,status='active')
            session.add_all([role,event]);session.flush()
            shift=m.Shift(event_id=event.id,role_id=role.id,slot_index=1)
            session.add(shift);session.flush()
            session.add(m.Assignment(volunteer_id=identifier,shift_id=shift.id,status='confirmed',source='admin',created_at=now,updated_at=now))
        session.commit()
    with TestClient(f.app) as client:
        assert client.delete(f'/api/volunteers/{identifier}').status_code == 409
    with f.app.state.session_factory() as session:
        assert session.get(m.Volunteer,identifier).phone == phone
        assert session.get(m.Volunteer,identifier).status == 'active'


@pytest.mark.parametrize('hold', ['no_consent','opt_out','gloo'])
def test_readd_saves_fresh_profile_but_holds_welcome_when_required(welcome_app, hold):
    f=welcome_app; identifier=f.people[0]; phone=f.phones[0]
    with TestClient(f.app) as client:
        assert client.delete(f'/api/volunteers/{identifier}').status_code == 200
        if hold == 'opt_out':
            with f.app.state.session_factory() as session:
                session.add(m.Policy(key='sms_opt_out:'+phone,value={'value':True}));session.commit()
        if hold == 'gloo':
            from app.llm.gloo_client import GlooUnavailableError
            def unavailable(**kwargs): raise GlooUnavailableError('Synthetic outage')
            f.app.state.gloo.create_response=unavailable
        response=client.post('/api/volunteers',json=payload(phone, consent=hold!='no_consent'))
        assert response.status_code == 200, response.text
        assert response.json()['welcome']['delivery'] == 'held'
        with f.app.state.session_factory() as session:
            person=session.get(m.Volunteer,int(response.json()['id']))
            assert person.preferences.get('onboarding_stage') is None
            assert session.scalar(select(m.Message).where(m.Message.volunteer_id==person.id)) is None
            if hold=='opt_out': assert session.get(m.Policy,'sms_opt_out:'+phone).value['value']


def test_delete_requires_authenticated_admin(welcome_app):
    f=welcome_app; f.app.dependency_overrides.pop(admin)
    with TestClient(f.app) as client:
        assert client.delete(f'/api/volunteers/{f.people[0]}').status_code in {401,403,503}
    with f.app.state.session_factory() as session:
        assert session.get(m.Volunteer,f.people[0]).phone == f.phones[0]


def test_readd_under_exact_review_creates_one_new_review(welcome_app):
    from dataclasses import replace
    from app.core import confirmations
    f=welcome_app; identifier=f.people[0]; phone=f.phones[0]
    f.app.state.settings=replace(f.app.state.settings, competition_confirmation_required=True)
    f.app.state.session_factory.configure(info={confirmations.MODE_KEY:True})
    f.app.state.gloo.create_response=lambda **kw: SimpleNamespace(output_text=json.loads(kw['input'])['approved_message'])
    with TestClient(f.app) as client:
        first=client.post(f'/api/volunteers/{identifier}/text-setup')
        assert first.status_code == 200, first.text
        old_review=first.json()['approval_id']
        assert client.delete(f'/api/volunteers/{identifier}').status_code == 200
        fresh=client.post('/api/volunteers',json=payload(phone))
        assert fresh.status_code == 200, fresh.text
        welcome=fresh.json()['welcome']
        assert welcome['delivery']=='awaiting_confirmation' and welcome['approval_id']!=old_review
        assert client.post('/api/volunteers/'+fresh.json()['id']+'/text-setup').json()['duplicate']
    with f.app.state.session_factory() as session:
        assert session.get(m.Approval,old_review).status=='cancelled'
        assert session.get(m.Approval,welcome['approval_id']).status=='pending'
        assert session.scalar(select(m.Message)) is None

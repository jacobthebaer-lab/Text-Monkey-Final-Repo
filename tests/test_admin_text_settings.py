"""Admin onboarding and notification enrollment, using synthetic identities only."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from app.db import models as m
from app.agents.fill_agent import FillContext
from app.core.notifications import queue_pre_event_updates, queue_staffing
from app.core.send_gate import SendGate, SendStatus
from tests.test_admin_setup import setup_client, save, DETAILS, OWNER_B


def enable(client, phone='(202) 555-0199', **extra):
    return client.post('/api/setup/admin-texts', json={'phone':phone,'enabled':True,'consent':True,**extra})


@pytest.fixture
def live_admin_client(tmp_path):
    import time
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from app.clock import FakeClock
    from app.config import Settings
    from app.main import create_app
    from app.web.texty import admin
    from tests.test_admin_setup import OWNER_A
    from tests.session_fixtures import session_json
    from tests.test_pre_event_updates import SyntheticGloo
    start = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path}/readiness.db', demo_mode=False,
        automation_enabled=False, gloo_api_key='synthetic-key', sms_provider='mac_messages',
        mac_bridge_enabled=True, mac_bridge_token='synthetic-credential-'+'x'*40,
        admin_password='synthetic-password', mac_demo_phones='+12025550199',
        mac_test_sessions=session_json(['+12025550199'], start)))
    app.dependency_overrides[admin] = lambda: {'id':OWNER_A, 'email':'admin@example.test'}
    app.state.clock = FakeClock(start)
    app.state.mac_delivery_clock = FakeClock(start)
    app.state.mac_last_poll = time.monotonic()
    app.state.gloo = SyntheticGloo()
    with TestClient(app) as client:
        yield client, app


def test_checklist_separates_one_shot_readiness_from_scheduled_updates(live_admin_client):
    client, app = live_admin_client
    initial = client.get('/api/setup/admin-texts').json()
    missing = {c['code']: c for c in initial['checks'] if not c['ready']}
    assert missing['setup']['action'] == 'setup'
    assert missing['recipient']['action'] == 'mobile'
    assert missing['session']['action'] == 'mobile'
    assert not initial['connection_check_ready']
    save(client, complete=True); enable(client)
    status = client.get('/api/setup/admin-texts').json()
    assert status['connection_check_ready'] and not status['ready']
    assert [c['code'] for c in status['checks'] if not c['ready']] == ['event_schedule', 'scheduler']
    assert status['upcoming_event_count'] == 0 and status['next_event_at'] is None
    assert 'one-time' in status['checks'][-1]['next_step']
    assert status['session_starts_at'] and status['session_expires_at']
    assert status['session_ongoing'] is False
    from dataclasses import replace
    app.state.settings = replace(app.state.settings, automation_enabled=True)
    assert not client.get('/api/setup/admin-texts').json()['ready']
    with app.state.session_factory() as session:
        session.add(m.Event(title='Demo: Sunday service', status='scheduled',
            starts_at=app.state.clock.now()+timedelta(days=1),
            ends_at=app.state.clock.now()+timedelta(days=1, hours=1)))
        session.commit()
    status = client.get('/api/setup/admin-texts').json()
    assert not status['ready']  # Changing the flag did not start a timer.
    from types import SimpleNamespace
    app.state.background_scheduler = SimpleNamespace(running=True,
        get_jobs=lambda: [SimpleNamespace(id='fill_tick')])
    status = client.get('/api/setup/admin-texts').json()
    assert status['ready'] and status['upcoming_event_count'] == 1
    assert status['next_event_at']
    assert 'uptime' in status['checks'][-1]['detail']
    assert not app.state.gloo.calls
    with app.state.session_factory() as session:
        assert not session.scalar(select(m.Message))


def test_status_reports_only_validated_active_ongoing_mac_authority(live_admin_client):
    import hashlib
    import json
    from dataclasses import replace
    from app.integrations.mac_ongoing import authorize
    from app.sms.mac_provider import MacMessagesProvider
    client, app = live_admin_client
    save(client, complete=True); enable(client)
    phone = DETAILS['coordinator_phone']
    selected = app.state.provider.test_sessions[phone]
    config = {'backend_url':'http://localhost:61887', 'phones':[phone],
        'receiving_number':'+12025550200', 'services':['iMessage'], 'input_mode':'natural',
        'token':app.state.settings.mac_bridge_token, 'test_sessions':{phone:selected.spec()}}
    checkpoint = {'phones':[phone], 'receiving_number':config['receiving_number'],
        'services':['iMessage'], 'input_mode':'natural', 'test_sessions':config['test_sessions'],
        'after':10, 'dispatches':{}}
    configured = authorize(config, json.dumps(checkpoint).encode(), phone=phone, guid='synthetic-unread',
        row_id=12, body_hash=hashlib.sha256(b'Synthetic reply').hexdigest(),
        received_at=app.state.clock.now().isoformat(), actor='Explicit synthetic operator',
        operator_confirmed=True, now=app.state.clock.now())
    app.state.settings = replace(app.state.settings, mac_test_sessions=json.dumps(configured['test_sessions']),
        mac_ongoing_authorization=json.dumps(configured['ongoing_authorization']))
    app.state.provider = MacMessagesProvider(app.state.settings)
    app.state.mac_delivery_clock.advance(timedelta(days=30))
    status = client.get('/api/setup/admin-texts').json()
    assert status['session_ongoing'] is True and status['session_expires_at'] is None
    assert status['session_starts_at'] == selected.starts_at.isoformat()
    assert not status['ready']  # Ongoing transport does not enable scheduling.
    app.state.mac_delivery_clock.set_time(selected.starts_at-timedelta(seconds=1))
    assert client.get('/api/setup/admin-texts').json()['session_ongoing'] is False
    assert not app.state.gloo.calls
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None


def test_service_time_preferences_and_closed_events_do_not_establish_schedule_readiness(live_admin_client):
    from dataclasses import replace
    client, app = live_admin_client
    save(client, {**DETAILS, 'service_times':'Sunday 9AM / 11AM'}, complete=True)
    enable(client)
    app.state.settings = replace(app.state.settings, automation_enabled=True)
    with app.state.session_factory() as session:
        session.add_all([
            m.Event(title='Demo: cancelled', status='cancelled',
                starts_at=app.state.clock.now()+timedelta(days=1),
                ends_at=app.state.clock.now()+timedelta(days=1, hours=1)),
            m.Event(title='Demo: finished', status='scheduled',
                starts_at=app.state.clock.now()-timedelta(hours=2),
                ends_at=app.state.clock.now()-timedelta(hours=1))])
        session.commit()
    status = client.get('/api/setup/admin-texts').json()
    assert status['connection_check_ready'] and not status['ready']
    assert status['upcoming_event_count'] == 0 and status['next_event_at'] is None
    assert [c['code'] for c in status['checks'] if not c['ready']] == ['event_schedule', 'scheduler']
    assert 'do not create events' in status['issues'][0]
    assert not app.state.gloo.calls


def test_checklist_preserves_policy_stop_and_exact_session_boundaries(live_admin_client):
    from dataclasses import replace
    client, app = live_admin_client
    save(client, complete=True); enable(client)
    selected = app.state.provider.test_sessions.pop('+12025550199')
    status = client.get('/api/setup/admin-texts').json()
    assert not status['connection_check_ready'] and any('No Messages session' in i for i in status['issues'])
    app.state.provider.test_sessions['+12025550199'] = replace(selected, starts_at=app.state.clock.now()+timedelta(minutes=1))
    status = client.get('/api/setup/admin-texts').json()
    assert any('not started' in i for i in status['issues'])
    app.state.provider.test_sessions['+12025550199'] = selected
    app.state.mac_delivery_clock.set_time(selected.expires_at)
    status = client.get('/api/setup/admin-texts').json()
    assert not status['connection_check_ready'] and any('expired' in i for i in status['issues'])
    app.state.mac_delivery_clock.set_time(selected.starts_at)
    with app.state.session_factory() as session:
        # The global opt-out policy must block even if roster consent is stale.
        session.add(m.Policy(key='sms_opt_out:+12025550199',value={'value':True})); session.commit()
    status = client.get('/api/setup/admin-texts').json()
    assert not status['enabled'] and not status['connection_check_ready']
    assert any('START' in i for i in status['issues'])
    from uuid import uuid4
    assert client.post('/api/setup/admin-texts/send-check',json={'request_id':str(uuid4())}).status_code == 409
    assert not app.state.gloo.calls


def test_same_check_recovers_after_gloo_outage_with_scheduler_paused(live_admin_client):
    from uuid import uuid4
    from app.llm.gloo_client import GlooUnavailableError
    from tests.test_pre_event_updates import SyntheticGloo
    class UnavailableGloo(SyntheticGloo):
        def create_response(self, **kwargs):
            self.calls.append(kwargs)
            raise GlooUnavailableError('synthetic outage')
    client, app = live_admin_client
    save(client, complete=True); enable(client)
    app.state.gloo = UnavailableGloo()
    request = {'request_id':str(uuid4())}
    first = client.post('/api/setup/admin-texts/send-check',json=request).json()
    assert first['delivery'] == 'pending' and first['retry_at'] and not first['message_id']
    pending = client.get('/api/setup/admin-texts').json()['pending_check']
    assert pending['request_id'] == request['request_id'] and pending['retry_at'] == first['retry_at']
    assert client.post('/api/setup/admin-texts/send-check',json=request).json()['delivery'] == 'pending'
    assert len(app.state.gloo.calls) == 1  # Respect the retry backoff.
    app.state.gloo = SyntheticGloo()
    app.state.clock.advance(timedelta(minutes=2))
    recovered = client.post('/api/setup/admin-texts/send-check',json=request).json()
    assert recovered['delivery'] == 'queued_for_mac'
    assert client.post('/api/setup/admin-texts/send-check',json=request).json()['message_id'] == recovered['message_id']
    assert len(app.state.gloo.calls) == 1
    assert client.get('/api/setup/admin-texts').json()['pending_check'] is None
    with app.state.session_factory() as session:
        checks = session.scalars(select(m.Notification).where(m.Notification.key.startswith('admin-check:'))).all()
        assert len(checks) == 1 and checks[0].message_id == recovered['message_id']
        receipt = session.get(m.Notification, f"conversation-message:{recovered['message_id']}")
        assert receipt is not None and receipt.detail == {}
        assert len(session.scalars(select(m.Message)).all()) == 1


def test_setup_requires_mobile_normalizes_it_and_does_not_enable_texts(setup_client):
    client, app, _ = setup_client
    missing = {k:v for k,v in DETAILS.items() if k != 'coordinator_phone'}
    assert save(client, missing, True).status_code == 422
    response = save(client, {**DETAILS,'coordinator_phone':'(202) 555-0199'},True)
    assert response.json()['details']['coordinator_phone'] == '+12025550199'
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        assert not session.scalar(select(m.Volunteer))
        assert not session.scalar(select(m.Message))
    assert client.get('/api/setup/admin-texts').json()['enabled'] is False


@pytest.mark.parametrize('phone', ['123','2025550199 ext 2','+0123456789'])
def test_bad_mobile_cannot_complete_setup(setup_client,phone):
    assert save(setup_client[0], {**DETAILS,'coordinator_phone':phone},True).status_code == 422


def test_explicit_consent_creates_deduplicated_coordinator_and_truthful_readiness(setup_client):
    client, app, _ = setup_client
    assert enable(client).status_code == 409
    save(client,complete=True)
    assert enable(client,consent=False).status_code == 422
    result = enable(client).json()
    assert result['enabled'] and not result['ready']
    assert any('paused' in issue for issue in result['issues'])
    assert any('Messages' in issue for issue in result['issues'])
    assert enable(client).status_code == 200
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        volunteers = session.scalars(select(m.Volunteer)).all()
        assert len(volunteers) == 1 and volunteers[0].is_coordinator and volunteers[0].sms_opt_in
        assert not session.scalar(select(m.Message))  # Saving enrollment never sends a text.


def test_enrolled_admin_is_used_by_both_notification_paths_even_with_existing_coordinator(setup_client):
    client, app, _ = setup_client
    save(client,complete=True)
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        session.add(m.Volunteer(name='Legacy Sample',phone='+12025550111',is_coordinator=True,sms_opt_in=True,status='active',created_at=app.state.clock.now()))
        session.commit()
    enable(client)
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        event = m.Event(title='Synthetic gathering', starts_at=app.state.clock.now()+timedelta(hours=2),ends_at=app.state.clock.now()+timedelta(hours=3),status='scheduled')
        session.add(event); session.flush()
        context = FillContext(session,app.state.clock,app.state.provider,app.state.gloo)
        queue_pre_event_updates(context); queue_staffing(context,event)
        admin = session.scalar(select(m.Volunteer).where(m.Volunteer.phone=='+12025550199'))
        notices = session.scalars(select(m.Notification).where(m.Notification.volunteer_id==admin.id,
            m.Notification.purpose.in_(('coordinator_notify','escalation_notify')))).all()
        assert len(notices)==2
        assert {n.key.split(':')[0] for n in notices}=={'staffing','pre-event'}
        assert len(session.scalars(select(m.Notification).where(
            m.Notification.purpose.in_(('coordinator_notify','escalation_notify')))).all())==4


def test_owner_isolation_and_existing_roster_number_cannot_be_claimed(setup_client):
    client, app, user = setup_client
    save(client,complete=True); enable(client)
    user['id']=OWNER_B
    save(client,complete=True)
    result=client.get('/api/setup/admin-texts').json()
    assert not result['enabled'] and result['recent']==[]
    assert enable(client).status_code == 409
    assert enable(client,phone='+12025550198').status_code == 200
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone=='+12025550199')).status=='active'


def test_pause_and_number_change_cancel_pending_delivery_but_preserve_consent(setup_client):
    client, app, _ = setup_client
    save(client,complete=True); enable(client)
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        volunteer=session.scalar(select(m.Volunteer))
        session.add(m.Notification(key='synthetic-pending',volunteer_id=volunteer.id,purpose='coordinator_notify',body='Synthetic notice',state='pending',created_at=app.state.clock.now(),due_at=app.state.clock.now()))
        session.add(m.Message(volunteer_id=volunteer.id,phone=volunteer.phone,direction='out',kind='ai',purpose='coordinator_notify',body='Synthetic update',status='queued',created_at=app.state.clock.now()))
        session.commit()
    assert enable(client,phone='+12025550198').status_code==200
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        old=session.scalar(select(m.Volunteer).where(m.Volunteer.phone=='+12025550199'))
        assert old.status=='inactive' and old.sms_opt_in
        assert session.get(m.Notification,'synthetic-pending').state=='expired'
        assert session.scalar(select(m.Message)).status=='blocked_admin_updates'
        outcome=SendGate(session,app.state.clock,app.state.provider).send(volunteer=old,body='Synthetic',purpose='coordinator_notify')
        assert outcome.status==SendStatus.BLOCKED_ELIGIBILITY
    assert client.post('/api/setup/admin-texts',json={'enabled':False}).json()['enabled'] is False
    assert enable(client,phone='+12025550198').json()['enabled'] is True


def test_stop_is_not_overridden_by_settings_and_recent_delivery_is_reported(setup_client):
    client, app, _ = setup_client
    save(client,complete=True); enable(client)
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        volunteer=session.scalar(select(m.Volunteer)); volunteer.sms_opt_in=False
        session.add(m.Message(volunteer_id=volunteer.id,phone=volunteer.phone,direction='out',kind='ai',purpose='coordinator_notify',body='Synthetic event summary',status='queued',created_at=app.state.clock.now()))
        session.commit()
    assert enable(client).status_code==409
    status=client.get('/api/setup/admin-texts').json()
    assert not status['enabled'] and 'START' in status['issues'][0]
    assert status['recent'][0]['status']=='queued'
    with app.state.session_factory() as session:
        session.info["record_authorized"] = True
        session.add(m.Policy(key='sms_opt_out:+12025550197',value={'value':True}));session.commit()
    assert enable(client,phone='+12025550197').status_code==409


def test_send_check_requires_live_connection_and_saved_recipient(setup_client):
    from uuid import uuid4
    client,app,_=setup_client
    payload={'request_id':str(uuid4())}
    assert client.post('/api/setup/admin-texts/send-check',json=payload).status_code==409
    save(client,complete=True);enable(client)
    assert client.post('/api/setup/admin-texts/send-check',json=payload).status_code==503
    assert client.post('/api/setup/admin-texts/send-check',json={**payload,'phone':'+12025550199'}).status_code==422
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None


def test_real_transport_check_uses_gloo_once_and_never_requests_confirmation(tmp_path,clock):
    import time
    from datetime import datetime,timezone
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.web.texty import admin
    from tests.test_admin_setup import OWNER_A
    from tests.session_fixtures import session_json
    from tests.test_pre_event_updates import SyntheticGloo
    phone='+12025550199'
    app=create_app(Settings(database_url=f'sqlite:///{tmp_path}/admin-check.db',demo_mode=False,automation_enabled=False,
        gloo_api_key='synthetic-test-key',sms_provider='mac_messages',mac_bridge_enabled=True,
        mac_bridge_token='synthetic-credential-'+'x'*40,admin_password='synthetic-admin-password',
        mac_demo_phones=phone+',+12025550198',mac_test_sessions=session_json([phone,'+12025550198'],clock.now())))
    app.state.clock=clock
    app.state.mac_delivery_clock=clock
    app.dependency_overrides[admin]=lambda:{'id':OWNER_A,'email':'admin@example.test'}
    app.state.mac_last_poll=time.monotonic()
    app.state.gloo=SyntheticGloo()
    with TestClient(app) as client:
        assert save(client,complete=True).status_code==200
        assert enable(client).status_code==200
        request={'request_id':str(uuid4())}
        first=client.post('/api/setup/admin-texts/send-check',json=request)
        assert first.status_code==200 and first.json()['delivery']=='queued_for_mac'
        again=client.post('/api/setup/admin-texts/send-check',json=request)
        assert again.json()['message_id']==first.json()['message_id']
        assert len(app.state.gloo.calls)==1
        sessions=app.state.provider.test_sessions
        app.state.provider.test_sessions={}
        readiness=client.get('/api/setup/admin-texts').json()
        assert not readiness['ready'] and any('No Messages session' in issue for issue in readiness['issues'])
        app.state.provider.test_sessions=sessions
        with app.state.session_factory() as session:
            texts=session.scalars(select(m.Message)).all()
            assert len(texts)==1 and texts[0].phone==phone and texts[0].status=='queued'
            assert 'connection check' in texts[0].body
            assert session.scalar(select(m.Approval)) is None
        assert enable(client,phone='+12025550198').status_code==200
        mismatched=client.post('/api/setup/admin-texts/send-check',json=request)
        assert mismatched.status_code==409 and 'changed' in mismatched.json()['detail']
        assert len(app.state.gloo.calls)==1
        assert client.post('/api/setup/admin-texts/send-check',json={'request_id':str(uuid4())}).json()['delivery']=='queued_for_mac'
        client.post('/api/setup/admin-texts',json={'enabled':False})
        assert client.post('/api/setup/admin-texts/send-check',json={'request_id':str(uuid4())}).status_code==409


def test_admin_check_never_falls_back_to_template_when_gloo_fails(session,clock,provider,make_volunteer):
    from types import SimpleNamespace
    from app.config import Settings
    from app.core.notifications import deliver
    from app.llm.gloo_client import GlooUnavailableError
    admin=make_volunteer(coordinator=True)
    def unavailable(**kwargs):
        raise GlooUnavailableError('Synthetic Gloo outage')
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=False),create_response=unavailable)
    notice=deliver(FillContext(session,clock,provider,gloo),key='admin-check:synthetic-owner:synthetic-request',
        purpose='coordinator_notify',volunteer=admin,body='Synthetic connection check')
    assert not notice.message_id and notice.state=='pending'
    assert not provider.sent and session.scalar(select(m.Message)) is None
    assert notice.detail['gloo_attempts']==1

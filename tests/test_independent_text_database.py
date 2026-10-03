"""Independent HTTP-route acceptance; synthetic data and no network/transport."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from app.core.signup_copy import WELCOME
from tests.test_mac_messages import mac_app, PHONE, incoming, post
from tests.test_exact_signup_copy import ExactGloo, EXPECTED


@pytest.fixture
def signup_app(mac_app):
    app = mac_app
    app.state.settings = replace(app.state.settings, allow_text_signup=True,
                                 gloo_signup_replies=True, automation_enabled=False)
    app.state.gloo = ExactGloo()
    with app.state.session_factory() as s:
        s.delete(s.scalar(select(m.Volunteer)))
        s.add(m.Policy(key='signup_exact_copy:'+PHONE, value={'value':True}))
        s.commit()
    return app


def submit_queued(client, expected):
    rows = post(client, '/mac/outbound/pull').json()['messages']
    assert len(rows) == 1
    assert rows[0]['body'] == expected
    assert '\u2014' not in rows[0]['body']
    item = rows[0]
    reply = post(client, f"/mac/outbound/{item['id']}/ack",
                 {'token':item['token'], 'outcome':'submitted'})
    assert reply.status_code == 200
    assert reply.json()['status'] == 'submitted'  # never claim native delivery


def test_http_signup_provenance_commits_then_retry_is_idempotent(signup_app):
    app = signup_app
    stages = [('hello-proof', 'Hello', 'signup_invitation'),
              ('name-proof', 'Alex Example', 'onboarding_interests'),
              ('roles-proof', 'Anything', 'onboarding_availability'),
              ('availability-proof', 'Sundays and Wednesdays all day', 'onboarding_complete')]
    with TestClient(app) as client:
        for (guid, body, stage), expected in zip(stages, EXPECTED):
            data = incoming(guid, body)
            first = post(client, '/mac/inbound', data)
            assert first.status_code == 200, first.text
            assert first.json()['intent'] == stage
            repeat = post(client, '/mac/inbound', data)
            assert repeat.status_code == 200 and repeat.json()['duplicate']
            assert post(client, '/mac/inbound', {**data,'body':'changed'}).status_code == 409
            submit_queued(client, expected)
    with app.state.session_factory() as s:
        person = s.scalar(select(m.Volunteer))
        assert person.name == 'Alex Example' and person.sms_opt_in
        assert person.preferences['consent_source'] == 'sms_name_reply_to_exact_invitation'
        assert person.preferences['onboarding_stage'] == 'complete'
        assert person.preferences['availability_weekdays'] == [6,2]
        assert person.preferences['availability_frequency_known'] is False
        assert 'max_per_month' not in person.preferences
        assert not person.is_coordinator and not person.is_pastor
        assert s.scalar(select(m.Qualification)) is None
        assert s.scalar(select(m.Assignment)) is None
        assert s.get(m.Role, 5).fill_policy == 'needs_approval'
        assert 'background_check' in s.get(m.Role, 5).required_qualifications
        assert len(s.scalars(select(MacInboundReceipt)).all()) == 4
        inputs = s.scalars(select(m.Message).where(m.Message.direction=='in')).all()
        outputs = s.scalars(select(m.Message).where(m.Message.direction=='out')).all()
        assert len(inputs) == len(outputs) == 4
        assert all(row.purpose == 'test:'+app.state.provider.test_sessions[PHONE].id for row in inputs)
        assert all(row.status == 'submitted' for row in outputs)


def test_http_stop_dominates_old_name_reply_and_retry(signup_app):
    app = signup_app
    with TestClient(app) as client:
        post(client, '/mac/inbound', incoming('hello-stop', 'Hello'))
        submit_queued(client, WELCOME)
        post(client, '/mac/inbound', incoming('name-before-stop','Alex Example'))
        submit_queued(client, EXPECTED[1])
        assert post(client, '/mac/inbound', incoming('stop-proof','STOP')).json()['intent'] == 'stop'
        assert post(client, '/mac/inbound', incoming('name-before-stop','Alex Example')).json()['duplicate']
        # A later name and interests must never act as fresh START consent.
        post(client, '/mac/inbound', incoming('late-name-proof','Alex Example'))
        with app.state.session_factory() as s:
            person = s.scalar(select(m.Volunteer))
            assert not person.sms_opt_in


def test_http_profile_update_preserves_existing_privileges_and_qualifications(mac_app):
    app = mac_app
    app.state.gloo = ExactGloo()
    with app.state.session_factory() as s:
        person = s.scalar(select(m.Volunteer))
        person.is_coordinator = person.is_pastor = True
        person.preferences = {'onboarding_stage':'availability', 'signup_minimal_texts':True,
                              'unrelated_admin_setting':'keep'}
        s.add(m.Qualification(volunteer_id=person.id, type='background_check', status='verified',
                              verified_by='Admin', verified_at=app.state.clock.now()))
        s.commit()
    with TestClient(app) as client:
        result = post(client, '/mac/inbound', incoming('admin-profile-proof','Sundays and Wednesdays all day'))
        assert result.status_code == 200, result.text
        assert result.json()['intent'] == 'onboarding_complete'
    with app.state.session_factory() as s:
        person = s.scalar(select(m.Volunteer))
        assert person.is_coordinator and person.is_pastor
        assert person.preferences['unrelated_admin_setting'] == 'keep'
        qual = s.scalar(select(m.Qualification))
        assert qual.status == 'verified' and qual.verified_by == 'Admin'


@pytest.mark.parametrize('bad', [
    {'weekdays':[True], 'all_day':True},
    {'weekdays':[9], 'all_day':True},
    {'weekdays':[6], 'all_day':True, 'preferred_services':['sun_9']},
    {'weekdays':[6], 'all_day':False, 'preferred_services':['wednesday_9']},
])
def test_http_invalid_model_extraction_never_broadens_saved_availability(mac_app, bad):
    app = mac_app
    base = ExactGloo()
    original = base.create_response
    def response(**kwargs):
        facts = json.loads(kwargs['input'])
        if isinstance(facts, dict) and facts.get('stage') == 'availability':
            return SimpleNamespace(output_text=json.dumps({'understood':True,'availability_known':True,
                'frequency_known':False,'max_per_month':None,'preferred_services':[],
                'available_dates':[],'unavailable_dates':[], **bad}))
        return original(**kwargs)
    base.create_response = response
    app.state.gloo = base
    before = {'onboarding_stage':'availability','signup_minimal_texts':True,
              'availability_weekdays':[6],'preferred_services':['sun_9'], 'availability_all_day':False}
    with app.state.session_factory() as s:
        s.scalar(select(m.Volunteer)).preferences = before
        s.commit()
    with TestClient(app) as client:
        result = post(client, '/mac/inbound', incoming('invalid-profile','Synthetic invalid reply'))
        assert result.status_code == 200
        assert result.json()['intent'] in {'onboarding_clarify','onboarding_review'}
    with app.state.session_factory() as s:
        prefs = s.scalar(select(m.Volunteer)).preferences
        assert all(prefs[key] == value for key,value in before.items())


@pytest.mark.parametrize('failure', ['none','cloud_unavailable','cloud_opt_out'])
def test_http_inbound_profile_outbox_to_cloud_with_separate_identity(mac_app, tmp_path,failure):
    from app.core import profile_sync as sync
    from app.integrations.profile_models import ProfileOutbox
    from app.db.session import make_engine, make_session_factory
    app = mac_app
    app.state.settings = replace(app.state.settings, profile_sync_enabled=True,
                                 profile_sync_phones=PHONE)
    app.state.gloo = ExactGloo()
    cloud_engine = make_engine('sqlite:///'+str(tmp_path/'synthetic-cloud.db'))
    m.Base.metadata.create_all(cloud_engine)
    cloud_factory = make_session_factory(cloud_engine)
    with app.state.session_factory() as s:
        person = s.scalar(select(m.Volunteer))
        local_id = person.id
        person.preferences = {'signup_source':'sms','onboarding_stage':'availability',
                              'signup_minimal_texts':True}
        s.commit()
    with cloud_factory() as cloud:
        cloud.add(m.Volunteer(id=900, name='Cloud Previous', phone=PHONE, sms_opt_in=True,
                              status='active', is_coordinator=True, is_pastor=True,
                              preferences={'paused_roles':['Greeter'], 'owner':'cloud-only'},
                              created_at=app.state.clock.now()))
        cloud.add(m.Qualification(volunteer_id=900,type='background_check',status='verified',
                                  verified_by='Cloud Admin'))
        cloud.commit()
    with TestClient(app) as client:
        payload = incoming('profile-cloud-proof','Sundays and Wednesdays all day')
        assert post(client,'/mac/inbound',payload).json()['intent'] == 'onboarding_complete'
        assert post(client,'/mac/inbound',payload).json()['duplicate']
    with app.state.session_factory() as s:
        row = s.scalar(select(ProfileOutbox))
        assert row is not None, 'actual HTTP inbound route failed to enqueue profile cloud work'
        assert row.source_guid == payload['guid'] and row.state == 'pending'
        assert s.get(MacInboundReceipt,payload['guid']) is not None
        assert len(s.scalars(select(ProfileOutbox)).all()) == 1
        if failure == 'cloud_unavailable':
            def unavailable():raise RuntimeError('Synthetic cloud unavailable')
            assert sync.publish_pending(s,unavailable,app.state.settings)[0]['state'] == 'failed'
            assert row.cloud_id is None and row.synced_at is None
        if failure == 'cloud_opt_out':
            with cloud_factory() as cloud:
                cloud.get(m.Volunteer,900).sms_opt_in=False
                cloud.commit()
        results = sync.publish_pending(s,cloud_factory,app.state.settings)
        if failure == 'cloud_opt_out':
            assert results[0]['state'] == 'held' and results[0]['detail'] == 'cloud_opt_out_requires_review'
            with cloud_factory() as cloud:
                assert not cloud.get(m.Volunteer,900).sms_opt_in
                assert cloud.get(m.Volunteer,900).name == 'Cloud Previous'
                assert cloud.scalar(select(m.Availability)) is None
            cloud_engine.dispose()
            return
        assert results[0]['state'] == 'synced'
        assert row.cloud_id == 900 and row.cloud_id != local_id
        row.state = 'pending'  # simulate cloud commit followed by lost local ack
        s.commit()
        assert sync.publish_pending(s,cloud_factory,app.state.settings)[0]['detail'] == 'already_applied'
    with cloud_factory() as cloud:
        person = cloud.get(m.Volunteer,900)
        assert person.is_coordinator and person.is_pastor and person.sms_opt_in
        assert person.preferences['owner'] == 'cloud-only'
        assert person.preferences['paused_roles'] == ['Greeter']
        assert person.preferences['availability_weekdays'] == [6,2]
        assert person.preferences['onboarding_stage'] == 'complete'
        assert len(cloud.scalars(select(m.Volunteer).where(m.Volunteer.phone==PHONE)).all()) == 1
        availability = cloud.scalar(select(m.Availability))
        assert availability.volunteer_id == 900 and availability.raw_reply is None
        assert availability.unavailable_dates == ['2026-10-18']
        assert cloud.scalar(select(m.Qualification)).verified_by == 'Cloud Admin'
    cloud_engine.dispose()


def test_http_cloud_sync_never_discards_role_time_restrictions(mac_app,tmp_path):
    from app.core import profile_sync as sync
    from app.integrations.profile_models import ProfileOutbox
    from app.db.session import make_engine,make_session_factory
    app=mac_app
    app.state.settings=replace(app.state.settings,profile_sync_enabled=True,profile_sync_phones=PHONE)
    gloo=ExactGloo();original=gloo.create_response
    window={'weekday':6,'role_ids':[11],'role_label':'Greeter','any_role':False,
            'start_time':'08:00','end_time':'10:00','all_day':False,'event_context':None}
    def response(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and facts.get('stage')=='availability':
            return SimpleNamespace(output_text=json.dumps({'understood':True,'availability_known':True,
                'frequency_known':True,'max_per_month':2,'weekdays':[6],'all_day':False,
                'preferred_services':[],'available_dates':[],'unavailable_dates':[],
                'recurring_windows':[window]}))
        return original(**kwargs)
    gloo.create_response=response;app.state.gloo=gloo
    with app.state.session_factory() as s:
        s.add(m.Role(id=11,name='Greeter',ministry='Hospitality',required_qualifications=[],
                     fill_policy='auto',criticality='standard'))
        s.scalar(select(m.Volunteer)).preferences={'signup_source':'sms','signup_minimal_texts':True,
            'onboarding_stage':'availability','interested_roles':['Greeter'],'recurring_windows':[window]}
        s.commit()
    engine=make_engine('sqlite:///'+str(tmp_path/'restricted-cloud.db'))
    m.Base.metadata.create_all(engine);factory=make_session_factory(engine)
    with factory() as cloud:
        cloud.add(m.Role(id=71,name='Greeter',ministry='Hospitality',required_qualifications=[],
                         fill_policy='auto',criticality='standard'))
        cloud.commit()
    with TestClient(app) as tc:
        result=post(tc,'/mac/inbound',incoming('role-window-http','I can greet Sundays 8 to 10am twice a month'))
        assert result.status_code==200,result.text
        assert result.json()['intent']=='onboarding_complete'
    with app.state.session_factory() as local:
        assert local.scalar(select(m.Volunteer)).preferences['recurring_windows']==[window]
        row=local.scalar(select(ProfileOutbox))
        assert row is not None,'Actual restricted profile must persist a queue receipt or explicit hold'
        if row.state in {'pending','failed'}:
            sync.publish_pending(local,factory,app.state.settings)
        assert row.state in {'synced','held'}
        with factory() as cloud:
            person=cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE))
            if row.state=='held':
                assert person is None
            else:
                assert person is not None
                windows=person.preferences.get('recurring_windows')
                assert windows,'Synced profile silently dropped restrictive recurring windows'
                assert windows==[{**window,'role_ids':[71]}],'Cloud window widened or copied local catalogue IDs'
    engine.dispose()


def test_http_authoritative_assignment_accept_cancel_reaches_verified_pco(mac_app,tmp_path,monkeypatch):
    from app.main import create_app
    from app.llm.parser import ParsedMessage
    from app.integrations.planning_center import PCOConfig, PCOStaffingIntent, PCOStaffingLink
    from app.integrations.planning_center_staffing import map_position,map_volunteer,process_staffing_outbox
    from tests.test_planning_center import CONFIG,client as pco_fixture,sync_schedule
    from tests.test_planning_center_staffing import StaffingAPI
    from tests.test_fill_agent import ScriptedAgentGloo
    monkeypatch.setattr(PCOConfig,'from_env',classmethod(lambda cls:CONFIG))
    app = create_app(replace(mac_app.state.settings,pco_staffing_write_enabled=True,
                            database_url='sqlite:///'+str(tmp_path/'staffing-http.db'),
                            automation_enabled=False))
    app.state.clock = app.state.mac_delivery_clock = mac_app.state.clock
    app.state.gloo = ScriptedAgentGloo()
    with app.state.session_factory() as s:
        with pco_fixture(quantity=1) as api:
            sync_schedule(s,api,CONFIG)
        shift = s.scalar(select(m.Shift))
        api = StaffingAPI(starts=shift.event.starts_at,ends=shift.event.ends_at)
        person = m.Volunteer(name='Alex Example',phone=PHONE,sms_opt_in=True,status='active',
                             preferences={},created_at=app.state.clock.now())
        s.add(person);s.flush()
        map_volunteer(s,CONFIG,person.id,'70',app.state.clock.now(),client=api)
        map_position(s,api,CONFIG,shift_id=shift.id,team_id='30',position_id='90',plan_time_id='60',now=app.state.clock.now())
        assignment = m.Assignment(shift_id=shift.id,volunteer_id=person.id,status='approved',source='admin',
                                  created_at=app.state.clock.now(),updated_at=app.state.clock.now())
        s.add(assignment);s.commit();assignment_id=assignment.id
        assert s.scalar(select(PCOStaffingIntent)) is None
    monkeypatch.setattr('app.web.mac_messages.parse_inbound',lambda gloo,body:ParsedMessage(intent='confirm',confidence=1))
    with TestClient(app) as tc:
        data = incoming('accepted-assignment-http','Yes')
        assert post(tc,'/mac/inbound',data).json()['intent'] == 'confirmed'
        assert post(tc,'/mac/inbound',data).json()['duplicate']
    with app.state.session_factory() as s:
        intent = s.scalar(select(PCOStaffingIntent))
        assert intent and intent.state == 'pending' and intent.action == 'accept'
        assert intent.person_id == '70' and intent.assignment_id == assignment_id
        assert s.get(m.Assignment,assignment_id).status == 'confirmed'
        assert len(s.scalars(select(PCOStaffingIntent)).all()) == 1
    assert process_staffing_outbox(app.state.session_factory,api,CONFIG,app.state.clock.now(),enabled=True)['verified'] == 1
    assert len(api.writes) == 1 and api.rows[0]['attributes']['status'] == 'C'
    assert process_staffing_outbox(app.state.session_factory,api,CONFIG,app.state.clock.now(),enabled=True)['verified'] == 0
    monkeypatch.setattr('app.web.mac_messages.parse_inbound',lambda gloo,body:ParsedMessage(intent='cancel',confidence=1))
    with TestClient(app) as tc:
        data=incoming('cancelled-assignment-http',"I can't serve")
        result=post(tc,'/mac/inbound',data)
        assert result.status_code == 200,result.text
        assert result.json()['intent'] == 'fill_agent'
        assert post(tc,'/mac/inbound',data).json()['duplicate']
    with app.state.session_factory() as s:
        assert s.get(m.Assignment,assignment_id).status == 'cancelled'
        rows=s.scalars(select(PCOStaffingIntent).order_by(PCOStaffingIntent.id)).all()
        assert len(rows) == 2 and rows[-1].state == 'pending' and rows[-1].action == 'cancel'
    assert process_staffing_outbox(app.state.session_factory,api,CONFIG,app.state.clock.now(),enabled=True)['verified'] == 1
    assert api.rows[0]['attributes']['status'] == 'D' and len(api.writes) == 2
    with app.state.session_factory() as s:
        assert s.get(PCOStaffingLink,assignment_id).remote_status == 'D'
        assert all(row.state=='verified' for row in s.scalars(select(PCOStaffingIntent)))
        assert len(s.scalars(select(MacInboundReceipt)).all()) == 2
    assert all(data['data']['attributes']['prepare_notification'] is False for _,_,data in api.writes)

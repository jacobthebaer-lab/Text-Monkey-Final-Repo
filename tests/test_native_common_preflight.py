"""Every native claim gets fresh transport, consent and quiet-hour checks."""
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.core.send_gate import SendGate
from app.core import confirmations
from app.db import models as m
from tests.test_mac_messages import mac_app, PHONE, post  # noqa: F401


def claim(mac_app, client, purpose):
    clock=mac_app.state.clock
    with mac_app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer))
        if purpose=='signup_reply':
            volunteer.preferences={'signup_source':'sms','onboarding_stage':'interests'}
        if purpose=='stop_confirm':
            import json
            from types import SimpleNamespace
            from app.agents.fill_agent import FillContext
            from app.core.send_gate import handle_stop_start
            from app.core.notifications import flush_due
            class ControlGloo:
                def create_response(self, **kwargs):
                    return SimpleNamespace(output_text=json.loads(kwargs['input'])['approved_message'],usage=None)
            handle_stop_start(session, clock, mac_app.state.provider, volunteer, 'STOP')
            flush_due(FillContext(session, clock, mac_app.state.provider, ControlGloo()))
            session.commit()
        if purpose=='coordinator_notify':
            volunteer.is_coordinator=True
        session.flush()
        if purpose != 'stop_confirm':
            outcome=SendGate(session,clock,mac_app.state.provider).send(body='Synthetic essential text.',purpose=purpose,
                volunteer=volunteer,conversation={'intake_fields':['interests']} if purpose=='signup_reply' else None)
            assert outcome.sent
        session.commit()
    response=post(client,'/mac/outbound/pull')
    assert response.status_code==200
    items=response.json()['messages']
    assert len(items)==1
    return items[0]


@pytest.mark.parametrize('purpose',['signup_reply','coordinator_notify'])
@pytest.mark.parametrize('change',['consent','stop','phone_hold','volunteer_hold','allowlist','session','origin','quiet'])
def test_every_claim_rechecks_current_restrictions_before_native(mac_app,purpose,change):
    with TestClient(mac_app) as client:
        item=claim(mac_app,client,purpose)
        with mac_app.state.session_factory() as session:
            volunteer=session.scalar(select(m.Volunteer))
            if change=='consent':
                volunteer.sms_opt_in=False
            elif change=='stop':
                session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
            elif change in ('phone_hold','volunteer_hold'):
                session.add(m.Escalation(category='sensitive',severity='normal',summary='Synthetic hold',status='open',
                    related_ids={'phone':PHONE} if change=='phone_hold' else {'volunteer_id':volunteer.id},created_at=mac_app.state.clock.now()))
            elif change=='origin':
                session.get(m.Message,item['id']).provider_sid='MACdifferent-origin'
            elif change=='quiet':
                session.add(m.Policy(key='quiet_hours',value={'value':{'start':'09:00','end':'11:00'}}))
            session.commit()
        if change=='allowlist':
            mac_app.state.provider.phones=frozenset()
        elif change=='session':
            mac_app.state.mac_delivery_clock.advance(timedelta(hours=24))
        response=post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']})
        assert response.status_code==409
        with mac_app.state.session_factory() as session:
            row=session.get(m.Message,item['id'])
            assert row.status.startswith('blocked_') and row.status!='dispatching'


def test_real_scoped_intake_claim_and_final_check_reconstruct_same_scope(mac_app):
    with TestClient(mac_app) as client:
        item=claim(mac_app,client,'signup_reply')
        response=post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']})
        assert response.status_code==200 and response.json()['verified']


def test_pending_signup_exception_remains_but_stop_policy_overrides_it(mac_app):
    with TestClient(mac_app) as client:
        item=claim(mac_app,client,'signup_reply')
        with mac_app.state.session_factory() as session:
            volunteer=session.scalar(select(m.Volunteer));volunteer.sms_opt_in=False
            volunteer.preferences={**volunteer.preferences,'consent_pending':True};session.commit()
        assert post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']}).status_code==200
        with mac_app.state.session_factory() as session:
            session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}));session.commit()
        assert post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']}).status_code==409


def test_initial_preconsent_name_intake_pulls_and_verifies_in_real_scope(mac_app):
    with mac_app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer))
        volunteer.sms_opt_in=False
        volunteer.preferences={'signup_source':'sms','consent_pending':True}
        session.flush()
        outcome=SendGate(session,mac_app.state.clock,mac_app.state.provider).send(
            body='Please provide your first and last name.',purpose='signup_reply',volunteer=volunteer,
            conversation={'intake_fields':['name']})
        assert outcome.sent;session.commit()
    with TestClient(mac_app) as client:
        item=post(client,'/mac/outbound/pull').json()['messages'][0]
        assert post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']}).status_code==200


@pytest.mark.parametrize('stop_after_claim',[False,True])
def test_explicit_manual_proof_survives_common_guard_without_bypassing_later_stop(mac_app,stop_after_claim):
    with mac_app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer));clock=mac_app.state.clock
        gate=SendGate(session,clock,mac_app.state.provider)
        proposal=gate.send(body='Exact human draft.',purpose='manual',volunteer=volunteer)
        approval=session.get(m.Approval,proposal.approval_id)
        confirmations.decide(session,gate,approval,approve=True,actor='admin@example.test',
            expected=approval.payload['content_hash'],now=clock.now());session.commit()
    with TestClient(mac_app) as client:
        item=post(client,'/mac/outbound/pull').json()['messages'][0]
        if stop_after_claim:
            with mac_app.state.session_factory() as session:
                session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}));session.commit()
        response=post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token'],'content_hash':item['content_hash']})
        assert response.status_code==(409 if stop_after_claim else 200)


def test_stop_confirmation_preserves_optout_and_quiet_exception(mac_app):
    with TestClient(mac_app) as client:
        item=claim(mac_app,client,'stop_confirm')
        with mac_app.state.session_factory() as session:
            session.scalar(select(m.Volunteer)).sms_opt_in=False
            session.add(m.Policy(key='quiet_hours',value={'value':{'start':'09:00','end':'11:00'}}))
            session.commit()
        response=post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']})
        assert response.status_code==200


@pytest.mark.parametrize('change', ['body', 'source'])
def test_native_control_claim_requires_persisted_composed_source(mac_app, change):
    with TestClient(mac_app) as client:
        item = claim(mac_app, client, 'stop_confirm')
        with mac_app.state.session_factory() as session:
            row = session.get(m.Message, item['id'])
            if change == 'body':
                row.body = 'An unrelated offer disguised as a control acknowledgment.'
            else:
                receipt = session.get(m.Notification, f'conversation-message:{row.id}')
                receipt.detail = {}
            session.commit()
        response = post(client, f"/mac/outbound/{item['id']}/verify", {'token':item['token']})
        assert response.status_code == 409

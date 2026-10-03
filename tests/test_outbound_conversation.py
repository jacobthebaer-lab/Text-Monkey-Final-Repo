"""Only intentional, source-backed volunteer contact reaches a mock transport."""
from datetime import timedelta
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import confirmations
from app.core.send_gate import SendGate, SendStatus, handle_stop_start
from app.db import models as m
from app.integrations.mac_messages import MacWorker
from app.integrations.mac_models import MacDeliveryClaim
from tests.test_mac_messages import mac_app, PHONE, post, config, ReaderFixture  # noqa: F401
from tests.session_fixtures import session_id


@pytest.mark.parametrize('purpose', ['thanks', 'filled_thanks', 'cancellation_ack', 'clarify',
    'clarify_shift', 'availability_ask', 'outreach', 'signup_reply', 'admin_reply', 'coordinator_notify', 'escalation_notify'])
def test_routine_chatter_creates_no_text_review_or_model_call(gate, session, provider, make_volunteer, purpose):
    volunteer = make_volunteer()
    gate.gloo = SimpleNamespace(create_response=lambda **kw: pytest.fail('Suppression must precede composition'))
    session.info[confirmations.MODE_KEY] = True
    result = gate.send(body='I am checking on that for you.', purpose=purpose, volunteer=volunteer)
    assert result.status == SendStatus.BLOCKED_POLICY
    assert not provider.sent and session.scalar(select(m.Message)) is None
    assert session.scalar(select(m.Approval)) is None
    assert session.scalar(select(m.Notification)).detail['reason'] == result.reason


def test_intake_prompts_only_missing_facts_once_without_blocking_later_new_question(gate, session, provider, make_volunteer):
    volunteer = make_volunteer(prefs={'signup_source': 'sms', 'onboarding_stage': 'interests'})
    assert gate.send(body='What roles?', purpose='signup_reply', volunteer=volunteer,
                     conversation={'intake_fields': ['interests']}).sent
    assert gate.send(body='Please choose roles.', purpose='signup_reply', volunteer=volunteer,
                     conversation={'intake_fields': ['interests']}).status == SendStatus.BLOCKED_POLICY
    volunteer.preferences = {**volunteer.preferences, 'interested_roles': ['Greeter'], 'onboarding_stage': 'availability',
        'onboarding_availability_draft': {'availability_known': True, 'frequency_known': False}}
    assert gate.send(body='Which days?', purpose='signup_reply', volunteer=volunteer,
                     conversation={'intake_fields': ['availability']}).status == SendStatus.BLOCKED_POLICY
    assert gate.send(body='How often?', purpose='signup_reply', volunteer=volunteer,
                     conversation={'intake_fields': ['frequency']}).sent
    assert gate.send(body='All set!', purpose='signup_reply', volunteer=volunteer).status == SendStatus.BLOCKED_POLICY
    assert len(provider.sent) == 2


def placement(session, clock, make_volunteer, make_shift, *, tomorrow=False, qualified=True):
    volunteer = make_volunteer(quals=[('training', 'verified', None)] if qualified else [])
    shift = make_shift(required=['training'], starts=clock.now()+timedelta(days=1 if tomorrow else 3))
    row = m.Assignment(volunteer_id=volunteer.id, shift_id=shift.id, status='confirmed', source='fill',
                       created_at=clock.now(), updated_at=clock.now())
    session.add(row); session.flush()
    return volunteer, row


def test_schedule_notice_and_day_before_are_one_each_per_real_eligible_placement(gate, session, clock, provider, make_volunteer, make_shift):
    volunteer, assignment = placement(session, clock, make_volunteer, make_shift)
    meta = {'assignment_id': assignment.id, 'notice': 'scheduled'}
    assert gate.send(body='You are scheduled for your recorded shift.', purpose='confirmation', volunteer=volunteer, conversation=meta).sent
    assert gate.send(body='Different wording for same placement.', purpose='confirmation', volunteer=volunteer, conversation=meta).status == SendStatus.BLOCKED_POLICY
    clock.advance(timedelta(days=2))
    meta = {'assignment_id': assignment.id, 'notice': 'day_before'}
    assert gate.send(body='Your recorded shift is tomorrow.', purpose='reminder', volunteer=volunteer, conversation=meta).sent
    assert gate.send(body='Reminder again.', purpose='reminder', volunteer=volunteer, conversation=meta).status == SendStatus.BLOCKED_POLICY
    assert len(provider.sent) == 2


@pytest.mark.parametrize('change', ['unqualified', 'proposed', 'cancelled', 'wrong_recipient', 'not_tomorrow'])
def test_schedule_notices_never_create_or_invent_eligible_bookings(gate, session, clock, provider, make_volunteer, make_shift, change):
    volunteer, assignment = placement(session, clock, make_volunteer, make_shift, tomorrow=True, qualified=change != 'unqualified')
    if change in ('proposed', 'cancelled'):
        assignment.status = change
    if change == 'wrong_recipient':
        volunteer = make_volunteer()
    if change == 'not_tomorrow':
        assignment.shift.event.starts_at += timedelta(days=1)
    session.flush()
    result = gate.send(body='Your shift is tomorrow.', purpose='reminder', volunteer=volunteer,
                       conversation={'assignment_id': assignment.id, 'notice': 'day_before'})
    assert result.status == SendStatus.BLOCKED_POLICY and not provider.sent
    assert len(list(session.scalars(select(m.Assignment)))) == 1


def test_manual_text_requires_exact_review_even_without_global_confirmation_mode(gate, session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    assert not confirmations.enabled(session)
    result = gate.send(body='The precise human draft.', purpose='manual', volunteer=volunteer)
    assert result.status == SendStatus.HELD_FOR_APPROVAL and not provider.sent
    approval = session.get(m.Approval, result.approval_id)
    confirmations.decide(session, gate, approval, approve=True, actor='admin@example.test',
                         expected=approval.payload['content_hash'], now=clock.now())
    assert len(provider.sent) == 1 and provider.sent[0].body == 'The precise human draft.'
    assert gate.send(body='Changed human draft.', purpose='manual', volunteer=volunteer, _confirmation=approval).status == SendStatus.BLOCKED_POLICY


def test_manual_exception_preserves_style_and_consent_checks(gate, session, provider, make_volunteer):
    volunteer = make_volunteer(opt_in=False)
    assert gate.send(body='Human draft.', purpose='manual', volunteer=volunteer).status == SendStatus.BLOCKED_OPT_OUT
    assert gate.send(body='Human\u2014draft.', purpose='manual', volunteer=volunteer).status == SendStatus.BLOCKED_STYLE
    assert not provider.sent


def test_conversation_metadata_is_bound_to_exact_review(gate, session, clock, provider, make_volunteer, make_shift):
    volunteer, assignment = placement(session, clock, make_volunteer, make_shift)
    session.info[confirmations.MODE_KEY] = True
    result = gate.send(body='Your recorded shift.', purpose='confirmation', volunteer=volunteer,
        conversation={'assignment_id': assignment.id, 'notice': 'scheduled'})
    approval = session.get(m.Approval, result.approval_id)
    original_hash = approval.payload['content_hash']
    approval.payload = {**approval.payload, 'conversation': {**approval.payload['conversation'], 'assignment_id': 999}}
    assert confirmations.digest(approval.payload) != original_hash
    with pytest.raises(ValueError, match='changed or expired'):
        confirmations.decide(session, gate, approval, approve=True, actor='admin@example.test',
                             expected=original_hash, now=clock.now())
    assert not provider.sent


def test_schedule_notifications_do_not_bypass_quiet_hours(gate, session, clock, provider, make_volunteer, make_shift):
    volunteer, assignment = placement(session, clock, make_volunteer, make_shift)
    clock.set_time(clock.now().replace(hour=23))
    result = gate.send(body='Your recorded shift.', purpose='confirmation', volunteer=volunteer,
        conversation={'assignment_id': assignment.id, 'notice': 'scheduled'})
    assert result.status == SendStatus.HELD_QUIET_HOURS and not provider.sent


def test_explicit_schedule_question_gets_one_answer_no_proactive_status(gate, session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    assert gate.send(body='No recorded bookings.', purpose='booking_status', volunteer=volunteer).status == SendStatus.BLOCKED_POLICY
    inbound = m.Message(direction='in', phone=volunteer.phone, volunteer_id=volunteer.id, body='Am I scheduled?',
                        status='received', kind='sms', created_at=clock.now())
    session.add(inbound); session.flush(); gate.reply_to_message_id = inbound.id
    assert gate.send(body='No recorded bookings.', purpose='booking_status', volunteer=volunteer).sent
    assert gate.send(body='Status again.', purpose='booking_status', volunteer=volunteer).status == SendStatus.BLOCKED_POLICY
    assert len(provider.sent) == 1


def test_stop_still_changes_consent_and_admin_status_stays_admin_only(gate, session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    assert handle_stop_start(session, clock, provider, volunteer, 'STOP') == 'stop'
    assert not volunteer.sms_opt_in and provider.sent == []
    assert handle_stop_start(session, clock, provider, volunteer, 'STOP') == 'stop'
    assert provider.sent == []
    assert len(session.scalars(select(m.Notification).where(m.Notification.purpose == 'stop_confirm')).all()) == 1
    admin = make_volunteer(coordinator=True)
    assert gate.send(body='Internal coverage gap.', purpose='coordinator_notify', volunteer=admin).sent


def test_old_chatter_queue_is_blocked_before_claim(mac_app):
    with mac_app.state.session_factory() as session:
        selected = mac_app.state.provider.test_sessions[PHONE]
        row = m.Message(direction='out', phone=PHONE, body='Checking for a shift.', purpose='clarify', kind='ai',
                        status='queued', provider_sid=selected.outbound_prefix+'old', created_at=mac_app.state.clock.now())
        session.add(row); session.commit(); ident=row.id
    with TestClient(mac_app) as client:
        assert post(client, '/mac/outbound/pull').json()['messages'] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message, ident).status == 'blocked_policy'
        assert session.get(MacDeliveryClaim, ident) is None


def test_changed_assignment_is_blocked_after_claim_before_native_send(mac_app):
    clock=mac_app.state.clock
    with mac_app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer))
        role=m.Role(name='Greeter', ministry='Welcome', required_qualifications=[], criticality='standard', fill_policy='auto')
        event=m.Event(title='Saved shift', starts_at=clock.now()+timedelta(days=3), ends_at=clock.now()+timedelta(days=3, hours=1), status='scheduled')
        session.add_all([role,event]); session.flush()
        shift=m.Shift(role_id=role.id,event_id=event.id,slot_index=0); session.add(shift);session.flush()
        assignment=m.Assignment(volunteer_id=volunteer.id,shift_id=shift.id,status='confirmed',source='planner',created_at=clock.now(),updated_at=clock.now())
        session.add(assignment);session.flush()
        outcome=SendGate(session,clock,mac_app.state.provider).send(body='Your scheduled shift.', purpose='confirmation', volunteer=volunteer,
            conversation={'assignment_id':assignment.id,'notice':'scheduled'})
        assert outcome.sent; session.commit(); aid=assignment.id
    with TestClient(mac_app) as client:
        item=post(client,'/mac/outbound/pull').json()['messages'][0]
        assert item['conversation_preflight_required']
        with mac_app.state.session_factory() as session:
            session.get(m.Assignment,aid).status='cancelled';session.commit()
        response=post(client,f"/mac/outbound/{item['id']}/verify",{'token':item['token']})
        assert response.status_code==409
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message,item['id']).status=='blocked_policy'


def test_native_worker_rechecks_conversation_before_any_device_attempt(tmp_path):
    sent=[]
    item={'id':44,'token':'c'*64,'phone':PHONE,'session_id':session_id(PHONE),
          'body':'Old automatic chatter.','conversation_preflight_required':True}
    def server(request):
        if request.url.path.endswith('/pull'):
            return httpx.Response(200,json={'messages':[item]})
        if request.url.path.endswith('/verify'):
            return httpx.Response(409,json={'detail':'Suppressed by conversation policy'})
        pytest.fail('Blocked item must not get a native acknowledgment')
    worker=MacWorker(config(tmp_path),live=True,client=httpx.Client(transport=httpx.MockTransport(server)),reader=ReaderFixture(),
                     sender=lambda *args: sent.append(args) or 'submitted')
    worker.once()
    assert not sent and worker.state['dispatches']['44']['outcome']=='blocked'

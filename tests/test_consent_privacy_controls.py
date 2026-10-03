"""Synthetic withdrawal and recognized-care privacy checks; no Gloo or texts."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.consent_controls import control_action
from app.core.inbound import handle_inbound
from app.core.send_gate import handle_stop_start
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import parse_inbound
from datetime import timedelta


class NoGloo:
    def create_response(self, **kwargs):
        pytest.fail('Withdrawal or recognized sensitive text must not reach Gloo')


def record_consent(session, clock, volunteer):
    from app.core.signup_copy import WELCOME
    invitation = m.Message(direction='out', volunteer_id=volunteer.id, phone=volunteer.phone,
        body=WELCOME, purpose='signup_reply', status='submitted', kind='ai', created_at=clock.now()-timedelta(minutes=2))
    session.add(invitation); session.flush()
    session.add(m.Message(direction='in', volunteer_id=volunteer.id, phone=volunteer.phone,
        body=volunteer.name, status='received', kind='inbound', created_at=clock.now()))
    volunteer.preferences = {**volunteer.preferences,
        'consent_at': clock.now().isoformat(), 'consent_source': 'sms_name_reply_to_exact_invitation'}
    session.flush()


@pytest.mark.parametrize('body', ['please stop texting me', "don't text me anymore",
    'I do not want any more texts', 'Please remove me from your texting list.',
    'I withdraw my consent to texting', 'STOP!', 'unsubscribe', 'opt out'])
def test_direct_withdrawal_precedes_pending_signup_and_any_model(
    session, clock, provider, make_volunteer, body
):
    volunteer = make_volunteer(prefs={'signup_source': 'sms', 'consent_pending': True,
        'onboarding_stage': 'availability'})
    queued = m.Message(direction='out', volunteer_id=volunteer.id, phone=volunteer.phone,
        body='Synthetic old queue.', purpose='signup_reply', status='queued', kind='ai', created_at=clock.now())
    session.add(queued); session.flush()
    ctx = FillContext(session, clock, provider, NoGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone, body,
        lambda _: pytest.fail('Withdrawal must precede classification'), ctx=ctx)
    assert result.routed_to == 'stop' and not volunteer.sms_opt_in
    assert volunteer.preferences['consent_pending'] is False
    assert session.get(m.Policy, 'sms_opt_out:'+volunteer.phone).value['value'] is True
    session.refresh(queued); assert queued.status == 'blocked_opt_out'
    count = len(provider.sent)
    handle_inbound(session, clock, provider, volunteer.phone, body,
        lambda _: pytest.fail('Repeated withdrawal must be silent'), ctx=ctx)
    assert len(provider.sent) == count


@pytest.mark.parametrize('body', ['If I say stop texting me, what happens?',
    'The example says "please stop texting me"', 'Do not stop texting me',
    'Maybe stop texting me later', "I can't serve Sunday", 'Cancel my Sunday shift',
    'Can you explain unsubscribe?', '"STOP"'])
def test_ambiguous_or_quoted_words_never_change_consent(body):
    assert control_action(body) is None


def test_unknown_direct_withdrawal_prevents_signup_model(session, clock, provider):
    phone = '+15550000001'
    queued = m.Message(direction='out', phone=phone, body='Synthetic prior invitation.',
        purpose='signup_reply', status='queued', kind='ai', created_at=clock.now())
    session.add(queued); session.flush()
    result = handle_inbound(session, clock, provider, phone, 'please stop texting me',
        lambda _: pytest.fail('Must not classify withdrawal'),
        ctx=FillContext(session, clock, provider, NoGloo()), allow_signup=True)
    assert result.routed_to == 'stop'
    assert session.get(m.Policy, 'sms_opt_out:'+phone).value['value'] is True
    session.refresh(queued); assert queued.status == 'blocked_opt_out'
    assert session.scalar(select(m.Volunteer)) is None and not provider.sent


def test_explicit_restart_clears_prior_stop_suppression(session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    record_consent(session, clock, volunteer)
    handle_stop_start(session, clock, provider, volunteer, 'please stop texting me')
    assert handle_stop_start(session, clock, provider, volunteer, 'START') == 'start'
    assert volunteer.sms_opt_in and session.get(m.Policy, 'sms_opt_out:'+volunteer.phone) is None


@pytest.mark.parametrize('fabricated_flags', [False, True])
def test_start_cannot_opt_in_import_without_actual_disclosure_and_reply(session, clock, provider, make_volunteer, fabricated_flags):
    volunteer = make_volunteer(opt_in=False, status='inactive', prefs={
        'consent_at': clock.now().isoformat(), 'consent_source': 'sms_name_reply_to_exact_invitation'} if fabricated_flags else {})
    assert handle_stop_start(session, clock, provider, volunteer, 'START') == 'consent_required'
    assert not volunteer.sms_opt_in and not provider.sent
    assert session.get(m.Policy, f'consent_restart_review:{volunteer.id}').value['state'] == 'held'


def test_stop_commits_without_gloo_and_ack_never_falls_back(session, clock, provider, make_volunteer):
    from app.core.notifications import flush_due
    volunteer = make_volunteer()
    handle_stop_start(session, clock, provider, volunteer, 'please stop texting me')
    session.commit()
    assert not volunteer.sms_opt_in and not provider.sent
    ack = session.scalar(select(m.Notification).where(m.Notification.purpose == 'stop_confirm'))
    ctx = FillContext(session, clock, provider, None)
    flush_due(ctx)
    assert ack.state == 'pending' and ack.detail['gloo_attempts'] == 1 and not provider.sent
    class ControlGloo:
        def create_response(self, **kwargs):
            facts = json.loads(kwargs['input'])
            return SimpleNamespace(output_text=facts['approved_message'], usage=None)
    clock.advance(timedelta(minutes=2)); ctx.gloo = ControlGloo()
    flush_due(ctx)
    assert ack.state == 'sent' and len(provider.sent) == 1
    assert provider.sent[0].body == ack.body


def test_restart_expires_pending_stop_ack_and_repeated_start_stays_quiet(session, clock, provider, make_volunteer):
    volunteer = make_volunteer(); record_consent(session, clock, volunteer)
    handle_stop_start(session, clock, provider, volunteer, 'STOP')
    stop = session.scalar(select(m.Notification).where(m.Notification.purpose == 'stop_confirm'))
    handle_stop_start(session, clock, provider, volunteer, 'START')
    assert stop.state == 'expired'
    handle_stop_start(session, clock, provider, volunteer, 'START')
    assert len(session.scalars(select(m.Notification).where(m.Notification.purpose == 'start_confirm')).all()) == 1


@pytest.mark.parametrize('body', ['I have cancer', 'I want to hurt myself',
    'I cannot serve Sunday because I am in the hospital'])
def test_sensitive_text_never_leaves_for_parser_model(body):
    parsed = parse_inbound(NoGloo(), body)
    assert parsed.sensitive
    if 'cannot serve' in body:
        assert parsed.intent == 'cancel' and not parsed.parse_error
    if 'hurt myself' in body:
        assert parsed.severity == 'urgent'


def test_sensitive_message_still_creates_internal_care_hold(session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    result = handle_inbound(session, clock, provider, volunteer.phone, 'I have cancer',
        lambda body: parse_inbound(NoGloo(), body))
    escalation = session.get(m.Escalation, result.escalation_id)
    assert escalation.category == 'sensitive' and escalation.related_ids['volunteer_id'] == volunteer.id
    assert not provider.sent


def test_outgoing_model_history_omits_known_care_details(session, clock, make_volunteer):
    volunteer = make_volunteer()
    for body in ('I have cancer', 'Greeter works for me'):
        session.add(m.Message(direction='in', volunteer_id=volunteer.id, phone=volunteer.phone,
            body=body, status='received', kind='inbound', created_at=clock.now()))
    session.flush()
    calls = []
    class CaptureGloo:
        settings = Settings(gloo_signup_replies=True)
        def create_response(self, **kwargs):
            facts = json.loads(kwargs['input']); calls.append(facts)
            return SimpleNamespace(output_text=facts['approved_message'], usage=None)
    assert compose_signup_reply(session, clock, CaptureGloo(), 'Recorded schedule.',
        volunteer=volunteer, require_gloo=True) == 'Recorded schedule.'
    history = calls[0]['recent_messages']
    assert history == [{'direction': 'in', 'body': 'Greeter works for me'}]
    with pytest.raises(GlooUnavailableError, match='sensitive'):
        compose_signup_reply(session, clock, NoGloo(), 'Private cancer diagnosis.',
            volunteer=volunteer, require_gloo=True)


@pytest.mark.parametrize('with_context', [False, True])
def test_unknown_stop_is_durable_when_signup_is_disabled(session, clock, provider, with_context):
    phone = '+15550000001'
    queued = m.Message(direction='out', phone=phone, body='Previous invitation.',
        purpose='signup_reply', status='queued', kind='ai', created_at=clock.now())
    session.add(queued); session.flush()
    result = handle_inbound(session, clock, provider, phone, 'please stop texting me',
        lambda _: pytest.fail('Withdrawal must be local'),
        ctx=FillContext(session, clock, provider, NoGloo()) if with_context else None,
        allow_signup=False)
    session.commit(); session.refresh(queued)
    assert result.routed_to == 'stop'
    assert session.get(m.Policy, 'sms_opt_out:'+phone).value['value'] is True
    assert queued.status == 'blocked_opt_out' and not provider.sent


def test_signup_interpreter_cannot_export_prior_sensitive_history(session, clock, provider):
    from app.core.signup import request_signup
    phone = '+15550000001'
    session.add(m.Message(direction='in', phone=phone, body='I have cancer',
        status='received', kind='inbound', created_at=clock.now()-timedelta(minutes=1)))
    session.flush()
    class CaptureGloo:
        def __init__(self): self.inputs = []
        def create_response(self, **kwargs):
            self.inputs.append(kwargs['input'])
            return SimpleNamespace(output_text='{}', usage=None)
    gloo = CaptureGloo()
    request_signup(session, clock, gloo, phone, 'I would like to volunteer')
    assert len(gloo.inputs) == 1 and 'cancer' not in gloo.inputs[0]
    assert 'I would like to volunteer' in gloo.inputs[0]


def test_stop_purpose_cannot_forge_control_authority(session, clock, provider, make_volunteer):
    from app.core.send_gate import SendGate
    volunteer = make_volunteer()
    handle_stop_start(session, clock, provider, volunteer, 'STOP'); session.commit()
    result = SendGate(session, clock, provider).send(
        body='A new routine offer disguised as a control acknowledgment.',
        purpose='stop_confirm', volunteer=volunteer, kind='ai')
    assert not result.sent and not provider.sent


@pytest.mark.parametrize('body', ['I have diabetes', 'My genetic data is private',
    'My fingerprints are sensitive', 'My sexual orientation is private',
    'I am Asian', 'I am bisexual', 'I support Republican party', 'I am a Democrat'])
def test_explicit_sensitive_categories_are_held_before_gloo(body):
    assert parse_inbound(NoGloo(), body).sensitive


def test_pastor_does_not_get_automatic_raw_sensitive_sms(session, clock, provider, make_volunteer):
    from app.core.care import escalate_sensitive
    from app.core.send_gate import SendGate
    sender = make_volunteer()
    pastor = make_volunteer(name='Synthetic Pastor', pastor=True)
    pastor.is_pastor = True; session.flush()
    escalation_id = escalate_sensitive(session, SendGate(session, clock, provider), sender,
        'I have cancer', clock.now())
    assert session.get(m.Escalation, escalation_id).status == 'open'
    assert not provider.sent


def test_sensitive_coordinator_command_never_reaches_admin_agent(session, clock, provider, make_volunteer):
    coordinator = make_volunteer(); coordinator.is_coordinator = True; session.flush()
    result = handle_inbound(session, clock, provider, coordinator.phone, 'I have diabetes',
        lambda _: pytest.fail('Coordinator privacy guard must be local'),
        ctx=FillContext(session, clock, provider, NoGloo()))
    assert result.routed_to == 'escalated_sensitive' and result.escalation_id
    assert not provider.sent


@pytest.mark.parametrize('input', ['I have diabetes', [{'role':'user','content':'My genetic data is private'}]])
def test_gloo_client_privacy_guard_never_calls_http(input):
    from app.llm.gloo_client import GlooClient
    class NeverHTTP:
        def create(self, **kwargs): pytest.fail('Sensitive input must not reach HTTP')
    gloo = GlooClient(Settings(), client=SimpleNamespace(responses=NeverHTTP()))
    with pytest.raises(GlooUnavailableError, match='internal human review'):
        gloo.create_response(model='synthetic', input=input)


def test_gloo_client_guard_excludes_static_prompt_and_schema():
    from app.llm.gloo_client import GlooClient
    class CaptureHTTP:
        def create(self, **kwargs): return SimpleNamespace(usage=None)
    gloo = GlooClient(Settings(), client=SimpleNamespace(responses=CaptureHTTP()))
    gloo.create_response(model='synthetic', input='Coffee signup',
        instructions='Never disclose cancer or genetic data', tools=[{'description':'Handle sensitive cancer'}])
    assert len(gloo.usage_log) == 1


@pytest.mark.parametrize('closed', [False, True])
@pytest.mark.parametrize('has_source', [False, True])
def test_durable_sensitive_history_is_never_reexported(session, clock, make_volunteer, closed, has_source):
    volunteer = make_volunteer()
    private = m.Message(direction='in', volunteer_id=volunteer.id, phone=volunteer.phone,
        body='Private matter with no keyword', status='received', kind='inbound',
        created_at=clock.now()-timedelta(minutes=2))
    session.add(private); session.flush()
    ids = {'volunteer_id':volunteer.id}
    if has_source: ids['message_id'] = private.id
    session.add(m.Escalation(category='sensitive',severity='normal',summary='Internal care',related_ids=ids,
        status='closed' if closed else 'open',created_at=clock.now()-timedelta(minutes=1)))
    session.add(m.Message(direction='in', volunteer_id=volunteer.id, phone=volunteer.phone,
        body='Coffee works',status='received',kind='inbound',created_at=clock.now()))
    session.flush()
    class CaptureGloo:
        def create_response(self, **kwargs):
            facts = json.loads(kwargs['input'])
            assert facts['recent_messages'] == [{'direction':'in','body':'Coffee works'}]
            return SimpleNamespace(output_text=facts['approved_message'], usage=None)
    compose_signup_reply(session, clock, CaptureGloo(), 'Recorded schedule.', volunteer=volunteer, require_gloo=True)


def test_notification_with_open_care_hold_never_composes(session, clock, provider, make_volunteer):
    from app.core.notifications import flush_due
    volunteer = make_volunteer(coordinator=True)
    session.add(m.Escalation(category='sensitive',severity='normal',summary='Internal care',
        related_ids={'volunteer_id':volunteer.id},status='open',created_at=clock.now()))
    row = m.Notification(key='synthetic-admin',volunteer_id=volunteer.id,purpose='coordinator_notify',
        body='Recorded staffing status.',state='pending',created_at=clock.now(),due_at=clock.now())
    session.add(row); session.flush()
    flush_due(FillContext(session, clock, provider, NoGloo()))
    assert row.state == 'blocked' and not provider.sent


def test_control_ack_retains_exact_review_and_source_binding(session, clock, provider, make_volunteer):
    from app.core import confirmations
    from app.core.notifications import flush_due
    volunteer = make_volunteer()
    session.info[confirmations.MODE_KEY] = True
    handle_stop_start(session, clock, provider, volunteer, 'STOP')
    class ControlGloo:
        def create_response(self, **kwargs):
            return SimpleNamespace(output_text=json.loads(kwargs['input'])['approved_message'], usage=None)
    ctx = FillContext(session, clock, provider, ControlGloo())
    flush_due(ctx)
    ack = session.scalar(select(m.Notification).where(m.Notification.purpose == 'stop_confirm'))
    assert ack.state == 'awaiting_approval' and not provider.sent
    approval = session.get(m.Approval, ack.detail['approval_id'])
    confirmations.decide(session, ctx.gate, approval, approve=True, actor='admin@example.test',
        expected=approval.payload['content_hash'], now=clock.now())
    assert len(provider.sent) == 1 and ack.message_id == approval.payload['message_id']
    assert ack.state == 'sent'


def test_sensitive_cancellation_keeps_authoritative_schedule_update(session, clock, provider, make_volunteer, make_shift, assign):
    volunteer = make_volunteer()
    shift = make_shift('usher')
    assignment = assign(volunteer, shift, status='confirmed')
    ctx = FillContext(session, clock, provider, NoGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone,
        'I cannot serve Sunday because I am in the hospital',
        lambda body: parse_inbound(NoGloo(), body), ctx=ctx)
    session.refresh(assignment)
    assert assignment.status == 'cancelled' and result.escalation_id
    assert not provider.sent

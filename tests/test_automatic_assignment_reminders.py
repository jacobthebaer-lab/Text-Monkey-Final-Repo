"""Authorized Mac reminders use fictional saved assignments and no native sends."""
from dataclasses import replace
from datetime import timedelta
from sqlalchemy import select
import pytest

from app.core import confirmations
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim
from tests.test_literal_reminder_mac_acceptance import reminder_mac, book, tick, reschedule
from tests.test_demo_acceptance_review import acceptance_app, pull, BRIDGE_TOKEN
from tests.test_exact_day_before_reminder import LITERAL
from tests.test_mac_roster_enrollment import roster, ADDED
from tests.test_enrolled_signup_composition import composer


def enable(app):
    app.state.settings = replace(app.state.settings, competition_confirmation_required=False)
    app.state.gloo.settings = app.state.settings
    app.state.session_factory.configure(info={confirmations.MODE_KEY: False})
    with app.state.session_factory() as session:
        session.add(m.Policy(key='automatic_assignment_reminders', value={'value': True}))
        session.commit()


def automatic(reminder_mac):
    client, app, gloo, clock = reminder_mac
    enable(app)
    assignment_id = book(app)
    return client, app, gloo, clock, assignment_id


def message(app):
    with app.state.session_factory() as session:
        return session.scalar(select(m.Message))


def test_automatic_confirmed_assignment_queues_exact_copy_once_and_native_preflight_passes(reminder_mac):
    client, app, gloo, _, assignment_id = automatic(reminder_mac)
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        row = session.get(m.Assignment, assignment_id)
        row.status, row.source = 'confirmed', 'fill'
        session.commit()
    result, reviews = tick(app)
    assert result['messages']['reminders'] == 1 and reviews == []
    row = message(app)
    assert row.body == LITERAL and row.status == 'queued'
    assert tick(app)[1] == [] and len(gloo.calls) == 1
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Approval)) is None
        assert session.info[confirmations.MODE_KEY] is False
        job = session.get(m.Policy, f'job:reminder:{assignment_id}').value
        assert job['message_id'] == row.id and job['automatic_reminder'] is True
    claim = pull(client).json()['messages'][0]
    assert 'confirmation_required' not in claim
    assert claim['conversation_preflight_required']
    response = client.post(f"/mac/outbound/{claim['id']}/verify", json={'token': claim['token']},
                          headers={'Authorization': 'Bearer ' + BRIDGE_TOKEN})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('phase', ['claim', 'native_verify'])
@pytest.mark.parametrize('change', ['cancel', 'date', 'name', 'role', 'timezone', 'consent',
                                   'stop', 'qualification', 'policy', 'session', 'body', 'proof'])
def test_changed_source_or_authority_never_reaches_native(reminder_mac, phase, change):
    client, app, _, _, assignment_id = automatic(reminder_mac)
    tick(app)
    row = message(app)
    claim = pull(client).json()['messages'][0] if phase == 'native_verify' else None
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        assignment = session.get(m.Assignment, assignment_id)
        person = assignment.volunteer
        if change == 'cancel': assignment.status = 'cancelled'
        elif change == 'date':
            assignment.shift.event.starts_at += timedelta(hours=1)
            assignment.shift.event.ends_at += timedelta(hours=1)
        elif change == 'name': person.name = 'Different Person'
        elif change == 'role': assignment.shift.role.name = 'Production'
        elif change == 'timezone': session.add(m.Policy(key='church_timezone', value={'value': 'UTC'}))
        elif change == 'consent': person.sms_opt_in = False
        elif change == 'stop': session.add(m.Policy(key='sms_opt_out:' + person.phone, value={'value': True}))
        elif change == 'qualification': assignment.shift.role.required_qualifications = ['background_check']
        elif change == 'policy': session.get(m.Policy, 'automatic_assignment_reminders').value = {'value': False}
        elif change == 'session':
            app.state.provider.test_sessions[person.phone] = replace(app.state.provider.test_sessions[person.phone], id='f'*32)
        elif change == 'body': session.get(m.Message, row.id).body += ' Altered.'
        else:
            proof = session.get(m.Notification, f'conversation-message:{row.id}')
            proof.detail = {k: v for k, v in proof.detail.items() if k != 'automatic_reminder'}
        session.commit()
    if claim:
        response = client.post(f"/mac/outbound/{claim['id']}/verify", json={'token': claim['token']},
                              headers={'Authorization': 'Bearer ' + BRIDGE_TOKEN})
        assert response.status_code == 409, response.text
    else:
        assert pull(client).json()['messages'] == []
        with app.state.session_factory() as session:
            assert session.get(MacDeliveryClaim, row.id) is None


@pytest.mark.parametrize('defect', ['no_gloo', 'outage', 'paraphrase', 'qualification', 'wrong_scope'])
def test_invalid_inputs_do_not_queue_or_make_automatic_approval(reminder_mac, defect):
    _, app, gloo, _, assignment_id = automatic(reminder_mac)
    if defect == 'no_gloo': app.state.gloo = None
    elif defect == 'outage': gloo.unavailable = True
    elif defect == 'paraphrase': gloo.transform = lambda body: body + ' Extra.'
    elif defect == 'wrong_scope':
        with app.state.session_factory() as session:
            phone = session.get(m.Volunteer, app.state.reminder_person_id).phone
        app.state.provider.test_sessions[phone] = replace(app.state.provider.test_sessions[phone], id='f'*32)
    else:
        with app.state.session_factory() as session:
            session.info['record_authorized'] = True
            session.get(m.Assignment, assignment_id).shift.role.required_qualifications = ['background_check']
            session.commit()
    assert tick(app)[1] == []
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None
        assert session.scalar(select(m.Approval)) is None
    if defect in {'qualification', 'wrong_scope'}: assert not gloo.calls


@pytest.mark.parametrize('status', ['pending', 'approved', 'rejected', 'expired'])
def test_enabling_automation_preserves_existing_exact_reviews(reminder_mac, status):
    _, app, gloo, _ = reminder_mac
    assignment_id = book(app)
    _, reviews = tick(app)
    with app.state.session_factory() as session:
        review = session.get(m.Approval, reviews[0][0])
        review.status = status
        original = dict(review.payload)
        session.commit()
    enable(app)
    reschedule(app)
    tick(app)
    with app.state.session_factory() as session:
        review = session.get(m.Approval, reviews[0][0])
        assert review.status == status and review.payload == original
        assert len(session.scalars(select(m.Approval)).all()) == 1
        assert session.scalar(select(m.Message)) is None
    assert len(gloo.calls) == 1


def test_uncertain_job_is_not_retried(reminder_mac):
    _, app, gloo, _, assignment_id = automatic(reminder_mac)
    tick(app)
    with app.state.session_factory() as session:
        row = session.scalar(select(m.Message))
        row.status = 'uncertain'
        job = session.get(m.Policy, f'job:reminder:{assignment_id}')
        job.value = {**job.value, 'state': 'uncertain'}
        session.commit()
    tick(app)
    assert len(gloo.calls) == 1 and message(app).status == 'uncertain'


def test_other_scheduled_workflows_still_require_review(reminder_mac):
    _, app, _, _, assignment_id = automatic(reminder_mac)
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        event = session.get(m.Assignment, assignment_id).shift.event
        event.starts_at += timedelta(days=1)
        event.ends_at += timedelta(days=1)
        session.commit()
    tick(app)
    with app.state.session_factory() as session:
        review = session.scalar(select(m.Approval))
        assert review.status == 'pending' and review.payload['purpose'] == 'confirmation'
        assert session.scalar(select(m.Message)) is None


@pytest.mark.parametrize('change', ['date', 'scope'])
def test_source_changed_during_gloo_composition_is_held(reminder_mac, change):
    from app.agents.fill_agent import FillContext
    from app.jobs import process_jobs
    _, app, gloo, clock, assignment_id = automatic(reminder_mac)
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        def changed(body):
            assignment = session.get(m.Assignment, assignment_id)
            if change == 'date':
                assignment.shift.event.starts_at += timedelta(hours=1)
                assignment.shift.event.ends_at += timedelta(hours=1)
                session.flush()
            else:
                phone = assignment.volunteer.phone
                app.state.provider.test_sessions[phone] = replace(app.state.provider.test_sessions[phone], id='f'*32)
            return body
        gloo.transform = changed
        result = process_jobs(FillContext(session, clock, app.state.provider, gloo))
        assert result['messages']['reminders'] == 0
        assert session.scalar(select(m.Message)) is None and session.scalar(select(m.Approval)) is None
        assert session.info[confirmations.MODE_KEY] is False
    assert len(gloo.calls) == 1


def test_competition_mode_never_uses_automatic_policy(reminder_mac):
    _, app, _, _ = reminder_mac
    book(app)
    with app.state.session_factory() as session:
        session.add(m.Policy(key='automatic_assignment_reminders', value={'value': True}))
        session.commit()
    # The real configuration stays in exact-review mode, not only local scheduler state.
    app.state.gloo.settings = app.state.settings
    assert len(tick(app)[1]) == 1 and message(app) is None


def test_fresh_signed_enrollment_reminder_uses_accepted_scope_not_boot_phones(roster):
    from types import SimpleNamespace
    from app.agents.fill_agent import FillContext
    from app.jobs import process_jobs
    f = roster
    worker = f.worker()
    worker.once()
    gloo, calls = composer(f)
    now = f.app.state.clock.now()
    f.app.state.clock = f.app.state.mac_delivery_clock = SimpleNamespace(now=lambda: now)
    f.app.state.gloo = gloo
    assert ADDED not in gloo.settings.mac_demo_phones
    with f.app.state.session_factory() as session:
        role = m.Role(name='Greeter', ministry='Welcome', required_qualifications=[],
                      criticality='standard', fill_policy='auto')
        event = m.Event(title='Fictional service', status='scheduled',
                        starts_at=now+timedelta(days=1), ends_at=now+timedelta(days=1, hours=1))
        shift = m.Shift(event=event, role=role, slot_index=0)
        session.add_all([shift,
            m.Policy(key='automatic_assignment_reminders', value={'value': True}),
            m.Policy(key='quiet_hours', value={'value': {'start': '04:00', 'end': '04:00'}})])
        session.flush()
        assignment = m.Assignment(volunteer_id=f.person_id, shift_id=shift.id,
            status='confirmed', source='fill', created_at=now, updated_at=now)
        session.add(assignment)
        session.commit()
    with f.app.state.session_factory() as session:
        result = process_jobs(FillContext(session, f.app.state.clock, f.app.state.provider, gloo))
        assert result['messages']['reminders'] == 1
        row = session.scalar(select(m.Message).where(m.Message.phone == ADDED))
        assert row.status == 'queued' and row.provider_sid.startswith(worker.test_sessions[ADDED].outbound_prefix)
        assert session.scalar(select(m.Approval)) is None
        session.commit()
    assert len(calls) == 1
    from tests.test_mac_ongoing import TOKEN
    response = f.backend.post('/mac/outbound/pull', json={},
                              headers={'Authorization': 'Bearer ' + TOKEN})
    assert response.status_code == 200, response.text
    assert len(response.json()['messages']) == 1

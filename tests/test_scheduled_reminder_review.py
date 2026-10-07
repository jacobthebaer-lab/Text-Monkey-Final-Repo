"""Scheduled Mac reminders require review without changing other texting modes."""
from dataclasses import replace
from sqlalchemy import select
import pytest

from app import jobs
from app.agents.fill_agent import FillContext
from app.core import confirmations, notifications, reminders
from app.db import models as m
from tests.test_literal_reminder_mac_acceptance import reminder_mac, book, tick
from tests.test_demo_acceptance_review import acceptance_app
from tests.test_exact_day_before_reminder import LITERAL


def automatic(app):
    app.state.settings = replace(app.state.settings, competition_confirmation_required=False,
                                gloo_signup_replies=True)
    app.state.gloo.settings = app.state.settings
    app.state.session_factory.configure(info={confirmations.MODE_KEY: False})


def test_automatic_mac_tick_stages_one_exact_reminder_until_explicit_approval(reminder_mac, monkeypatch):
    client, app, gloo, _ = reminder_mac
    automatic(app)
    assignment_id = book(app)
    fill_modes = []
    process_fills = jobs.process_due_fill_requests
    def fills(ctx):
        fill_modes.append(confirmations.enabled(ctx.session))
        return process_fills(ctx)
    monkeypatch.setattr(jobs, 'process_due_fill_requests', fills)
    result, reviews = tick(app)
    assert result['messages']['reminders'] == 0 and len(reviews) == 1
    assert reviews[0][2] == LITERAL
    assert tick(app)[1] == reviews and len(gloo.calls) == 1
    assert fill_modes == [False, False]
    with app.state.session_factory() as session:
        assert session.info[confirmations.MODE_KEY] is False
        assert session.get(m.Assignment, assignment_id).status == 'approved'
        assert session.scalar(select(m.Message)) is None
        review = session.get(m.Approval, reviews[0][0])
        assert review.status == 'pending'
        assert review.payload['workflow_source_hash']
        assert review.payload['content_hash'] == confirmations.digest(review.payload)
    wrong_hash = client.post(f'/api/proposals/{reviews[0][0]}/approve',
                             json={'content_hash': 'wrong'})
    assert wrong_hash.status_code == 409
    response = client.post(f'/api/proposals/{reviews[0][0]}/approve',
                          json={'content_hash': reviews[0][1]})
    assert response.status_code == 200, response.text
    with app.state.session_factory() as session:
        message = session.scalar(select(m.Message))
        assert message.status == 'queued' and message.body == LITERAL
        assert session.info[confirmations.MODE_KEY] is False
    assert not app.state.settings.competition_confirmation_required


@pytest.mark.parametrize('previous', [None, False, True])
def test_exact_reminder_mode_is_restored_when_processing_raises(session, clock, monkeypatch, previous):
    if previous is None:
        session.info.pop(confirmations.MODE_KEY, None)
    else:
        session.info[confirmations.MODE_KEY] = previous
    before = dict(session.info)
    def fills(ctx):
        assert ctx.session.info == before
        return []
    def fail(ctx):
        assert confirmations.enabled(ctx.session)
        raise RuntimeError('Synthetic reminder failure')
    monkeypatch.setattr(jobs, 'process_due_fill_requests', fills)
    monkeypatch.setattr(reminders, 'process', fail)
    with pytest.raises(RuntimeError, match='Synthetic reminder failure'):
        jobs.process_jobs(FillContext(session, clock, object(), None))
    assert session.info == before


def test_tick_does_not_make_an_ordinary_welcome_require_review(reminder_mac):
    client, app, _, _ = reminder_mac
    automatic(app)
    tick(app)
    with app.state.session_factory() as session:
        session.add(m.Policy(key='full_text_onboarding', value={'value': True}))
        session.commit()
    response = client.post(f'/api/volunteers/{app.state.reminder_person_id}/text-setup')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['delivery'] == 'queued_for_mac' and result['approval_id'] is None
    with app.state.session_factory() as session:
        assert session.get(m.Message, result['message_id']).status == 'queued'
        assert session.scalar(select(m.Approval)) is None
        assert session.info[confirmations.MODE_KEY] is False


def test_notification_tick_preserves_pending_internal_workflow_records(reminder_mac):
    _, app, gloo, clock = reminder_mac
    automatic(app)
    with app.state.session_factory() as session:
        rows = [m.Notification(key='synthetic-internal:' + purpose,
                    volunteer_id=app.state.reminder_person_id, purpose=purpose, body='',
                    state='pending', due_at=clock.now(), created_at=clock.now(),
                    detail={'synthetic_internal_marker': purpose})
                for purpose in ('onboarding_turn', 'mac_followup_recovery', 'cancellation_scope')]
        session.add_all(rows)
        session.flush()
        ctx = FillContext(session, clock, app.state.provider, gloo)
        assert notifications.flush_due(ctx) == 0
        for row in rows:
            notifications._dispatch(ctx, row)
            assert row.state == 'pending'
            assert row.detail == {'synthetic_internal_marker': row.purpose}
        assert gloo.calls == [] and session.scalar(select(m.Message)) is None

"""Every notification requires Gloo; fake delivery cannot receive raw fallback text."""
from datetime import timedelta
import json
from types import SimpleNamespace

from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.notifications import deliver, flush_due, queue_staffing
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError


class FixtureGloo:
    settings = Settings(gloo_signup_replies=False)

    def __init__(self, *, unavailable=False):
        self.unavailable = unavailable
        self.calls = []

    def create_response(self, **kwargs):
        self.calls.append(json.loads(kwargs['input']))
        if self.unavailable:
            raise GlooUnavailableError('Synthetic outage')
        return SimpleNamespace(output_text='Gloo composed: ' + self.calls[-1]['approved_message'])


def test_staffing_digest_requires_gloo_and_recovers_with_fresh_facts(
    session, clock, provider, make_volunteer, make_shift, assign
):
    admin = make_volunteer(coordinator=True)
    shift = make_shift(starts=clock.now()+timedelta(days=1))
    gloo = FixtureGloo(unavailable=True)
    ctx = FillContext(session, clock, provider, gloo)
    queue_staffing(ctx, shift.event)
    row = session.scalar(select(m.Notification))
    flush_due(ctx)
    assert not gloo.calls and not provider.sent  # Preserve the five-minute debounce.
    clock.advance(timedelta(minutes=5))
    flush_due(ctx)
    assert len(gloo.calls) == 1
    assert 'Still needs cover:' in gloo.calls[0]['approved_message']
    assert '0/1 required spots covered' in gloo.calls[0]['approved_message']
    assert not provider.sent
    assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None
    assert row.state == 'pending' and row.message_id is None
    assert row.due_at == clock.now()+timedelta(minutes=2)
    assert row.detail['gloo_attempts'] == 1
    assign(make_volunteer(), shift, status='confirmed')
    gloo.unavailable = False
    clock.advance(timedelta(minutes=2))
    flush_due(ctx)
    assert len(gloo.calls) == 2
    facts = gloo.calls[-1]
    assert 'Fully staffed:' in facts['approved_message']
    assert 'All 1 required spots are covered' in facts['approved_message']
    assert facts['required_phrases'] == [facts['approved_message']]
    assert provider.sent_to(admin.phone)[0].body == 'Gloo composed: ' + facts['approved_message']
    assert row.state == 'sent' and row.message_id is not None
    flush_due(ctx)
    assert len(gloo.calls) == 2 and len(provider.sent) == 1


def test_other_notification_never_bypasses_gloo_with_signup_setting_disabled(
    session, clock, provider, make_volunteer
):
    admin = make_volunteer(coordinator=True)
    gloo = FixtureGloo(unavailable=True)
    ctx = FillContext(session, clock, provider, gloo)
    row = deliver(ctx, key='ordinary-notice', body='Synthetic approved facts.',
                  purpose='coordinator_notify', volunteer=admin)
    assert len(gloo.calls) == 1
    assert not provider.sent and row.state == 'pending'
    assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None
    gloo.unavailable = False
    clock.advance(timedelta(minutes=2))
    flush_due(ctx)
    assert provider.sent_to(admin.phone)[0].body == 'Gloo composed: Synthetic approved facts.'
    assert len(gloo.calls) == 2 and row.state == 'sent'


def test_notification_without_gloo_client_is_held(session, clock, provider, make_volunteer):
    ctx = FillContext(session, clock, provider, None)
    row = deliver(ctx, key='missing-client', body='Never send this raw fallback.',
                  purpose='coordinator_notify', volunteer=make_volunteer(coordinator=True))
    assert not provider.sent and row.state == 'pending'
    assert row.message_id is None and row.detail['gloo_attempts'] == 1
    assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None

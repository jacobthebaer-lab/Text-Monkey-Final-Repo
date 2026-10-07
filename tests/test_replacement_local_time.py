"""Replacement invitations use actual church-local facts before exact review."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.core import algorithm_outreach, invitation_facts, offer_windows
from app.core.send_gate import SendGate
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.tools import fill_agent_tools, replacement_pool, shift_context


def set_zone(session, zone):
    row = session.get(m.Policy, 'church_timezone')
    if row:
        row.value = {'value': zone}
    else:
        session.add(m.Policy(key='church_timezone', value={'value': zone}))
    session.flush()


@pytest.mark.parametrize('zone,start,end,local_date,weekday,offset,start_time,end_time', [
    ('America/Denver', '2026-10-11T15:00:00Z', '2026-10-11T16:15:00Z', '2026-10-11', 'Sunday', '-06:00', '9:00am', '10:15am'),
    ('America/Denver', '2026-11-08T16:00:00Z', '2026-11-08T17:15:00Z', '2026-11-08', 'Sunday', '-07:00', '9:00am', '10:15am'),
    ('America/New_York', '2026-10-11T15:00:00Z', '2026-10-11T16:15:00Z', '2026-10-11', 'Sunday', '-04:00', '11:00am', '12:15pm'),
    ('America/Denver', '2027-01-01T01:00:00Z', '2027-01-01T02:15:00Z', '2026-12-31', 'Thursday', '-07:00', '6:00pm', '7:15pm'),
])
def test_source_labels_use_event_offset_and_local_calendar_day(session, clock, make_shift, zone,
        start, end, local_date, weekday, offset, start_time, end_time):
    set_zone(session, zone)
    slot = make_shift(starts=datetime.fromisoformat(start.replace('Z', '+00:00')))
    slot.event.ends_at = datetime.fromisoformat(end.replace('Z', '+00:00'))
    session.flush()
    facts = shift_context(session, slot, clock.now())
    assert facts['timezone'] == zone and facts['weekday_name'] == weekday
    assert facts['local_starts_at'].startswith(local_date)
    assert facts['local_starts_at'].endswith(offset)
    assert start_time in facts['invitation_label'] and end_time in facts['invitation_label']
    assert invitation_facts.copy_problem(session, slot, 'Could you serve on '+facts['invitation_label']+'?') is None


def test_fold_and_overnight_labels_keep_both_dated_offsets(session, make_shift):
    slot = make_shift(starts=datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc), minutes=45)
    facts = invitation_facts.shift_labels(session, slot)
    assert '1:30am MDT (UTC-0600)' in facts['invitation_label']
    assert '1:15am MST (UTC-0700)' in facts['invitation_label']
    assert invitation_facts.copy_problem(session, slot, facts['invitation_label']) is None
    slot.event.starts_at = datetime(2026, 10, 12, 5, 30, tzinfo=timezone.utc)
    slot.event.ends_at = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)
    session.flush()
    overnight = invitation_facts.shift_labels(session, slot)['invitation_label']
    assert 'Sun Oct 11' in overnight and 'Mon Oct 12' in overnight
    assert invitation_facts.copy_problem(session, slot, overnight) is None


def tools_for_review(session, clock, provider, make_volunteer, make_shift):
    slot = make_shift(starts=datetime(2026, 10, 11, 15, tzinfo=timezone.utc))
    person = make_volunteer('Elena Brooks')
    fill = m.FillRequest(shift_id=slot.id, state='in_progress', urgency='normal', current_tranche=1,
                         created_at=clock.now())
    session.add(fill); session.flush()
    session.add(m.Policy(key='algorithm_outreach_enabled', value={'value': True}))
    session.flush()
    plan = algorithm_outreach.plan(session, fill, replacement_pool(session, fill, clock.now(), 'America/Denver'), clock.now())
    algorithm_outreach.reserve(session, fill, plan, clock.now())
    outreach = session.scalar(select(m.Outreach).where(m.Outreach.fill_request_id == fill.id))
    assert outreach and outreach.volunteer_id == person.id
    session.info.update(competition_confirmation_required=True, confirmation_now=clock.now(), record_authorized=True)
    return slot, person, outreach, fill_agent_tools(session, clock, SendGate(session, clock, provider), fill)


@pytest.mark.parametrize('wording', [
    'Could you serve Sunday from 3:00–4:15PM?',
    'Could you serve on Sunday?',
    '{label} Please arrive at 3pm.',
    '{label} Another time is 15:00 to 16:15.',
    '{label} On Monday.',
    '{label} On 2026-10-12.',
    '{label} Times are UTC.',
    '{label} In America/New_York.',
    '{label} On Oct 12 2026.',
    '{label} Hours are 10:15am to 9:00am.',
    '{label} The shift is 10:15 to 09:00.',
    '{label} The shift is from 10:15–9:00.',
])
def test_wrong_or_missing_local_facts_never_reach_review(session, clock, provider, make_volunteer, make_shift, wording):
    slot, person, outreach, tools = tools_for_review(session, clock, provider, make_volunteer, make_shift)
    label = invitation_facts.shift_labels(session, slot)['invitation_label']
    response = tools['request_send_text'].handler({'volunteer_id': person.id, 'body': wording.format(label=label)})
    assert 'error' in response and response['shift']['invitation_label'] == label
    assert session.scalar(select(m.Approval)) is None
    assert session.scalar(select(m.Message)) is None and not provider.sent
    assert offer_windows.metadata(session, outreach) is None


@pytest.mark.parametrize('wording', [
    'Could you cover {label}? No worries if not.',
    'Hi Rebecca, would you serve {label}?',
    'Could you help on {label}, in MDT?',
    'Could you help on {label}? The shift is 09:00 to 10:15.',
    'Could you help on {label}? The shift is 9:00–10:15.',
])
def test_natural_wording_and_matching_ordered_ranges(session, make_shift, wording):
    slot = make_shift()
    label = invitation_facts.shift_labels(session, slot)['invitation_label']
    assert invitation_facts.copy_problem(session, slot, wording.format(label=label)) is None


@pytest.mark.parametrize('start,correct_zone,wrong_zone', [
    (datetime(2026, 10, 11, 15, tzinfo=timezone.utc), 'MDT', 'MST'),
    (datetime(2026, 11, 8, 16, tzinfo=timezone.utc), 'MST', 'MDT'),
])
def test_zone_abbreviation_uses_event_date_offset(session, make_shift, start, correct_zone, wrong_zone):
    slot = make_shift(starts=start)
    label = invitation_facts.shift_labels(session, slot)['invitation_label']
    assert invitation_facts.copy_problem(session, slot, label+', in '+correct_zone+'?') is None
    assert invitation_facts.copy_problem(session, slot, label+', in '+wrong_zone+'?')


def test_committed_time_change_is_refreshed_after_model_turn(session, clock, provider, make_volunteer, make_shift):
    slot, person, outreach, tools = tools_for_review(session, clock, provider, make_volunteer, make_shift)
    old = invitation_facts.shift_labels(session, slot)['invitation_label']
    session.execute(update(m.Event).where(m.Event.id == slot.event_id).values(
        starts_at=datetime(2026, 10, 11, 16, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 11, 17, 15, tzinfo=timezone.utc)), execution_options={'synchronize_session': False})
    session.commit()
    response = tools['request_send_text'].handler({'volunteer_id': person.id, 'body': 'Could you serve '+old+'?'})
    assert 'error' in response and '10:00am to 11:15am' in response['shift']['invitation_label']
    assert session.scalar(select(m.Approval)) is None and not provider.sent


def test_gloo_corrects_wrong_time_before_exact_review_and_later_zone_change_holds(session, clock, provider, make_volunteer, make_shift, tmp_path):
    slot, person, outreach, tools = tools_for_review(session, clock, provider, make_volunteer, make_shift)
    class Gloo:
        def __init__(self): self.calls = 0; self.correct_body = None
        def create_response(self, *, input, **kwargs):
            self.calls += 1
            if self.calls == 1:
                body = 'Elena, could you serve Sunday from 3:00–4:15PM?'
            elif self.calls == 2:
                rejected = json.loads(input[-1]['output'])
                assert 'error' in rejected
                assert session.scalar(select(m.Approval)) is None
                assert not provider.sent
                body = 'Elena, could you serve on '+rejected['shift']['invitation_label']+'? No worries if not.'
                self.correct_body = body
            else:
                self.last_result = json.loads(input[-1]['output'])
                return SimpleNamespace(output=[], output_text='Held for exact review.')
            call = SimpleNamespace(type='function_call', call_id=str(self.calls), name='request_send_text',
                                   arguments=json.dumps({'volunteer_id': person.id, 'body': body}))
            return SimpleNamespace(output=[call], output_text=None)
    gloo = Gloo()
    logger = RunLogger(session, clock, agent='fill_agent', trigger='replacement facts', log_dir=tmp_path)
    result = run_agent(gloo, logger, model='gloo-anthropic-claude-sonnet-4.6',
        instructions=(Path(__file__).resolve().parents[1]/'prompts/fill_agent.md').read_text(),
        user_input=json.dumps({'shift': shift_context(session, slot, clock.now())}), tools=tools, max_steps=4)
    assert result['outcome'] == 'completed' and gloo.calls == 3
    assert gloo.last_result['status'] == 'held_for_approval', gloo.last_result
    approval = session.scalar(select(m.Approval))
    metadata = offer_windows.metadata(session, outreach)
    assert approval and gloo.correct_body in approval.payload['body']
    assert '3:00' not in approval.payload['body'] and metadata.detail['draft_body'] == gloo.correct_body
    assert metadata.detail['invitation_time_contract'] == 1
    assert 'Reply yes or no by' in approval.payload['body'] and not provider.sent
    # The different reply-deadline time must not be validated as service time.
    assert offer_windows.problem(session, outreach, clock.now()) == 'offer was not dispatched'
    body, content_hash = approval.payload['body'], approval.payload['content_hash']
    set_zone(session, 'America/New_York')
    assert offer_windows.dispatch(session, outreach, None, clock.now(), exact=True)
    assert outreach.response == 'revoked'
    assert approval.payload['body'] == body and approval.payload['content_hash'] == content_hash
    assert not provider.sent


def test_unmarked_historical_offer_body_is_preserved(session, clock, provider, make_volunteer, make_shift):
    from tests.test_fill_agent import historical_invitation
    slot = make_shift()
    person = make_volunteer('Elena Brooks')
    fill = m.FillRequest(shift_id=slot.id, state='in_progress', urgency='normal', current_tranche=1, created_at=clock.now())
    session.add(fill); session.flush()
    outreach = historical_invitation(session, clock, person, fill)
    metadata = offer_windows.metadata(session, outreach)
    body = metadata.body
    assert 'invitation_time_contract' not in metadata.detail
    assert offer_windows.problem(session, outreach, clock.now()) is None
    assert metadata.body == body

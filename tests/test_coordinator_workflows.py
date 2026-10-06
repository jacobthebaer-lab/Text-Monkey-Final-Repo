"""Synthetic coordinator proposals use actual tools, exact review and stale checks."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.admin_agent import prepare
from app.agents.fill_agent import FillContext
from app.core import admin_changes, confirmations as c
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.web.texty import admin
from tests.test_confirmations import mode_app


class ScriptedGloo:
    def __init__(self, steps=(), *, fail=False):
        self.steps = list(steps)
        self.fail = fail
        self.inputs = []

    def create_response(self, **kwargs):
        self.inputs.append(kwargs)
        if self.steps:
            name, args = self.steps.pop(0)
            call = SimpleNamespace(type='function_call', name=name, arguments=json.dumps(args),
                                   call_id='synthetic-' + str(len(self.inputs)))
            return SimpleNamespace(output=[call], output_text='', usage=None)
        if self.fail:
            raise GlooUnavailableError('Synthetic outage')
        return SimpleNamespace(output=[], output_text='Prepared for exact review.', usage=None)


def context(session, clock, provider, tmp_path, steps=(), fail=False):
    return FillContext(session, clock, provider, ScriptedGloo(steps, fail=fail), log_dir=tmp_path)


def ask(ctx, coordinator, action, **args):
    ctx.gloo = ScriptedGloo([('read_context', {}), ('propose_change', {'action': action, **args})])
    result = prepare(ctx, coordinator, 'Synthetic coordinator request')
    assert result['outcome'] == 'completed' and result['applied'] is False
    return [ctx.session.get(m.Approval, i) for i in result['approval_ids']]


def approve(ctx, review):
    with ctx.session.begin_nested():
        c.decide(ctx.session, ctx.gate, review, approve=True, actor='coordinator@example.test',
                 expected=review.payload['content_hash'], now=ctx.clock.now())
    return review.payload['applied_record_id']


def test_event_type_recipe_event_and_slots_only_apply_after_each_exact_review(
    session, clock, provider, make_volunteer, make_shift, tmp_path
):
    coordinator = make_volunteer(coordinator=True)
    shift = make_shift('Greeter', required=['training'])
    ctx = context(session, clock, provider, tmp_path)
    session.info[c.MODE_KEY] = True
    reviews = ask(ctx, coordinator, 'create_event_type', name='Synthetic Festival')
    assert session.scalar(select(m.EventType)) is None
    event_type_id = approve(ctx, reviews[0])
    assert session.get(m.EventType, event_type_id).title_patterns == []
    recipe = ask(ctx, coordinator, 'set_recipe', event_type_id=event_type_id, role_id=shift.role_id, count=4)[0]
    assert session.scalar(select(m.RoleRecipe)) is None
    recipe_id = approve(ctx, recipe)
    assert session.get(m.RoleRecipe, recipe_id).count == 4
    event = ask(ctx, coordinator, 'create_event', title='Synthetic Festival', event_type_id=event_type_id,
                starts_at=(clock.now()+timedelta(days=5)).isoformat(),
                ends_at=(clock.now()+timedelta(days=5, hours=2)).isoformat())[0]
    assert len(list(session.scalars(select(m.Event)))) == 1
    event_id = approve(ctx, event)
    slots = ask(ctx, coordinator, 'add_slots', event_id=event_id, role_id=shift.role_id, count=2)
    assert session.scalar(select(m.Shift).where(m.Shift.event_id == event_id)) is None
    for review in slots:
        approve(ctx, review)
    assert [s.slot_index for s in session.scalars(select(m.Shift).where(m.Shift.event_id == event_id))] == [0, 1]
    assert session.get(m.Role, shift.role_id).required_qualifications == ['training']
    assert not provider.sent and session.scalar(select(m.Message)) is None


@pytest.mark.parametrize('changed', ['event', 'role', 'timezone', 'coordinator', 'slot', 'hash'])
def test_stale_sources_and_changed_hash_block_slots(session, clock, provider, make_volunteer, make_shift, tmp_path, changed):
    coordinator = make_volunteer(coordinator=True)
    shift = make_shift('Greeter')
    ctx = context(session, clock, provider, tmp_path)
    session.info[c.MODE_KEY] = True
    review = ask(ctx, coordinator, 'add_slots', event_id=shift.event_id, role_id=shift.role_id, count=1)[0]
    session.info['record_authorized'] = True
    if changed == 'event': shift.event.title = 'Changed after review'
    if changed == 'role': shift.role.required_qualifications = ['training']
    if changed == 'timezone': session.add(m.Policy(key='church_timezone', value={'value': 'UTC'}))
    if changed == 'coordinator': coordinator.is_coordinator = False
    if changed == 'slot': session.add(m.Shift(event_id=shift.event_id, role_id=shift.role_id, slot_index=1))
    if changed == 'hash': review.payload = {**review.payload, 'admin_change_source': {}}
    session.flush()
    session.info['record_authorized'] = False
    before = len(list(session.scalars(select(m.Shift))))
    with pytest.raises(ValueError): approve(ctx, review)
    assert len(list(session.scalars(select(m.Shift)))) == before
    assert not provider.sent


@pytest.mark.parametrize('args', [
    {'action': 'verify_qualification', 'volunteer_id': 1},
    {'action': 'add_slots', 'event_id': 99999, 'role_id': 1, 'count': 2},
    {'action': 'add_slots', 'event_id': 1, 'role_id': 1, 'count': True},
    {'action': 'add_slots', 'event_id': 1, 'role_id': 1, 'count': 31},
    {'action': 'pause_role', 'volunteer_id': 1, 'role_id': 1, 'sms_opt_in': True},
    {'action': 'create_event', 'title': 'Synthetic', 'starts_at': '2026-10-05T10:00:00', 'ends_at': '2026-10-05T11:00:00'},
    {'action': 'create_event', 'title': 'Synthetic', 'starts_at': '2026-09-01T10:00:00-06:00', 'ends_at': '2026-09-01T11:00:00-06:00'},
    {'action': 'set_recipe', 'event_type_id': True, 'role_id': 1, 'count': 3},
])
def test_invalid_or_privilege_changes_do_not_stage(session, clock, provider, make_volunteer, make_shift, tmp_path, args):
    coordinator = make_volunteer(coordinator=True)
    make_shift()
    ctx = context(session, clock, provider, tmp_path)
    with pytest.raises((ValueError, TypeError)): admin_changes.propose(ctx, coordinator, args)
    assert session.scalar(select(m.Approval)) is None
    assert not provider.sent


def test_pause_and_multimonth_unavailability_preserve_roster_consent_and_qualifications(
    session, clock, provider, make_volunteer, make_shift, assign, tmp_path
):
    coordinator = make_volunteer(coordinator=True)
    volunteer = make_volunteer(quals=[('training', 'verified', None)])
    shift = make_shift('Greeter')
    assignment = assign(volunteer, shift)
    session.add(m.Availability(volunteer_id=volunteer.id, month='2026-10',
        available_dates=['2026-10-10'], unavailable_dates=[]))
    session.flush()
    ctx = context(session, clock, provider, tmp_path)
    session.info[c.MODE_KEY] = True
    pause = ask(ctx, coordinator, 'pause_role', volunteer_id=volunteer.id, role_id=shift.role_id)[0]
    assert not volunteer.preferences.get('paused_roles')
    approve(ctx, pause)
    assert volunteer.preferences['paused_roles'] == ['Greeter'] and assignment.status == 'approved'
    assert volunteer.sms_opt_in and volunteer.qualifications[0].status == 'verified'
    dates = ask(ctx, coordinator, 'mark_unavailable', volunteer_id=volunteer.id,
                dates=['2026-10-10', '2026-11-02'])
    for review in dates: approve(ctx, review)
    rows = list(session.scalars(select(m.Availability).order_by(m.Availability.month)))
    assert rows[0].unavailable_dates == ['2026-10-10'] and rows[0].available_dates == []
    assert rows[1].unavailable_dates == ['2026-11-02']
    assert not provider.sent


def test_recipe_update_dedup_and_existing_event_unchanged(session, clock, provider, make_volunteer, make_shift, tmp_path):
    coordinator = make_volunteer(coordinator=True)
    shift = make_shift()
    group = m.EventType(name='Synthetic Group', title_patterns=[])
    session.add(group); session.flush()
    recipe = m.RoleRecipe(event_type_id=group.id, role_id=shift.role_id, count=1)
    session.add(recipe); session.flush()
    ctx = context(session, clock, provider, tmp_path)
    review = ask(ctx, coordinator, 'set_recipe', event_type_id=group.id, role_id=shift.role_id, count=3)[0]
    again = ask(ctx, coordinator, 'set_recipe', event_type_id=group.id, role_id=shift.role_id, count=3)[0]
    assert again.id == review.id and recipe.count == 1
    approve(ctx, review)
    assert recipe.count == 3 and len(list(session.scalars(select(m.Shift)))) == 1
    assert not provider.sent


def test_outage_after_proposal_expires_only_new_reviews(session, clock, provider, make_volunteer, make_shift, tmp_path):
    coordinator = make_volunteer(coordinator=True)
    shift = make_shift()
    ctx = context(session, clock, provider, tmp_path)
    old = ask(ctx, coordinator, 'add_slots', event_id=shift.event_id, role_id=shift.role_id, count=1)[0]
    ctx.gloo = ScriptedGloo([('read_context', {}), ('propose_change', {
        'action': 'add_slots', 'event_id': shift.event_id, 'role_id': shift.role_id, 'count': 2})], fail=True)
    result = prepare(ctx, coordinator, 'Synthetic request with outage')
    assert result['outcome'] == 'gloo_unavailable'
    reviews = [session.get(m.Approval, i) for i in result['approval_ids']]
    assert len(reviews) == 2 and old.status == 'pending'
    assert [r.status for r in reviews if r.id != old.id] == ['expired']
    assert len(list(session.scalars(select(m.Shift)))) == 1 and not provider.sent


def test_missing_context_and_sensitive_request_cannot_prepare_changes(session, clock, provider, make_volunteer, tmp_path):
    coordinator = make_volunteer(coordinator=True)
    ctx = context(session, clock, provider, tmp_path, [('propose_change', {'action': 'create_event_type', 'name': 'Synthetic'})])
    result = prepare(ctx, coordinator, 'Synthetic command')
    assert result['approval_ids'] == [] and session.scalar(select(m.EventType)) is None
    ctx.gloo = ScriptedGloo()
    result = prepare(ctx, coordinator, 'I want to hurt myself')
    assert result['outcome'] == 'human_review' and not ctx.gloo.inputs
    assert not provider.sent


def test_signed_in_api_proposal_visible_and_exact_review_only(mode_app, session, make_volunteer, tmp_path):
    app, _, _ = mode_app
    coordinator = make_volunteer(coordinator=True); session.commit()
    app.state.gloo = ScriptedGloo([('read_context', {}), ('propose_change', {'action': 'create_event_type', 'name': 'Synthetic API Group'})])
    with TestClient(app) as client:
        data = {'coordinator_id': coordinator.id, 'command': 'Create a synthetic group'}
        assert client.post('/api/coordinator/command', json=data).status_code == 401
        app.dependency_overrides[admin] = lambda: {'email': 'coordinator@example.test'}
        assert client.post('/api/coordinator/command', json={**data, 'approve': True}).status_code == 422
        response = client.post('/api/coordinator/command', json=data)
        assert response.status_code == 200, response.text
        result = response.json()
        review = result['reviews'][0]
        assert result['state'] == 'pending_exact_review' and result['sent'] == 0
        state = client.get('/api/state').json()
        assert any(p['id'] == str(review['id']) and p['content_hash'] == review['content_hash'] for p in state['proposals'])
        assert client.post(f"/api/proposals/{review['id']}/approve", json={'content_hash': 'wrong'}).status_code == 409
        assert client.post(f"/api/proposals/{review['id']}/approve", json={'content_hash': review['content_hash']}).status_code == 200
        assert client.post('/mac/outbound/pull', headers={'Authorization': 'Bearer synthetic-bridge-'+'x'*40}, json={}).json()['messages'] == []
    with app.state.session_factory() as saved:
        assert saved.scalar(select(m.EventType)).name == 'Synthetic API Group'
        assert saved.scalar(select(m.Message)) is None


@pytest.mark.parametrize('changed', ['role', 'new_duplicate', 'before'])
def test_recipe_sources_must_still_match_at_review(session, clock, provider, make_volunteer, make_shift, tmp_path, changed):
    coordinator = make_volunteer(coordinator=True)
    shift = make_shift()
    group = m.EventType(name='Synthetic Recipe Group', title_patterns=[])
    session.add(group); session.flush()
    recipe = m.RoleRecipe(event_type_id=group.id, role_id=shift.role_id, count=1)
    session.add(recipe); session.flush()
    ctx = context(session, clock, provider, tmp_path)
    review = ask(ctx, coordinator, 'set_recipe', event_type_id=group.id, role_id=shift.role_id, count=3)[0]
    if changed == 'role': shift.role.required_qualifications = ['new_training']
    if changed == 'before': recipe.count = 2
    if changed == 'new_duplicate': session.add(m.RoleRecipe(event_type_id=group.id, role_id=shift.role_id, count=9))
    session.flush()
    before = recipe.count
    with pytest.raises(ValueError): approve(ctx, review)
    assert recipe.count == before and not provider.sent


def test_event_times_rechecked_and_updates_preserve_external_source(session, clock, provider, make_volunteer, make_shift, tmp_path):
    coordinator = make_volunteer(coordinator=True)
    shift = make_shift(starts=clock.now()+timedelta(hours=1))
    shift.event.gcal_event_id = 'synthetic-external-event'
    session.flush()
    ctx = context(session, clock, provider, tmp_path)
    review = ask(ctx, coordinator, 'update_event', event_id=shift.event_id, title='Synthetic new title')[0]
    assert review.payload['after']['gcal_event_id'] == 'synthetic-external-event'
    clock.advance(timedelta(hours=1, minutes=30))
    with pytest.raises(ValueError): approve(ctx, review)
    assert shift.event.title != 'Synthetic new title'
    assert not provider.sent


def test_step_exhaustion_holds_partial_proposal(session, clock, provider, make_volunteer, tmp_path):
    coordinator = make_volunteer(coordinator=True)
    ctx = context(session, clock, provider, tmp_path, [
        ('read_context', {}), ('propose_change', {'action': 'create_event_type', 'name': 'Synthetic bounded group'})])
    ctx.gloo.settings = SimpleNamespace(agent_model='synthetic', max_agent_steps=2)
    result = prepare(ctx, coordinator, 'Synthetic bounded request')
    assert result['outcome'] == 'max_steps'
    assert session.get(m.Approval, result['approval_ids'][0]).status == 'expired'
    assert session.scalar(select(m.EventType)) is None and not provider.sent


def test_proposal_tool_does_not_export_full_profile_to_gloo(session, clock, provider, make_volunteer, make_shift, tmp_path):
    coordinator = make_volunteer(coordinator=True)
    volunteer = make_volunteer(prefs={'private_history': 'Synthetic internal-only profile detail'})
    shift = make_shift()
    ctx = context(session, clock, provider, tmp_path)
    review = ask(ctx, coordinator, 'pause_role', volunteer_id=volunteer.id, role_id=shift.role_id)[0]
    sent_to_model = json.dumps(ctx.gloo.inputs)
    assert volunteer.phone not in sent_to_model
    assert 'Synthetic internal-only profile detail' not in sent_to_model
    assert review.payload['before']['preferences']['private_history'] == 'Synthetic internal-only profile detail'


def test_api_holds_without_exact_mode_or_active_coordinator(mode_app):
    from dataclasses import replace
    app, volunteers, _ = mode_app
    app.dependency_overrides[admin] = lambda: {'email': 'coordinator@example.test'}
    with TestClient(app) as client:
        assert client.post('/api/coordinator/command', json={'coordinator_id': volunteers[0].id, 'command': 'Who is serving?'}).status_code == 422
        app.state.settings = replace(app.state.settings, competition_confirmation_required=False)
        assert client.get('/api/coordinator').status_code == 409
        assert client.post('/api/coordinator/capacity', json={}).status_code == 409


@pytest.mark.parametrize('when', ['before_proposal', 'after_proposal'])
def test_event_time_changes_cannot_invalidate_existing_assignments(
    session, clock, provider, make_volunteer, make_shift, assign, tmp_path, when
):
    coordinator = make_volunteer(coordinator=True)
    volunteer = make_volunteer()
    shift = make_shift()
    ctx = context(session, clock, provider, tmp_path)
    args = {'action': 'update_event', 'event_id': shift.event_id,
            'starts_at': (shift.event.starts_at+timedelta(hours=2)).isoformat(),
            'ends_at': (shift.event.ends_at+timedelta(hours=2)).isoformat()}
    if when == 'before_proposal':
        assignment = assign(volunteer, shift)
        with pytest.raises(ValueError, match='existing assignments'):
            admin_changes.propose(ctx, coordinator, args)
        assert session.scalar(select(m.Approval)) is None
    else:
        review = session.get(m.Approval, admin_changes.propose(ctx, coordinator, args)['approval_ids'][0])
        assignment = assign(volunteer, shift)
        with pytest.raises(ValueError, match='existing assignments'):
            approve(ctx, review)
    assert assignment.status == 'approved' and not provider.sent

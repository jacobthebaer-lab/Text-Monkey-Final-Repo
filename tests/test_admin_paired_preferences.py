"""Coordinator pair intake through actual tools and review, synthetic evidence only."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.admin_agent import prepare
from app.agents.fill_agent import FillContext
from app.core import confirmations as c, paired_planning
from app.db import models as m
from app.web.texty import admin
from tests.test_admin_planning_tools import PlanningGloo, tools
from tests.test_confirmations import mode_app


@pytest.fixture
def participants(session, make_volunteer, make_shift):
    coordinator = make_volunteer('Laura Bennett', coordinator=True)
    roles = [make_shift('Greeter').role, make_shift('Production', required=('production_training',)).role]
    prefs = {'onboarding_stage': 'complete', 'interested_roles': [r.name for r in roles],
        'private_note': 'Keep this private', 'pending_constraints': [
            {'kind': 'same_day', 'role_ids': [r.id for r in roles], 'description': 'Keep private wording'},
            {'kind': 'unknown', 'description': 'Unrelated unresolved preference'}]}
    person = make_volunteer('Rebecca Miller', opt_in=False, status='inactive', prefs=prefs)
    session.flush()
    return coordinator, person, roles


def stage_args(person, roles, mutate=None, **overrides):
    def step(kwargs):
        result = json.loads(next(i['output'] for i in reversed(kwargs['input'])
            if i.get('type') == 'function_call_output'))
        if mutate:
            mutate()
        return 'stage_paired_preference_review', {'volunteer_id': person.id,
            'evidence_hash': result['evidence_hash'], 'pairs': [{'role_ids': [r.id for r in roles]}],
            'resolved_constraint_indexes': [0], **overrides}
    return step


def context(session, clock, provider, tmp_path, person, roles, *, mutate=None, fail=False, **overrides):
    return FillContext(session, clock, provider, PlanningGloo([('read_context', {}),
        ('read_paired_preferences', {'volunteer_id': person.id}),
        stage_args(person, roles, mutate, **overrides)], fail=fail), log_dir=tmp_path)


def test_pair_tool_uses_existing_rule_review_preserves_unrelated_facts_and_deduplicates(
    session, clock, provider, tmp_path, participants
):
    coordinator, person, roles = participants
    before = c.values(person)
    ctx = context(session, clock, provider, tmp_path, person, roles)
    result = prepare(ctx, coordinator, 'Review Rebecca serving Greeter and Production on the same day')
    review = session.get(m.Approval, result['approval_ids'][0])
    assert review.kind == 'confirm_record' and review.status == 'pending' and c.valid(review, clock.now())
    assert review.payload['workflow_planning_rules']['rules']['same_day_role_pairs'] == [{'role_ids': [r.id for r in roles]}]
    assert c.values(person) == before and not result['applied']
    assert session.info.get(c.MODE_KEY) is None
    exported = json.dumps(ctx.gloo.inputs)
    assert person.phone not in exported and 'Keep this private' not in exported and 'Keep private wording' not in exported
    again = prepare(context(session, clock, provider, tmp_path, person, roles), coordinator, 'Review the same pair')
    assert again['approval_ids'] == result['approval_ids']
    c.decide(session, ctx.gate, review, approve=True, actor='coordinator@example.test',
        expected=review.payload['content_hash'], now=clock.now())
    assert person.preferences['pending_constraints'] == before['preferences']['pending_constraints'][1:]
    assert person.preferences['private_note'] == 'Keep this private'
    assert person.preferences['same_day_role_pairs'] == [{'role_ids': [r.id for r in roles]}]
    assert session.get(m.Policy, f'planning-rules:{person.id}').value['approval_id'] == review.id
    assert paired_planning.rule_problem(session, person) == 'Stated scheduling constraints need exact coordinator review.'
    assert not person.sms_opt_in and person.status == 'inactive'
    assert not session.scalar(select(m.Qualification)) and not session.scalar(select(m.Assignment))
    assert not session.scalar(select(m.Message)) and not provider.sent


@pytest.mark.parametrize('change', ['person', 'role', 'coordinator'])
@pytest.mark.parametrize('phase', ['before_stage', 'before_approval'])
def test_changed_pair_source_is_held(session, clock, provider, tmp_path, participants, change, phase):
    coordinator, person, roles = participants
    original = deepcopy(person.preferences)
    def mutate():
        if change == 'person':
            person.preferences = {**person.preferences, 'additional_fact': 'Changed current fact'}
        elif change == 'role':
            roles[1].required_qualifications = ['additional_training']
        else:
            coordinator.is_coordinator = False
        session.flush()
    ctx = context(session, clock, provider, tmp_path, person, roles,
        mutate=mutate if phase == 'before_stage' else None)
    result = prepare(ctx, coordinator, 'Prepare the pair preference')
    if phase == 'before_stage':
        assert result['approval_ids'] == [] and 'error' in tools(ctx)['stage_paired_preference_review']
    else:
        review = session.get(m.Approval, result['approval_ids'][0])
        mutate()
        with pytest.raises(ValueError):
            c.decide(session, ctx.gate, review, approve=True, actor='coordinator@example.test',
                expected=review.payload['content_hash'], now=clock.now())
    assert person.preferences['pending_constraints'] == original['pending_constraints']
    assert 'same_day_role_pairs' not in person.preferences and not provider.sent


@pytest.mark.parametrize('overrides', [
    {'evidence_hash': 'invented'}, {'resolved_constraint_indexes': [True]},
    {'resolved_constraint_indexes': [0, 0]}, {'resolved_constraint_indexes': [1]},
    {'pairs': [{'role_ids': [99998, 99999]}]}, {'pairs': [{'role_ids': [True, 2]}]},
    {'extra': 'untrusted'},
])
def test_invalid_pair_request_cannot_resolve_pending_constraints(
    session, clock, provider, tmp_path, participants, overrides
):
    coordinator, person, roles = participants
    before = c.values(person)
    ctx = context(session, clock, provider, tmp_path, person, roles, **overrides)
    assert prepare(ctx, coordinator, 'Review a pair')['approval_ids'] == []
    assert 'error' in tools(ctx)['stage_paired_preference_review'] and c.values(person) == before
    assert not session.scalar(select(m.Approval)) and not provider.sent


def test_pair_tool_requires_specific_read_and_gloo_completion(session, clock, provider, tmp_path, participants):
    coordinator, person, roles = participants
    args = {'volunteer_id': person.id, 'evidence_hash': 'invented',
        'pairs': [{'role_ids': [r.id for r in roles]}], 'resolved_constraint_indexes': [0]}
    ctx = FillContext(session, clock, provider, PlanningGloo([('read_context', {}),
        ('stage_paired_preference_review', args)]), log_dir=tmp_path)
    assert prepare(ctx, coordinator, 'Review the pair')['approval_ids'] == []
    ctx = context(session, clock, provider, tmp_path, person, roles, fail=True)
    result = prepare(ctx, coordinator, 'Review the pair')
    assert result['outcome'] == 'gloo_unavailable'
    assert session.get(m.Approval, result['approval_ids'][0]).status == 'expired'
    assert 'same_day_role_pairs' not in person.preferences and not provider.sent


def test_signed_in_coordinator_route_and_exact_hash_review_work_with_texting_mode_off(mode_app, session, participants):
    app, _, _ = mode_app
    coordinator, person, roles = participants
    session.commit()
    app.state.settings = replace(app.state.settings, competition_confirmation_required=False)
    app.state.gloo = PlanningGloo([('read_context', {}),
        ('read_paired_preferences', {'volunteer_id': person.id}), stage_args(person, roles)])
    payload = {'coordinator_id': coordinator.id, 'command': 'Review Rebecca serving Greeter and Production together on the same date'}
    with TestClient(app) as client:
        assert client.post('/api/coordinator/command', json=payload).status_code == 401
        app.dependency_overrides[admin] = lambda: {'email': 'coordinator@example.test'}
        response = client.post('/api/coordinator/command', json=payload)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['state'] == 'pending_exact_review' and data['sent'] == 0
        review = data['reviews'][0]
        assert client.post(f"/api/proposals/{review['id']}/approve", json={'content_hash': 'wrong'}).status_code == 409
        response = client.post(f"/api/proposals/{review['id']}/approve", json={'content_hash': review['content_hash']})
        assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(m.Volunteer, person.id).preferences['same_day_role_pairs'] == [{'role_ids': [r.id for r in roles]}]
    assert not app.state.settings.competition_confirmation_required
    assert not session.scalar(select(m.Message)) and not session.scalar(select(m.Assignment))


def test_new_pair_and_explicit_removal_use_fresh_reviews_without_assigning(session, clock, provider, tmp_path, participants):
    coordinator, person, roles = participants
    person.preferences = {**person.preferences, 'pending_constraints': []}
    ctx = context(session, clock, provider, tmp_path, person, roles, resolved_constraint_indexes=[])
    first = session.get(m.Approval, prepare(ctx, coordinator, 'Review a new explicit pair')['approval_ids'][0])
    c.decide(session, ctx.gate, first, approve=True, actor='coordinator@example.test',
        expected=first.payload['content_hash'], now=clock.now())
    assert paired_planning.rule_problem(session, person) is None
    ctx = context(session, clock, provider, tmp_path, person, roles, pairs=[], resolved_constraint_indexes=[])
    second = session.get(m.Approval, prepare(ctx, coordinator, 'Remove the existing pair requirement')['approval_ids'][0])
    assert second.id != first.id and person.preferences['same_day_role_pairs']
    c.decide(session, ctx.gate, second, approve=True, actor='coordinator@example.test',
        expected=second.payload['content_hash'], now=clock.now())
    assert person.preferences['same_day_role_pairs'] == [] and paired_planning.rule_problem(session, person) is None
    assert not person.sms_opt_in and not session.scalar(select(m.Assignment)) and not provider.sent


def test_expired_selected_session_cannot_stage_pair_review(session, clock, provider, tmp_path, participants):
    from datetime import timedelta
    from app.integrations.test_sessions import TestSession as SelectedSession
    coordinator, person, roles = participants
    session.info['mac_test_session'] = SelectedSession('0' * 32, clock.now() - timedelta(hours=1), clock.now())
    ctx = context(session, clock, provider, tmp_path, person, roles)
    result = prepare(ctx, coordinator, 'Prepare the exact pair')
    assert result['approval_ids'] == [] and 'error' in tools(ctx)['stage_paired_preference_review']
    assert not session.scalar(select(m.Approval)) and 'same_day_role_pairs' not in person.preferences
    assert not provider.sent

"""Gloo narration cannot add claims or contact people; evidence survives holds."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.capacity_agent import scan
from app.agents.fill_agent import FillContext
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.web.texty import admin
from tests.test_confirmations import mode_app


class NarrationGloo:
    def __init__(self, *, corrupt=None, fail=False, mutate=None):
        self.calls = []
        self.corrupt = corrupt
        self.fail = fail
        self.mutate = mutate

    def create_response(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            flags = json.loads(kwargs['input'][0]['content'])['flags']
            calls = []
            for fact in flags:
                args = {'flag_id': fact['flag_id'], 'source_hash': fact['source_hash'],
                        'summary': fact['allowed_summaries'][-1], 'suggested_action': fact['next_step']}
                if self.corrupt == 'claim': args['summary'] = 'This volunteer served 99 times and has burnout.'
                if self.corrupt == 'authority': args['suggested_action'] = 'Training verified. Transfer the volunteer today.'
                if self.corrupt == 'hash': args['source_hash'] = 'stale'
                if self.corrupt == 'identity': args['flag_id'] = 99999
                if self.corrupt == 'style': args['summary'] = fact['allowed_summaries'][0] + '\u2014'
                if self.corrupt == 'extra': args['contact'] = True
                calls.append(SimpleNamespace(type='function_call', name='narrate_flag', arguments=json.dumps(args),
                                             call_id='synthetic-'+str(fact['flag_id'])))
            if self.mutate: self.mutate()
            return SimpleNamespace(output=calls, output_text='', usage=None)
        if self.fail: raise GlooUnavailableError('Synthetic narration outage')
        return SimpleNamespace(output=[], output_text='Flags prepared for human review.', usage=None)


def ctx(session, clock, provider, tmp_path, **kwargs):
    return FillContext(session, clock, provider, NarrationGloo(**kwargs), log_dir=tmp_path)


def test_narration_uses_actual_flag_sources_and_deduplicates(session, clock, provider, make_shift, tmp_path):
    shift = make_shift('Greeter')
    current = ctx(session, clock, provider, tmp_path)
    flags = scan(current)
    assert flags and all(f.evidence['narration']['state'] == 'ready' for f in flags)
    flag = next(f for f in flags if f.type == 'single_point_of_failure')
    assert flag.evidence['role_id'] == shift.role_id and flag.summary.startswith('For your review: ')
    assert flag.summary.endswith(flag.evidence['observation'])
    assert flag.suggested_action == flag.evidence['next_step']
    assert len(flag.evidence['narration']['source_hash']) == 64
    count = len(list(session.scalars(select(m.Flag))))
    current.gloo = NarrationGloo()
    scan(current)
    assert len(list(session.scalars(select(m.Flag)))) == count
    assert session.scalar(select(m.Message)) is None and not provider.sent
    run = session.scalar(select(m.AgentRun).order_by(m.AgentRun.id.desc()))
    assert run.outcome == 'completed' and any(s.tool_name == 'narrate_flag' for s in run.step_rows)


@pytest.mark.parametrize('corrupt', ['claim', 'authority', 'hash', 'identity', 'style', 'extra'])
def test_invented_claims_or_authority_hold_narration_without_losing_evidence(
    session, clock, provider, make_shift, tmp_path, corrupt
):
    make_shift('Greeter')
    flags = scan(ctx(session, clock, provider, tmp_path, corrupt=corrupt))
    assert flags and all(f.evidence['narration']['state'] == 'held' for f in flags)
    assert all(f.summary == 'Gloo narration held. Review the structured evidence.' for f in flags)
    assert all(f.suggested_action is None and f.evidence['role_id'] for f in flags)
    assert all(f.evidence['observation'] and f.evidence['scan_at'] for f in flags)
    assert not provider.sent and session.scalar(select(m.Message)) is None


def test_outage_after_tools_does_not_publish_partial_narration(session, clock, provider, make_shift, tmp_path):
    make_shift()
    flags = scan(ctx(session, clock, provider, tmp_path, fail=True))
    assert flags and all(f.evidence['narration']['state'] == 'held' for f in flags)
    assert all(f.evidence['narration']['outcome'] == 'gloo_unavailable' for f in flags)
    assert not provider.sent


def test_changed_evidence_during_composition_holds_old_claims(session, clock, provider, make_shift, tmp_path):
    make_shift()
    def mutate():
        for flag in session.scalars(select(m.Flag)):
            flag.evidence = {**flag.evidence, 'interested_qualified': 99}
    flags = scan(ctx(session, clock, provider, tmp_path, mutate=mutate))
    assert flags and all(f.evidence['narration']['state'] == 'held' for f in flags)
    assert not provider.sent


def test_empty_scan_does_not_call_gloo(session, clock, provider, tmp_path):
    current = ctx(session, clock, provider, tmp_path)
    assert scan(current) == [] and current.gloo.calls == []


def test_capacity_api_requires_auth_and_preserves_evidence_on_gloo_outage(mode_app, session, make_shift):
    app, _, _ = mode_app
    make_shift(); session.commit()
    app.state.gloo = NarrationGloo(fail=True)
    with TestClient(app) as client:
        assert client.post('/api/coordinator/capacity', json={}).status_code == 401
        app.dependency_overrides[admin] = lambda: {'email': 'coordinator@example.test'}
        result = client.post('/api/coordinator/capacity', json={})
        assert result.status_code == 200, result.text
        assert result.json()['state'] == 'held' and result.json()['sent'] == 0
        flags = client.get('/api/coordinator/capacity').json()['flags']
        assert flags and all(f['evidence']['narration']['state'] == 'held' for f in flags)
        assert client.post('/api/coordinator/capacity', json={'send': True}).status_code == 422
    with app.state.session_factory() as saved:
        assert saved.scalar(select(m.Message)) is None

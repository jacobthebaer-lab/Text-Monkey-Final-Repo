"""Exercise the current inbound signup contract, entirely offline."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def replay(monkeypatch):
    source = Path(__file__).parents[1] / 'tools/check_synthetic_gloo_signup.py'
    spec = importlib.util.spec_from_file_location('current_signup_replay', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Even an accidental unmocked model/transport call must fail immediately.
    import httpx
    monkeypatch.setattr(httpx.Client, 'request', lambda *a, **k: pytest.fail('No network in replay tests'))
    return module


class ScriptedGloo:
    def __init__(self, *, failure=None, changed_copy=None, bad_window=False):
        self.calls = []
        self.failure = failure
        self.changed_copy = changed_copy
        self.bad_window = bad_window

    def create_response(self, **kwargs):
        from app.llm.gloo_client import GlooUnavailableError
        facts = json.loads(kwargs['input'])
        kind = ('identity' if isinstance(facts, list) else
                'copy' if 'approved_message' in facts else facts['stage'])
        if self.failure == kind:
            raise GlooUnavailableError('synthetic outage, private detail must not appear in report')
        if kind == 'identity':
            named = facts[-1]['body'] == 'Jordan Demo'
            data = dict(signup=named, first_name='Jordan' if named else None,
                        last_name='Demo' if named else None, identity_reply=named)
            output = json.dumps(data)
        elif kind == 'copy':
            output = self.changed_copy or facts['approved_message']
        elif kind == 'interests':
            output = json.dumps(dict(understood=True, any_role=False, role_ids=[1]))
        else:
            assert kind == 'availability'
            output = json.dumps(dict(understood=True, availability_known=True,
                frequency_known=True, weekdays=[6], all_day=False, preferred_services=[],
                max_per_month=2, available_dates=[], unavailable_dates=[],
                recurring_windows=[dict(weekday=6, role_ids=[1], role_label='Greeter',
                    any_role=False, time_mode='clock', start_time='09:00',
                    end_time='11:00' if self.bad_window else '10:00', all_day=False, event_context=None)]))
        self.calls.append(kind)
        return SimpleNamespace(output_text=output, usage=SimpleNamespace(input_tokens=3, output_tokens=4))

    def total_usage(self):
        return dict(calls=len(self.calls), input_tokens=3*len(self.calls), output_tokens=4*len(self.calls))


def test_actual_current_inbound_exact_signup_and_quiet_completion(replay):
    model = ScriptedGloo()
    result = replay.run_signup(gloo=model, model_provenance='scripted_gloo')
    assert result['passed'], result
    assert [s['fictional_input'] for s in result['steps']] == [
        'JOIN', 'Jordan Demo', 'Greeter', 'Sundays 9-10am, twice a month']
    assert [len(s['mock_messages']) for s in result['steps']] == [1, 1, 1, 0]
    assert all(m['status'] == 'sent' and m['provider_sid'].startswith('MOCK')
               for s in result['steps'] for m in s['mock_messages'])
    assert result['fictional_profile']['preferences']['consent_source'] == 'sms_name_reply_to_exact_invitation'
    assert result['consent_provenance']['verified'] is True
    assert result['consent_provenance']['disclosure_message_id'] < result['consent_provenance']['reply_message_id']
    assert result['fictional_profile']['preferences']['recurring_windows'][0]['end_time'] == '10:00'
    assert result['real_messages_sent'] == 0
    assert result['composition'] == 'scripted_gloo'
    assert result['real_gloo_usage']['calls'] == 0
    assert result['scripted_gloo_usage']['calls'] == len(model.calls) > 0
    assert any(row['agent'] == 'signup_reply' and row['outcome'] == 'exact_copy_composed'
               for row in result['gloo_audit'])


@pytest.mark.parametrize('failure', ['identity', 'copy', 'interests', 'availability'])
def test_outage_is_truthful_failure_with_no_template_fallback(replay, failure):
    result = replay.run_signup(gloo=ScriptedGloo(failure=failure))
    assert not result['passed']
    assert result['real_messages_sent'] == 0
    assert 'private detail' not in json.dumps(result)
    # Earlier successful prompts can exist; the failing step never emits fallback copy.
    if result['steps']:
        assert result['steps'][-1]['mock_messages'] == []


@pytest.mark.parametrize('copy', ['Welcome, please reply YES to sign up.', 'Welcome — please sign up.'])
def test_changed_exact_copy_or_em_dash_is_held(replay, copy):
    result = replay.run_signup(gloo=ScriptedGloo(changed_copy=copy))
    assert not result['passed']
    assert len(result['steps']) == 1
    assert result['steps'][0]['mock_messages'] == []
    assert any(row['outcome'] in {'invalid_exact_copy', 'invalid_typography'} for row in result['gloo_audit'])


def test_wrong_saved_window_cannot_be_reported_as_success(replay):
    result = replay.run_signup(gloo=ScriptedGloo(bad_window=True))
    assert not result['passed']
    assert len(result['steps']) == 4
    assert result['steps'][-1]['mock_messages'] == []


def test_suppressed_intake_is_not_claimed_as_mock_delivery(replay, monkeypatch):
    from app.core import signup_delivery
    from app.core.send_gate import SendOutcome, SendStatus
    monkeypatch.setattr(signup_delivery, 'intake_block',
                        lambda *a, **k: SendOutcome(SendStatus.BLOCKED_POLICY, reason='synthetic hold'))
    model = ScriptedGloo()
    result = replay.run_signup(gloo=model)
    assert not result['passed']
    assert result['steps'][0]['route'] == 'signup_invitation'
    assert result['steps'][0]['mock_messages'] == []
    assert model.calls == ['identity']  # No needless outgoing composition.


def test_missing_actual_key_fails_safely_before_inherited_runtime_import(tmp_path):
    private_db = tmp_path / 'must-not-create.db'
    output = tmp_path / 'fresh-output'
    env = {**os.environ, 'DATABASE_URL':'sqlite:///' + str(private_db),
        'SMS_PROVIDER':'mac_messages', 'LIVE_SMS':'true', 'MAC_BRIDGE_ENABLED':'true',
        'AUTOMATION_ENABLED':'true', 'PROFILE_SYNC_ENABLED':'true',
        'PROFILE_SYNC_DATABASE_URL':'postgresql://invalid.invalid/private',
        'PCO_REVIEW_ENABLED':'true', 'PCO_REVIEW_SIGNING_KEY_PATH':str(tmp_path/'absent-key'),
        'PCO_STAFFING_WRITE_ENABLED':'true', 'PCO_STAFFING_POLL_ENABLED':'true',
        'GLOO_API_KEY':'', 'PYTHON_DOTENV_DISABLED':'1'}
    command = [sys.executable, str(Path(__file__).parents[1]/'tools/check_synthetic_gloo_signup.py'),
               '--output-dir', str(output)]
    process = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
    assert process.returncode == 1, process.stderr
    result = json.loads((output/'synthetic-gloo-signup.json').read_text())
    assert not private_db.exists()
    assert result['error_type'] == 'GlooUnavailableError'
    assert result['steps'] == []
    assert result['real_gloo_usage']['calls'] == 0
    assert 'invalid.invalid' not in process.stdout + process.stderr + json.dumps(result)


def test_provider_ack_without_actual_mock_delivery_cannot_pass(replay, monkeypatch):
    from app.sms.mock_provider import MockSMSProvider
    monkeypatch.setattr(MockSMSProvider, 'send', lambda *a, **k: 'MOCK-NOT-DELIVERED')
    result = replay.run_signup(gloo=ScriptedGloo())
    assert not result['passed']
    assert result['steps'][0]['simulated_replies'] == []
    assert result['steps'][0]['mock_messages'][0]['status'] == 'sent'
    assert result['steps'][0]['mock_messages'][0]['provider_sid'] == 'MOCK-NOT-DELIVERED'

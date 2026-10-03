"""Replay evidence must survive repeated runs, collisions and symlinks."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def replay(monkeypatch, tmp_path):
    source = Path(__file__).parents[1] / 'tools/check_synthetic_gloo_signup.py'
    spec = importlib.util.spec_from_file_location('signup_replay_output', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'root', tmp_path.resolve())
    monkeypatch.setattr(module, 'run_signup', lambda: {
        'passed': True, 'real_gloo_usage': {}, 'real_messages_sent': 0,
    })
    return module


def test_repeated_default_runs_preserve_all_prior_evidence(replay):
    reports = replay.root / 'evals/reports'
    reports.mkdir(parents=True)
    legacy = reports / 'synthetic-gloo-signup.json'
    legacy.write_text('historical proof')
    assert replay.main([]) == 0
    first = next(reports.glob('signup-*/synthetic-gloo-signup.json'))
    proof = first.read_bytes()
    assert replay.main([]) == 0
    assert len(list(reports.glob('signup-*/synthetic-gloo-signup.json'))) == 2
    assert first.read_bytes() == proof
    assert legacy.read_text() == 'historical proof'


@pytest.mark.parametrize('existing', ['directory', 'file', 'symlink'])
def test_explicit_output_conflicts_before_model_execution(replay, monkeypatch, existing):
    output = replay.root / 'chosen'
    target = replay.root / 'prior-proof'
    target.write_text('keep')
    if existing == 'directory':
        output.mkdir()
    elif existing == 'file':
        output.write_text('keep')
    else:
        output.symlink_to(target)
    monkeypatch.setattr(replay, 'run_signup', lambda: pytest.fail('Model must not run on conflict'))
    with pytest.raises(SystemExit) as failure:
        replay.main(['--output-dir', str(output)])
    assert failure.value.code == 2
    assert target.read_text() == 'keep'
    if existing == 'file':
        assert output.read_text() == 'keep'


def test_symlink_ancestor_is_refused_without_writing_target(replay, monkeypatch):
    target = replay.root / 'actual'
    target.mkdir()
    alias = replay.root / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(replay, 'run_signup', lambda: pytest.fail('Model must not run via symlink'))
    with pytest.raises(SystemExit) as failure:
        replay.main(['--output-dir', str(alias / 'new-run')])
    assert failure.value.code == 2
    assert list(target.iterdir()) == []


@pytest.mark.parametrize('replacement', ['file', 'symlink'])
def test_report_created_after_reservation_is_never_overwritten(replay, replacement):
    target = replay.root / 'prior.json'
    target.write_text('keep')
    with replay.fresh_output_directory(replay.root / 'new-run') as (directory, descriptor):
        output = directory / 'synthetic-gloo-signup.json'
        if replacement == 'file':
            output.write_text('keep')
        else:
            output.symlink_to(target)
        with pytest.raises(FileExistsError):
            replay.write_report(descriptor, {'passed': True})
        assert output.read_text() == 'keep'
    assert target.read_text() == 'keep'


def test_failed_replay_gets_its_own_receipt(replay, monkeypatch):
    monkeypatch.setattr(replay, 'run_signup', lambda: {
        'passed': False, 'real_gloo_usage': {}, 'error_type': 'GlooUnavailableError',
        'real_messages_sent': 0,
    })
    output = replay.root / 'new-run'
    assert replay.main(['--output-dir', str(output)]) == 1
    result = json.loads((output / 'synthetic-gloo-signup.json').read_text())
    assert result['passed'] is False and result['real_messages_sent'] == 0


def test_help_does_not_import_backend_or_create_private_store(tmp_path):
    source = Path(__file__).parents[1] / 'tools/check_synthetic_gloo_signup.py'
    import os
    private_store = tmp_path / 'connected.db'
    environment = dict(os.environ, DATABASE_URL='sqlite:///' + str(private_store),
                       SMS_PROVIDER='mac_messages', MAC_BRIDGE_ENABLED='true')
    result = subprocess.run([sys.executable, str(source), '--help'], cwd=tmp_path,
                            env=environment, text=True, capture_output=True)
    assert result.returncode == 0 and '--output-dir' in result.stdout
    assert not private_store.exists()

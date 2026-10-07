"""Hard process exits and restart, with fake HTTP and a fake native sender only."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_mac_messages import config
from tests.test_mac_worker_recovery import item


PROGRAM = r'''
import json, os, sys
from pathlib import Path
import httpx
from app.integrations.mac_messages import MacWorker

root = Path(sys.argv[1])
phase = sys.argv[2]
cfg = json.loads((root / 'fixture.json').read_text())
message = cfg.pop('fixture_message')
class Reader:
    def watermark(self): return 42
    def new_messages(self, after): return []
def server(request):
    with (root / 'requests.jsonl').open('a') as output:
        output.write(json.dumps({'path': request.url.path, 'data': json.loads(request.content)}) + '\n')
    if request.url.path.endswith('/pull'):
        return httpx.Response(200, json={'messages': [] if phase == 'resume' else [message]})
    assert request.url.path.endswith('/ack')
    if phase == 'crash_during_ack': os._exit(92)
    return httpx.Response(200, json={'status': json.loads(request.content)['outcome']})
def sender(phone, body):
    assert phase != 'resume', 'Restart must not invoke even the fake native sender'
    with (root / 'fake-attempts.jsonl').open('a') as output:
        output.write(json.dumps({'phone': phone, 'body': body}) + '\n')
        output.flush(); os.fsync(output.fileno())
    if phase == 'crash_during_native': os._exit(91)
    return 'submitted'
worker = MacWorker(cfg, live=True, reader=Reader(), sender=sender,
    client=httpx.Client(transport=httpx.MockTransport(server)))
worker.once()
'''


@pytest.mark.parametrize('phase,exitcode,original,recovered', [
    ('crash_during_native', 91, 'attempting', 'uncertain'),
    ('crash_during_ack', 92, 'submitted', 'submitted'),
])
def test_hard_exit_restarts_receipt_only_without_repeating_native_attempt(tmp_path, phase, exitcode, original, recovered):
    cfg = {**config(tmp_path), 'fixture_message': item()}
    (tmp_path / 'fixture.json').write_text(json.dumps(cfg))
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    cwd = Path(__file__).resolve().parents[1]
    def run(mode):
        return subprocess.run([sys.executable, '-c', PROGRAM, str(tmp_path), mode],
            cwd=cwd, env=environment, capture_output=True, text=True, timeout=15)
    crashed = run(phase)
    assert crashed.returncode == exitcode, crashed.stderr
    assert json.loads((tmp_path / 'checkpoint.json').read_text())['dispatches']['7']['outcome'] == original
    for _ in range(2):
        result = run('resume')
        assert result.returncode == 0, result.stderr
    assert len((tmp_path / 'fake-attempts.jsonl').read_text().splitlines()) == 1
    state = json.loads((tmp_path / 'checkpoint.json').read_text())
    assert state['dispatches']['7']['outcome'] == recovered
    requests = [json.loads(line) for line in (tmp_path / 'requests.jsonl').read_text().splitlines()]
    acknowledgments = [row['data'] for row in requests if row['path'].endswith('/ack')]
    assert acknowledgments and all(row == {'token': item()['token'], 'outcome': recovered} for row in acknowledgments)
    assert not (tmp_path / 'checkpoint.active').exists()

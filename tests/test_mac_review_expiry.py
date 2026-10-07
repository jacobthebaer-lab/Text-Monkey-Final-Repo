"""Expired saved claims must not stop unrelated native work or retry an attempt."""
from datetime import datetime, timedelta

import httpx
import pytest

from app.integrations import mac_messages as connector
from tests.test_mac_messages import WORKER_NOW
from tests.test_mac_worker_recovery import worker, item


@pytest.mark.parametrize('backend_expired', [True, False])
def test_expired_saved_review_reconciles_without_stalling_batch(tmp_path, monkeypatch, backend_expired):
    now = [WORKER_NOW]
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None): return now[0]
    monkeypatch.setattr(connector, 'datetime', Frozen)
    old = item(confirmation_required=True, content_hash='a'*64,
               approval_expires_at=(WORKER_NOW+timedelta(minutes=1)).isoformat())
    fresh = item(id=8, token='d'*64, body='Another valid synthetic response', conversation_preflight_required=True)
    calls, sent = [], []
    offline = [True]
    def server(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/pull'):
            return httpx.Response(200, json={'messages':[old, fresh] if calls.count('/mac/outbound/pull') == 1 else []})
        if request.url.path.endswith('/7/verify'):
            if offline and offline.pop():
                raise httpx.ReadError('Synthetic lost connection before native attempt')
            if backend_expired:
                return httpx.Response(409, json={'detail':'Exact approval expired'})
            # Even if the backend clock lags, the connector must not send the expired copy.
            return httpx.Response(200, json={'verified':True, 'phone':old['phone'],
                'body':old['body'], 'content_hash':old['content_hash']})
        if request.url.path.endswith('/8/verify'):
            return httpx.Response(200, json={'verified':True, 'phone':fresh['phone'], 'body':fresh['body']})
        if request.url.path.endswith('/7/review-hold'):
            return httpx.Response(200, json={'message_id':7, 'status':'blocked_review_expired', 'native_attempted':False})
        assert request.url.path.endswith('/8/ack')
        return httpx.Response(200, json={'status':'submitted'})
    sender = lambda phone, body: sent.append(body) or 'submitted'
    first = worker(tmp_path, server, sender)
    with pytest.raises(httpx.ReadError): first.once()
    assert first.active_path.exists() and not sent
    now[0] += timedelta(minutes=2)
    resumed = worker(tmp_path, server, sender)
    resumed.once()
    assert resumed.state['dispatches']['7']['outcome'] == 'blocked'
    assert resumed.state['dispatches']['8']['outcome'] == 'submitted'
    assert not resumed.active_path.exists()
    worker(tmp_path, server, sender).once()
    assert sent == [fresh['body']]
    assert calls.count('/mac/outbound/7/verify') == 2
    assert '/mac/outbound/7/ack' not in calls


@pytest.mark.parametrize('prior', ['submitted', 'uncertain', 'attempting'])
def test_expired_review_never_downgrades_a_persisted_attempt(tmp_path, prior):
    from app.integrations.mac_messages import atomic_json
    old = item(confirmation_required=True, content_hash='a'*64,
               approval_expires_at=(WORKER_NOW-timedelta(minutes=1)).isoformat())
    calls = []
    def server(request):
        import json
        calls.append((request.url.path, json.loads(request.content)))
        assert request.url.path == '/mac/outbound/7/ack'
        return httpx.Response(200, json={'status':'submitted' if prior == 'submitted' else 'uncertain'})
    first = worker(tmp_path, server, lambda *_: pytest.fail('Never resend a prior attempt'))
    first.state['dispatches']['7'] = {'token':old['token'], 'outcome':prior}
    first.save(); atomic_json(first.active_path, [old])
    resumed = worker(tmp_path, server, lambda *_: pytest.fail('Never resend a prior attempt'))
    resumed.once()
    assert calls == [('/mac/outbound/7/ack', {'token':old['token'],
        'outcome':'submitted' if prior == 'submitted' else 'uncertain'})]
    assert not resumed.active_path.exists()

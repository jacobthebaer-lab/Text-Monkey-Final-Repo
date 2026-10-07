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

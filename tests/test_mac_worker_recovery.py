"""Connector interruption/recovery uses fake HTTP, Messages readers and senders only."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations import mac_messages as connector
from tests.test_mac_messages import config, ReaderFixture, PHONE, WORKER_NOW
from tests.session_fixtures import session_id


def item(**extra):
    return {'id':7,'token':'c'*64,'phone':PHONE,'session_id':session_id(PHONE),
            'body':'Synthetic private text', **extra}


def worker(tmp_path, server, sender, reader=None):
    return connector.MacWorker(config(tmp_path), live=True,
        client=httpx.Client(transport=httpx.MockTransport(server)),
        reader=reader or ReaderFixture(), sender=sender)


def test_unreadable_ack_recovery_replays_receipt_not_native_attempt(tmp_path):
    calls, sent = [], []
    def server(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/pull'):
            return httpx.Response(200,json={'messages':[item()]})
        if calls.count('/mac/outbound/7/ack') == 1:
            return httpx.Response(200,text='<html>Temporary tunnel response</html>')
        return httpx.Response(200,json={'status':'submitted'})
    send=lambda phone,body: sent.append((phone,body)) or 'submitted'
    first=worker(tmp_path,server,send)
    with pytest.raises(httpx.RemoteProtocolError): first.once()
    report=connector.checkpoint_diagnostic(config(tmp_path),now=WORKER_NOW)
    assert report['receipts_pending_ack']==1
    second=worker(tmp_path,server,send)
    second.once()
    assert sent == [(PHONE,item()['body'])]
    assert calls.count('/mac/outbound/pull') == 1
    assert calls.count('/mac/outbound/7/ack') == 2
    assert not second.active_path.exists()


def test_inbound_response_loss_retains_watermark_and_retries_same_guid(tmp_path):
    receipts, calls = set(), []
    class Reader(ReaderFixture):
        def new_messages(self,after):
            return [] if after>=43 else [{'row_id':43,'guid':'fictional-guid','phone':PHONE,
                'body':'Synthetic message','session_id':session_id(PHONE)}]
    def server(request):
        assert request.url.path=='/mac/inbound'
        body=json.loads(request.content); calls.append(body); receipts.add(body['guid'])
        if len(calls)==1: return httpx.Response(200,text='temporary non-JSON')
        return httpx.Response(200,json={'duplicate':True})
    client=httpx.Client(transport=httpx.MockTransport(server))
    first=connector.MacWorker(config(tmp_path),client=client,reader=Reader())
    with pytest.raises(httpx.RemoteProtocolError): first.once()
    assert json.loads(first.state_path.read_text())['after']==42
    second=connector.MacWorker(config(tmp_path),client=client,reader=Reader())
    second.once()
    assert calls[0]==calls[1] and len(receipts)==1
    assert second.state['after']==43


def test_crash_with_attempting_journal_becomes_uncertain_without_resend(tmp_path):
    calls=[]
    class Crash(BaseException): pass
    def server(request):
        if request.url.path.endswith('/pull'): return httpx.Response(200,json={'messages':[item()]})
        calls.append(json.loads(request.content)); return httpx.Response(200,json={})
    def crashing_sender(phone,body): raise Crash()
    first=worker(tmp_path,server,crashing_sender)
    with pytest.raises(Crash): first.once()
    assert first.state['dispatches']['7']['outcome']=='attempting'
    second=worker(tmp_path,server,lambda *a:pytest.fail('Crash recovery must not send again'))
    second.once()
    assert calls == [{'token':'c'*64,'outcome':'uncertain'}]
    assert second.state['dispatches']['7']['outcome']=='uncertain'


def test_lost_pull_response_is_visible_without_inventing_or_replaying_claim(tmp_path):
    pulls=[]
    def server(request):
        assert request.url.path.endswith('/pull'); pulls.append(request.url.path)
        # The backend may have committed an unavailable claim response.
        if len(pulls)==1: raise httpx.ReadError('synthetic interrupted claim response')
        return httpx.Response(200,json={'messages':[]})
    send=lambda *a:pytest.fail('A lost claim response cannot authorize a native attempt')
    first=worker(tmp_path,server,send)
    with pytest.raises(httpx.ReadError): first.once()
    assert connector.checkpoint_diagnostic(config(tmp_path),now=WORKER_NOW)['claim_response_uncertain']
    recovered=worker(tmp_path,server,send); recovered.once()
    assert connector.checkpoint_diagnostic(config(tmp_path),now=WORKER_NOW)['claim_response_uncertain']
    assert recovered.state['dispatches']=={}


def test_durable_active_response_clears_only_pending_flag_after_crash(tmp_path):
    first=worker(tmp_path,lambda req:httpx.Response(200,json={}),lambda *a:pytest.fail('Already submitted'))
    first.state['dispatches']['7']={'token':'c'*64,'outcome':'submitted'}
    first.state['claim_response_pending']=True; first.save()
    connector.atomic_json(first.active_path,[item()])
    first.once()
    assert 'claim_response_pending' not in first.state
    assert not connector.checkpoint_diagnostic(config(tmp_path),now=WORKER_NOW)['claim_response_uncertain']


def test_pending_unsent_claim_cannot_cross_expiry_and_diagnostic_is_private(tmp_path,monkeypatch):
    calls=[]
    def server(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/pull'):
            return httpx.Response(200,json={'messages':[item(confirmation_required=True,
                content_hash='h'*64,approval_expires_at=(WORKER_NOW+timedelta(minutes=50)).isoformat())]})
        raise httpx.ConnectError('synthetic offline preflight')
    first=worker(tmp_path,server,lambda *a:pytest.fail('Offline preflight cannot send'))
    with pytest.raises(httpx.ConnectError): first.once()
    future=WORKER_NOW+timedelta(hours=2)
    class Future(datetime):
        @classmethod
        def now(cls,tz=None): return future
    monkeypatch.setattr(connector,'datetime',Future)
    recovered=worker(tmp_path,server,lambda *a:pytest.fail('Expired claim cannot send'))
    with pytest.raises(ValueError,match='active'): recovered.once()
    report=connector.checkpoint_diagnostic(config(tmp_path),now=future)
    assert report['session_state']=='expired' and report['unattempted_claims']==1
    assert report['backend_connectivity']==report['messages_connection']=='not_checked'
    rendered=json.dumps(report)
    assert PHONE not in rendered and item()['body'] not in rendered and item()['token'] not in rendered
    assert calls.count('/mac/outbound/pull')==1


def test_main_500_ack_reconnect_resumes_without_duplicate(tmp_path,monkeypatch,capsys):
    cfg=config(tmp_path); path=tmp_path/'config.json'; path.write_text(json.dumps(cfg))
    original=connector.MacWorker
    calls, sent, sleeps=[],[],[]
    def server(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/pull'): return httpx.Response(200,json={'messages':[item()]})
        return httpx.Response(500 if calls.count('/mac/outbound/7/ack')==1 else 200,json={})
    def factory(config,*,live):
        return original(config,live=live,reader=ReaderFixture(),sender=lambda p,b:sent.append((p,b)) or 'submitted',
            client=httpx.Client(transport=httpx.MockTransport(server)))
    class Done(BaseException): pass
    def sleep(delay):
        sleeps.append(delay)
        if len(sleeps)==2: raise Done()
    monkeypatch.setattr(connector,'MacWorker',factory)
    monkeypatch.setattr(connector.time,'sleep',sleep)
    monkeypatch.setattr('sys.argv',['connector','--config',str(path),'--live-delivery'])
    with pytest.raises(Done): connector.main()
    assert len(sent)==1 and calls.count('/mac/outbound/pull')==1
    assert calls.count('/mac/outbound/7/ack')==2 and sleeps==[4,2]
    output=capsys.readouterr().out
    assert 'temporarily unavailable' in output and 'connection restored' in output
    assert item()['body'] not in output and cfg['token'] not in output


@pytest.mark.parametrize('status,expected',[(500,1),(401,2)])
def test_once_failure_exit_status_is_not_success(tmp_path,monkeypatch,status,expected):
    cfg=config(tmp_path); path=tmp_path/'config.json'; path.write_text(json.dumps(cfg))
    original=connector.MacWorker
    monkeypatch.setattr(connector,'MacWorker',lambda config,*,live:original(config,live=live,
        reader=ReaderFixture(),sender=lambda *a:pytest.fail('Failed backend must not send'),
        client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(status,json={})))) )
    monkeypatch.setattr('sys.argv',['connector','--config',str(path),'--live-delivery','--once'])
    assert connector.main()==expected


def test_diagnose_cli_never_constructs_reader_client_sender_or_updates_checkpoint(tmp_path,monkeypatch,capsys):
    cfg=config(tmp_path); path=tmp_path/'config.json'; path.write_text(json.dumps(cfg))
    monkeypatch.setattr(connector,'MacWorker',lambda *a,**k:pytest.fail('Diagnostic must not construct worker'))
    monkeypatch.setattr('sys.argv',['connector','--config',str(path),'--diagnose'])
    assert connector.main()==0
    report=json.loads(capsys.readouterr().out)
    assert report['check']=='local_journal_only' and not report['checkpoint_exists']
    assert not (tmp_path/'checkpoint.json').exists()


def test_diagnostic_reports_checkpoint_route_mismatch_without_connection(tmp_path):
    first=worker(tmp_path,lambda req:pytest.fail('Diagnostic must not contact backend'),
                 lambda *a:pytest.fail('Diagnostic must not send'))
    cfg=config(tmp_path); cfg['receiving_number']='+15555550199'
    before=first.state_path.read_text()
    report=connector.checkpoint_diagnostic(cfg,now=WORKER_NOW)
    assert report['checkpoint_exists'] and not report['checkpoint_matches_config']
    assert first.state_path.read_text()==before


def test_diagnostic_flags_crash_before_claim_response_was_saved(tmp_path):
    first=worker(tmp_path,lambda req:pytest.fail('Diagnostic must not contact backend'),
                 lambda *a:pytest.fail('Diagnostic must not send'))
    first.state['claim_response_pending']=True; first.save()
    report=connector.checkpoint_diagnostic(config(tmp_path),now=WORKER_NOW)
    assert report['claim_response_uncertain'] and report['claimed_items']==0

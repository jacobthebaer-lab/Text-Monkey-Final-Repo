"""Synthetic safety tests. Never Google Voice, accounts, credentials or live SMS."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.integrations.voice_browser import VoiceConfig, VoiceBrowser, BrowserBlocked, Bubble
from app.integrations.voice_journal import Journal
from app.integrations.voice_worker import VoiceWorker, Backend
from app.sms.provider import get_provider
from app.sms.voice_provider import GoogleVoiceProvider
from app.config import Settings

PHONE = "+12025550191"
NOW = datetime.now(timezone.utc)
SID = "a" * 32


def spec():
    return {"account_label": "synthetic-project@example.invalid", "receiving_number": "+12025550190",
            "project_only_account": True, "bindings": {PHONE: {"thread_id": "synthetic-thread",
            "url": (Path(__file__).parent / "fixtures/voice/thread.html").resolve().as_uri()}},
            "test_sessions": {PHONE: {"id": SID, "starts_at": (NOW-timedelta(minutes=1)).isoformat(),
                                    "expires_at": (NOW+timedelta(minutes=30)).isoformat()}},
            "dom": {"account":"[data-account]", "line":"[data-line]", "thread":"[data-thread]",
                    "recipient":"[data-recipient]", "rows":"#messages > div", "body":"[data-body]",
                    "composer":"textarea", "send":"button", "login":"[data-login]", "challenge":"[data-challenge]",
                    "message_id_attr":"data-id", "direction_attr":"data-direction", "timestamp_attr":"data-time"}}


class FakeBackend:
    def __init__(self):
        self.items, self.received, self.acks = [], {}, []
        self.lost_inbound_response = False
        self.changed_proof = False
        self.lost_ack_response = False

    def incoming(self, payload):
        self.received[payload['guid']] = payload
        if self.lost_inbound_response:
            self.lost_inbound_response = False
            raise ConnectionError('synthetic response loss')

    def pull(self):
        items, self.items = self.items, []
        return items

    def verify(self, item):
        return {"verified":True,"phone":item['phone'],"body":"changed" if self.changed_proof else item['body'],"content_hash":item['content_hash']}

    def ack(self,item,outcome):
        self.acks.append((item['id'],outcome))
        if self.lost_ack_response:
            self.lost_ack_response = False
            raise ConnectionError('synthetic ack response loss')


def item():
    return {"id":1,"token":"x"*64,"phone":PHONE,"body":"Synthetic approved reply",
            "session_id":SID,"confirmation_required":True,"content_hash":"b"*64,
            "approval_expires_at":(NOW+timedelta(minutes=20)).isoformat()}


@pytest.fixture
def harness(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.route('http://**/*',lambda r:r.abort())
        page.route('https://**/*',lambda r:r.abort())
        config = VoiceConfig(spec(),fixture=True)
        adapter = VoiceBrowser(page,config)
        # Keep changes in synthetic DOM across polling without navigation reload.
        adapter.sync_thread(PHONE)
        adapter.sync_thread = lambda phone: adapter.read_current(phone)
        journal = Journal(tmp_path/'journal.db',config.identity())
        backend = FakeBackend()
        worker = VoiceWorker(config,adapter,journal,backend,ingress=True,outbound=True,now=lambda:NOW)
        worker.once()  # skip initial history
        yield worker,page,backend,journal
        journal.db.close()
        browser.close()


def incoming(page,identity='new-1',body=None):
    page.evaluate('([i,b]) => addMessage(i,"in",b)',[identity,body or '[TEXTY '+SID+'] JOIN'])


def test_bootstrap_excludes_old_history_and_accepts_natural_bound_replies(harness):
    worker,page,backend,journal = harness
    assert not backend.received
    incoming(page,body='JOIN')
    worker.once()
    assert len(backend.received)==1
    incoming(page,'new-2')
    worker.once(); worker.once()
    assert len(backend.received)==2
    assert all(v['body']=='JOIN' for v in backend.received.values())


def test_inbox_response_loss_retries_same_guid_without_duplicate(harness):
    worker,page,backend,journal=harness
    incoming(page)
    backend.lost_inbound_response=True
    with pytest.raises(ConnectionError):worker.once()
    worker.once()
    assert len(backend.received)==1
    assert not journal.pending_inbox()


def test_one_click_observed_and_deduplicated(harness):
    worker,page,backend,journal=harness
    backend.items=[item()]
    worker.once(); worker.once()
    assert page.evaluate('clicks')==1
    assert journal.operations()[0]['state']=='observed_sent'
    assert backend.acks==[(1,'submitted')]
    backend.items=[item()]
    worker.once()
    assert page.evaluate('clicks')==1


def test_unknown_no_blind_retry_even_after_restart(harness,tmp_path):
    worker,page,backend,journal=harness
    page.evaluate('window.dropSend=true')
    backend.items=[item()]
    worker.once()
    assert journal.operations()[0]['state']=='unknown'
    worker.once()
    assert page.evaluate('clicks')==1
    journal.set_state('1','sending')  # simulate process death at external boundary
    path=tmp_path/'restart.db'
    other=Journal(path,worker.config.identity())
    other.queue_send(item());other.set_state('1','sending',baseline=['old-1']);other.db.close()
    other=Journal(path,worker.config.identity())
    recovered=VoiceWorker(worker.config,worker.browser,other,backend,ingress=True,outbound=True,now=lambda:NOW)
    recovered.once()
    assert other.operations()[0]['state']=='unknown'
    assert page.evaluate('clicks')==1
    other.db.close()


def test_unknown_reconciles_unique_visible_evidence_and_ambiguity_stays_unknown(harness):
    worker,page,backend,journal=harness
    journal.queue_send(item());journal.set_state('1','unknown',baseline=['old-1'])
    page.evaluate('addMessage("proof-1","out","Synthetic approved reply")')
    worker.once()
    assert journal.operations()[0]['observed_id']=='proof-1'
    assert page.evaluate('clicks')==0
    second={**item(),'id':2}
    journal.queue_send(second);journal.set_state('2','unknown',baseline=['old-1'])
    page.evaluate('addMessage("proof-2","out","Synthetic approved reply")')
    worker.once()
    assert journal.operations()[1]['state']=='unknown'
    assert page.evaluate('clicks')==0


def test_ack_response_loss_does_not_click_again(harness):
    worker,page,backend,journal=harness
    backend.items=[item()];backend.lost_ack_response=True
    with pytest.raises(ConnectionError):worker.once()
    worker.once()
    assert page.evaluate('clicks')==1
    assert backend.acks==[(1,'submitted'),(1,'submitted')]


@pytest.mark.parametrize('mutate,status',[
    ('document.querySelector("[data-account]").textContent="wrong"','blocked'),
    ('document.querySelector("[data-line]").textContent="+12025550999"','blocked'),
    ('document.querySelector("[data-recipient]").textContent="+12025550999"','blocked'),
    ('document.querySelector("[data-thread]").textContent="wrong"','blocked'),
    ('document.body.insertAdjacentHTML("beforeend","<div data-login></div>")','login_required'),
    ('document.body.insertAdjacentHTML("beforeend","<div data-challenge></div>")','blocked'),
    ('document.querySelector("textarea").remove()','dom_changed'),
    ('document.querySelector("[data-id]").removeAttribute("data-id")','dom_changed'),
])
def test_identity_auth_and_dom_fail_closed_before_body_or_send(harness,mutate,status):
    worker,page,backend,journal=harness
    backend.items=[item()];page.evaluate(mutate)
    with pytest.raises(BrowserBlocked):worker.once()
    assert worker.health['status']==status
    assert page.evaluate('clicks')==0
    assert not backend.received


def test_stop_suppresses_pending_output_even_on_backend_outage(harness):
    worker,page,backend,journal=harness
    incoming(page,body='STOP');backend.items=[item()];backend.lost_inbound_response=True
    with pytest.raises(ConnectionError):worker.once()
    assert journal.suppressed(PHONE)
    with pytest.raises(BrowserBlocked):worker.once()
    assert page.evaluate('clicks')==0


def test_changed_proof_expiry_recipient_and_session_fail_closed(harness):
    worker,page,backend,journal=harness
    backend.items=[item()];backend.changed_proof=True
    with pytest.raises(BrowserBlocked):worker.once()
    assert journal.operations()[0]['state']=='failed'
    for invalid in ({**item(),'phone':'+12025550999'},{**item(),'session_id':'c'*32},
                    {**item(),'confirmation_required':False},
                    {**item(),'approval_expires_at':(NOW-timedelta(seconds=1)).isoformat()}):
        with pytest.raises(BrowserBlocked):worker.valid_item(invalid)
    assert page.evaluate('clicks')==0


def test_read_only_no_backend_calls_or_claims(harness):
    worker,page,backend,journal=harness
    worker.ingress=False;worker.outbound=False
    incoming(page);backend.items=[item()]
    worker.once()
    assert not backend.received and len(backend.items)==1
    assert page.evaluate('clicks')==0


def test_config_change_and_live_unreviewed_refused(tmp_path):
    config=VoiceConfig(spec(),fixture=True)
    journal=Journal(tmp_path/'journal.db',config.identity());journal.db.close()
    changed=config.identity()|{'account_label':'other'}
    with pytest.raises(ValueError):Journal(tmp_path/'journal.db',changed)
    with pytest.raises(ValueError):VoiceConfig(spec())
    data=spec();data['project_only_account']=False
    with pytest.raises(ValueError):VoiceConfig(data,fixture=True)


def test_provider_opt_in_and_exact_allowlist():
    with pytest.raises(ValueError):get_provider(Settings(sms_provider='google_voice'))
    provider=get_provider(Settings(sms_provider='google_voice',mac_bridge_enabled=True,
        mac_bridge_token='x'*40,mac_demo_phones=PHONE,mac_message_services='SMS',admin_password='x'*20,
        mac_test_sessions=json.dumps(spec()['test_sessions']), competition_confirmation_required=True))
    assert isinstance(provider,GoogleVoiceProvider)
    assert provider.send(PHONE,'Synthetic') .startswith('MAC'+SID+':')
    with pytest.raises(ValueError):provider.send('+12025550999','Outside')


def test_backend_requires_safe_origin():
    for url in ('http://remote.invalid','https://user:pass@example.invalid','https://example.invalid/path','https://example.invalid?key=secret'):
        with pytest.raises(ValueError):Backend(url,'x'*40)


def test_real_backend_gate_review_claim_browser_receipt(harness,tmp_path,monkeypatch):
    """Real app code and headed fixture, with fabricated Gloo and local TestClient."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.main import create_app
    from app.clock import FakeClock
    from app.core import confirmations
    from app.core.send_gate import SendGate
    from app.db import models as m
    from app.web.texty import admin
    from app.llm.parser import ParsedMessage
    worker,page,_,journal=harness
    clock=FakeClock(NOW,timezone='UTC')
    application=create_app(Settings(database_url=f'sqlite:///{tmp_path}/app.db',sms_provider='google_voice',
        demo_mode=False,automation_enabled=False,mac_bridge_enabled=True,mac_bridge_token='x'*40,
        mac_demo_phones=PHONE,mac_message_services='SMS',admin_password='x'*20,
        mac_test_sessions=json.dumps(spec()['test_sessions']),competition_confirmation_required=True))
    application.state.clock=clock
    application.state.mac_delivery_clock=clock
    application.dependency_overrides[admin]=lambda:{'email':'synthetic-admin@example.invalid'}
    monkeypatch.setattr('app.web.mac_messages.parse_inbound',lambda *a:ParsedMessage(intent='question',confidence=1))
    with application.state.session_factory() as session:
        session.info['record_authorized']=True
        session.add(m.Volunteer(name='Synthetic Project Tester',phone=PHONE,sms_opt_in=True,
            status='active',preferences={},created_at=NOW))
        session.commit()
    with TestClient(application) as client:
        backend=Backend('http://localhost','x'*40,client=client)
        backend.check_configuration(worker.config)
        # Mac worker sharing a token cannot operate a selected Voice backend.
        assert client.post('/mac/outbound/pull',json={},headers={'Authorization':'Bearer '+'x'*40}).status_code==409
        worker.backend=backend
        incoming(page,body='What time?')
        worker.once()
        with application.state.session_factory() as session:
            incoming_row=session.scalar(select(m.Message).where(m.Message.direction=='in'))
            assert incoming_row.purpose=='test:'+SID
            assert incoming_row.body=='What time?'
            proposal=session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_text'))
            assert proposal.status=='pending'
            proposal_id,content_hash=proposal.id,proposal.payload['content_hash']
            assert not session.scalar(select(m.Message).where(m.Message.direction=='out'))
        result=client.post(f'/api/proposals/{proposal_id}/approve',json={'content_hash':content_hash})
        assert result.status_code==200,result.text
        worker.once();worker.once()
        assert page.evaluate('clicks')==1
        with application.state.session_factory() as session:
            assert session.scalar(select(m.Message).where(m.Message.direction=='out')).status=='submitted'
        # STOP enters existing opt-out policy without waiting on review.
        incoming(page,'stop-2','STOP');worker.once()
        with application.state.session_factory() as session:
            assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE)).sms_opt_in is False
        assert page.evaluate('clicks')==1


def test_virtualized_old_and_expired_input_never_enters_backend(harness):
    worker,page,backend,journal=harness
    incoming(page,'old-virtualized','Old fixture text')
    page.evaluate('document.querySelector("[data-id=old-virtualized]").dataset.time="2000-01-01T00:00:00+00:00"')
    worker.once()
    assert not backend.received
    worker.now=lambda:NOW+timedelta(hours=1)
    incoming(page,'expired','JOIN');worker.once()
    assert not backend.received
    incoming(page,'stop-after-expiry','STOP')
    worker.once()
    assert next(iter(backend.received.values()))['body']=='STOP'


def test_operation_conflict_and_profile_lock_fail_closed(harness,tmp_path):
    from contextlib import ExitStack
    from app.integrations.voice_worker import lock_file
    worker,page,backend,journal=harness
    journal.queue_send(item())
    with pytest.raises(ValueError):journal.queue_send({**item(),'body':'changed'})
    with ExitStack() as first:
        lock_file(first,tmp_path/'profile.lock')
        with ExitStack() as second:
            with pytest.raises(BlockingIOError):lock_file(second,tmp_path/'profile.lock')


def test_stop_arriving_during_prepare_blocks_click(harness):
    worker,page,backend,journal=harness
    prepare=worker.browser.prepare
    def stopping_prepare(phone,body):
        prepare(phone,body)
        incoming(page,'late-stop','STOP')
    worker.browser.prepare=stopping_prepare
    backend.items=[item()]
    with pytest.raises(BrowserBlocked):worker.once()
    assert journal.suppressed(PHONE)
    assert page.evaluate('clicks')==0


def test_unresolved_send_holds_later_recipient_output(harness):
    worker,page,backend,journal=harness
    journal.queue_send(item());journal.set_state('1','unknown',baseline=['old-1'])
    backend.items=[{**item(),'id':2,'body':'Later output'}]
    with pytest.raises(BrowserBlocked):worker.once()
    assert page.evaluate('clicks')==0
    assert journal.operations()[0]['state']=='unknown'


def test_normal_product_default_remains_mock_and_voice_modes_are_explicit():
    settings=Settings()
    assert settings.sms_provider=='mock'
    assert settings.competition_confirmation_required is False
    assert not settings.mac_bridge_enabled
    from app.sms.mock_provider import MockSMSProvider
    assert isinstance(get_provider(settings),MockSMSProvider)
    provider=get_provider(Settings(sms_provider='google_voice',mac_bridge_enabled=True,
        mac_bridge_token='x'*40,mac_demo_phones=PHONE,mac_message_services='SMS',admin_password='x'*20,
        mac_test_sessions=json.dumps(spec()['test_sessions']),competition_confirmation_required=False))
    assert provider.exact_review is False

"""Bounded real adapter candidate, verified only with synthetic offline services."""
import hashlib
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.core import confirmations
from app.db import models as m
from app.integrations.google_voice_client import GoogleVoiceConnector, ConnectorUnavailable
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim, GoogleVoiceInboundReceipt
from app.integrations.google_voice_runtime import set_paused, dispatch_outbound, tick_google_voice
from app.main import create_app
from app.sms.google_voice_provider import GoogleVoiceProvider
from app.web.texty import admin
from tests.session_fixtures import session_json
from tests.test_google_voice import FakeConnector, ExactGloo, queued, status, incoming, PHONE, EMAIL, NUMBER, TOKEN


class DemoConnector(FakeConnector):
    def __init__(self):
        super().__init__()
        self.scans = 0
        self.baseline = None

    def health(self):
        return {**super().health(), "demo_mode": True, "baseline_at": self.baseline}

    def intake(self, phone=None):
        self.scans += 1
        self.baseline = "synthetic-baseline"
        return self.health()


@pytest.fixture
def demo(tmp_path, clock, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/demo.db", sms_provider="google_voice",
        google_voice_demo_mode=True, demo_mode=False, automation_enabled=False,
        google_voice_enabled=True, google_voice_connector_token=TOKEN, live_sms=True,
        google_voice_expected_email=EMAIL, google_voice_expected_number=NUMBER,
        google_voice_demo_phones=PHONE, google_voice_test_sessions=session_json([PHONE], clock.now()),
        competition_confirmation_required=True, gloo_api_key="synthetic-gloo",
        superadmin_email_allowlist=EMAIL, admin_email_allowlist=EMAIL)
    monkeypatch.setattr("app.main.build_gloo", lambda s: ExactGloo(s))
    application = create_app(settings)
    state = application.state
    state.clock = state.google_voice_clock = state.mac_delivery_clock = clock
    state.google_voice_connector = DemoConnector()
    from app.integrations.google_voice_demo import scope_fingerprint
    original_health = state.google_voice_connector.health
    state.google_voice_connector.health = lambda: {**original_health(), "scope_fingerprint": scope_fingerprint(state.provider.test_sessions)}
    state.google_voice_status = {"connected": True, "checked_monotonic": time.monotonic()}
    with state.session_factory() as session:
        session.info["record_authorized"] = True
        session.add(m.Volunteer(name="Synthetic Tester", phone=PHONE, sms_opt_in=True,
            status="active", preferences={}, created_at=clock.now()))
        session.flush()
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        from app.core.signup_copy import WELCOME
        selected = state.provider.test_sessions[PHONE]
        invitation = m.Message(direction="out",phone=PHONE,body=WELCOME,kind="ai",purpose="signup_reply",
            provider_sid=selected.outbound_prefix+"synthetic-prior-invitation",status="submitted",created_at=clock.now()-timedelta(seconds=30))
        session.add(invitation);session.flush()
        response = m.Message(direction="in",phone=PHONE,volunteer_id=person.id,body=person.name,kind="google_voice_test_in",
            purpose="test:"+selected.id,status="received",created_at=clock.now()-timedelta(seconds=20))
        session.add(response)
        person.preferences={"consent_source":"sms_name_reply_to_exact_invitation","consent_at":response.created_at.isoformat()}
        set_paused(session, False)
        session.commit()
    application.dependency_overrides[admin] = lambda: {"email": EMAIL, "email_confirmed_at": "synthetic"}
    return application


def payload(demo, message_id):
    with demo.state.session_factory() as session:
        body = session.get(m.Message, message_id).body
    return {"message_id": message_id, "body_hash": hashlib.sha256(body.encode()).hexdigest()}


def test_demo_does_not_schedule_poll_send_or_drain_queue(demo, monkeypatch):
    monkeypatch.setattr("apscheduler.schedulers.background.BackgroundScheduler.start",
        lambda *a: pytest.fail("Demo must not start a scheduler"))
    first, second = queued(demo), queued(demo)
    with TestClient(demo) as client:
        report = client.get("/api/cloud-texting").json()
        assert report["demo_mode"] and not report["background_processing"]
        assert report["delivery_verified"] is False
        assert {row["id"] for row in report["reviewed_messages"]} == {first, second}
        tick_google_voice(demo.state)
        dispatch_outbound(demo.state, demo.state.google_voice_connector)
        assert demo.state.google_voice_connector.scans == 0
        assert demo.state.google_voice_connector.calls == []
        response = client.post("/api/cloud-texting/demo/dispatch", json=payload(demo, second))
        assert response.status_code == 200
        assert response.json()["step_result"]["status"] == "submitted"
        assert response.json()["step_result"]["delivery_verified"] is False
        assert status(demo, first) == "queued" and status(demo, second) == "submitted"
        assert len(demo.state.google_voice_connector.calls) == 1
        assert client.post("/api/cloud-texting/demo/dispatch", json=payload(demo, second)).status_code == 409
        assert len(demo.state.google_voice_connector.calls) == 1


@pytest.mark.parametrize("change,expected", [("content", "blocked_confirmation"),
    ("consent", "blocked_confirmation"), ("stale", "blocked_stale"),
    ("expired_session", "blocked_test_session"), ("quiet_hours", "blocked_quiet_hours"),
    ("style", "blocked_style")])
def test_single_step_reuses_all_existing_delivery_guards(demo, monkeypatch, change, expected):
    message_id = queued(demo)
    if change == "stale":
        demo.state.clock.advance(timedelta(minutes=16))
    elif change == "expired_session":
        demo.state.clock.advance(timedelta(hours=3))
    elif change == "quiet_hours":
        monkeypatch.setattr("app.integrations.google_voice_runtime.in_quiet_hours", lambda *a: True)
    else:
        with demo.state.session_factory() as session:
            session.info["record_authorized"] = True
            if change in {"content", "style"}:
                session.get(m.Message, message_id).body = "Changed" if change == "content" else "Forbidden \u2014 text"
            else:
                session.scalar(select(m.Volunteer)).sms_opt_in = False
            session.commit()
    response = TestClient(demo).post("/api/cloud-texting/demo/dispatch", json=payload(demo, message_id))
    assert response.status_code == 200
    assert status(demo, message_id) == expected
    assert demo.state.google_voice_connector.calls == []


def test_exact_hash_and_uncertainty_are_not_retryable(demo):
    message_id = queued(demo)
    client = TestClient(demo)
    wrong = {"message_id": message_id, "body_hash": "0" * 64}
    assert client.post("/api/cloud-texting/demo/dispatch", json=wrong).status_code == 409
    assert demo.state.google_voice_connector.calls == []
    demo.state.google_voice_connector.outcome = ConnectorUnavailable("synthetic timeout")
    exact = payload(demo, message_id)
    assert client.post("/api/cloud-texting/demo/dispatch", json=exact).json()["step_result"]["status"] == "uncertain"
    assert client.post("/api/cloud-texting/demo/dispatch", json=exact).status_code == 409
    with demo.state.session_factory() as session:
        assert session.get(GoogleVoiceDeliveryClaim, message_id)
        from app.core.send_gate import SendGate
        gate=SendGate(session,demo.state.clock,demo.state.provider)
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE))
        fresh=gate.send(body='Different text with a fresh key.',purpose='manual',kind='ai',volunteer=person)
        assert fresh.reason=='Prior uncertain submission requires manual delivery review, never a fresh-key retry'
        assert fresh.approval_id is None
    assert len(demo.state.google_voice_connector.calls) == 1


def test_intake_processes_stop_without_dispatch_and_session_import_never_scans(demo):
    message_id = queued(demo)
    connector = demo.state.google_voice_connector
    connector.messages, connector.cursor = [incoming(demo, "STOP")], 1
    client = TestClient(demo)
    cookies = [{"name": name, "value": "synthetic-private", "domain": ".google.com", "path": "/"}
               for name in ("SID", "HSID", "SSID", "APISID", "SAPISID")]
    assert client.post("/api/cloud-texting/session", json={"cookies": cookies}).json()["paused"]
    assert connector.scans == 0 and connector.calls == []
    first = client.post("/api/cloud-texting/demo/intake", json={})
    assert first.status_code == 200 and first.json()["step_result"]["baseline_established"]
    assert connector.scans == 1 and connector.calls == []
    with demo.state.session_factory() as session:
        assert session.scalar(select(m.Volunteer)).sms_opt_in is False
        assert session.get(GoogleVoiceInboundReceipt, "synthetic-inbound").result["intent"] == "stop"
    assert status(demo, message_id) == "blocked_opt_out"


@pytest.mark.parametrize("user", [{"email": "coordinator@example.test", "email_confirmed_at": "yes"},
    {"email": EMAIL}, {"email": "", "email_confirmed_at": "yes"}])
def test_demo_actions_require_verified_superadmin(demo, user):
    demo.dependency_overrides[admin] = lambda: user
    client = TestClient(demo)
    for path, body in [("intake", {}), ("dispatch", {"message_id": 1, "body_hash": "0" * 64})]:
        assert client.post("/api/cloud-texting/demo/" + path, json=body).status_code == 403
    assert demo.state.google_voice_connector.scans == 0 and demo.state.google_voice_connector.calls == []


@pytest.mark.parametrize("field", ["automation_enabled", "demo_mode", "mac_bridge_enabled",
    "profile_sync_enabled", "pco_staffing_poll_enabled", "pco_staffing_write_enabled"])
def test_demo_rejects_background_or_synthetic_auth_configuration(demo, field):
    with pytest.raises(ValueError, match="background/Mac"):
        GoogleVoiceProvider(replace(demo.state.settings, **{field: True}))


def test_private_http_adapter_is_real_bounded_and_redacts_failures(demo, monkeypatch):
    requests = []
    @contextmanager
    def request(client, method, url, **kwargs):
        requests.append((method, url, kwargs))
        assert client.trust_env is False and client.follow_redirects is False
        assert client.timeout.connect == 5 and client.timeout.read == 90
        yield httpx.Response(200, json={"ready": False}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.Client, "stream", request)
    connector = GoogleVoiceConnector(demo.state.settings)
    assert connector.intake() == {"ready": False}
    assert requests[0][0] == "POST" and requests[0][1].endswith("/demo/intake")
    assert requests[0][2]["headers"]["Authorization"] == "Bearer " + TOKEN
    monkeypatch.setattr(httpx.Client, "stream", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("private-secret")))
    with pytest.raises(ConnectorUnavailable) as error:
        connector.health()
    assert "private-secret" not in str(error.value)


def test_private_http_adapter_stops_stream_before_downloading_excess_content(demo, monkeypatch):
    reads=[]
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(20):
                reads.append(index)
                yield b'x' * 65536
    @contextmanager
    def request(client, method, url, **kwargs):
        yield httpx.Response(200,stream=Oversized(),request=httpx.Request(method,url))
    monkeypatch.setattr(httpx.Client,'stream',request)
    with pytest.raises(ConnectorUnavailable):
        GoogleVoiceConnector(demo.state.settings).health()
    assert len(reads)==9  # first chunk exceeding512KiB aborts; remaining chunks never read.


@pytest.mark.parametrize("change", ["revoked", "pending", "expired"])
def test_status_does_not_offer_invalid_review_as_sendable(demo, change):
    message_id = queued(demo)
    with demo.state.session_factory() as session:
        row = session.get(m.Message, message_id)
        approval = confirmations.proof_for(session, row)
        if change == "expired":
            demo.state.clock.advance(timedelta(hours=3))
        else:
            approval.status = change
        session.commit()
    assert TestClient(demo).get("/api/cloud-texting").json()["reviewed_messages"] == []


@pytest.mark.parametrize("inside", [True, False])
def test_natural_demo_reply_requires_current_session_window(demo, monkeypatch, inside):
    connector = demo.state.google_voice_connector
    item = incoming(demo, "Can I help Sunday?", marker=False)
    if not inside:
        item["received_at"] = (demo.state.provider.test_sessions[PHONE].starts_at - timedelta(seconds=1)).isoformat()
    connector.messages, connector.cursor = [item], 1
    called = []
    from types import SimpleNamespace
    def handle(*args, **kwargs):
        called.append(args[4])
        return SimpleNamespace(routed_to="question")
    monkeypatch.setattr("app.integrations.google_voice_runtime.handle_inbound", handle)
    assert TestClient(demo).post("/api/cloud-texting/demo/intake", json={}).status_code == 200
    assert called == (["Can I help Sunday?"] if inside else [])
    assert connector.calls == []


def test_single_dispatch_requires_fresh_explicit_intake(demo):
    message_id = queued(demo)
    demo.state.google_voice_status["checked_monotonic"] -= 91
    response = TestClient(demo).post("/api/cloud-texting/demo/dispatch", json=payload(demo, message_id))
    assert response.status_code == 409
    assert demo.state.google_voice_connector.calls == []
    assert status(demo, message_id) == "queued"


def test_pause_and_reconnect_cannot_interleave_with_an_inflight_demo_step(demo):
    from app.integrations.google_voice_runtime import _tick_lock
    assert _tick_lock.acquire(blocking=False)
    try:
        client = TestClient(demo)
        assert client.post("/api/cloud-texting/pause", json={"paused": True}).status_code == 409
        assert client.post("/api/cloud-texting/session", json={"cookies": []}).status_code == 409
        assert demo.state.google_voice_connector.imports == []
        with demo.state.session_factory() as session:
            from app.integrations.google_voice_runtime import is_paused
            assert is_paused(session) is False
    finally:
        _tick_lock.release()


def test_new_stop_suppresses_older_held_gloo_work_before_retry(demo, monkeypatch):
    from app.llm.gloo_client import GlooUnavailableError
    connector = demo.state.google_voice_connector
    question = incoming(demo, "Can I help Sunday?", marker=False)
    connector.messages, connector.cursor = [question], 1
    def unavailable(*a, **k):
        raise GlooUnavailableError("synthetic outage")
    monkeypatch.setattr("app.integrations.google_voice_runtime.handle_inbound", unavailable)
    client = TestClient(demo)
    assert client.post("/api/cloud-texting/demo/intake", json={}).status_code == 200
    stop = {**incoming(demo, "STOP", marker=False), "id": "synthetic-stop"}
    connector.messages, connector.cursor = [stop], 2
    monkeypatch.setattr("app.integrations.google_voice_runtime.handle_inbound", lambda *a, **k: pytest.fail("STOP must suppress older held non-control work"))
    assert client.post("/api/cloud-texting/demo/intake", json={}).status_code == 200
    with demo.state.session_factory() as session:
        assert session.get(GoogleVoiceInboundReceipt, question["id"]).result["state"] == "held_opt_out"
        assert session.scalar(select(m.Volunteer)).sms_opt_in is False
    assert connector.calls == []


class RegisteredDemoConnector(DemoConnector):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.sessions = dict(state.provider.test_sessions)
        self.registration_fail_after_commit = False

    def health(self):
        from app.integrations.google_voice_demo import scope_fingerprint
        return {**super().health(), "scope_fingerprint": scope_fingerprint(self.sessions)}

    def register_recipient(self, value):
        from app.sms.google_voice_provider import GoogleVoiceTestSession
        from app.integrations.google_voice_demo import scope_fingerprint
        spec = GoogleVoiceTestSession(value['id'], datetime.fromisoformat(value['starts_at']), datetime.fromisoformat(value['expires_at']))
        self.sessions[value['phone']] = spec
        if self.registration_fail_after_commit:
            self.registration_fail_after_commit = False
            raise ConnectorUnavailable('synthetic failure after scope commit')
        return {'registered':True,'scope_fingerprint':scope_fingerprint(self.sessions)}


@pytest.fixture
def dynamic_demo(demo):
    from app.sms.google_voice_provider import GoogleVoiceProvider
    demo.state.settings = replace(demo.state.settings, google_voice_demo_phones='', google_voice_test_sessions='{}', allow_text_signup=True)
    demo.state.provider = GoogleVoiceProvider(demo.state.settings)
    demo.state.google_voice_connector = RegisteredDemoConnector(demo.state)
    return demo


def register(demo, phone='+12025550155'):
    return TestClient(demo).post('/api/cloud-texting/demo/recipients',json={'phone':phone})


def test_dynamic_registration_waits_for_name_consent_and_recovers_scope_after_partial_commit(dynamic_demo):
    from app.integrations.google_voice_demo import RECIPIENT_KEY, restore_demo_scope
    connector = dynamic_demo.state.google_voice_connector
    phone = '+12025550155'
    connector.registration_fail_after_commit = True
    assert register(dynamic_demo, phone).status_code == 503
    with dynamic_demo.state.session_factory() as session:
        assert session.get(m.Policy,RECIPIENT_KEY+phone).value['state'] == 'pending'
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone==phone)) is None
    assert TestClient(dynamic_demo).get('/api/cloud-texting').json()['connection']['connected'] is False
    assert register(dynamic_demo, phone).status_code == 200
    with dynamic_demo.state.session_factory() as session:
        record=session.get(m.Policy,RECIPIENT_KEY+phone).value
        assert record['state']=='active' and record['consent_state']=='awaiting_name'
        assert record['initiation']['actor']==EMAIL and record['initiation']['phone']==phone
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone==phone)) is None
    dynamic_demo.state.provider = GoogleVoiceProvider(dynamic_demo.state.settings)
    restore_demo_scope(dynamic_demo.state)
    assert phone in dynamic_demo.state.provider.test_sessions
    assert connector.calls==[] and connector.scans==0
    with dynamic_demo.state.session_factory() as session:
        session.add(m.Policy(key='sms_opt_out:'+phone,value={'value':True}));session.commit()
    assert register(dynamic_demo, phone).status_code==409


@pytest.mark.parametrize("first_reply", ["Judge Example", "Judge"])
def test_initial_invitation_requires_gloo_review_submits_once_and_only_name_reply_grants_consent(dynamic_demo, first_reply):
    from app.integrations.google_voice_demo import RECIPIENT_KEY
    from app.core.signup_copy import WELCOME
    from types import SimpleNamespace
    phone='+12025550155'
    assert register(dynamic_demo,phone).status_code==200
    class SignupGloo(ExactGloo):
        def create_response(self,**kwargs):
            data=json.loads(kwargs['input'])
            self.calls.append(kwargs)
            if isinstance(data,list):
                reply=data[-1]['body']
                first,last=('Judge',None) if reply=='Judge' else (None,'Example') if reply=='Example' else ('Judge','Example')
                return SimpleNamespace(output_text=json.dumps({'signup':True,'first_name':first,'last_name':last,'identity_reply':True}),usage=None)
            if 'recovery' in data:
                return SimpleNamespace(output_text=json.dumps({'stage':data['recovery']['stage'],'missing':data['recovery']['missing'],
                    'acknowledgment':'','question':data['approved_message']}),usage=None)
            return SimpleNamespace(output_text=data['approved_message'],usage=None)
    import json
    dynamic_demo.state.gloo=SignupGloo(dynamic_demo.state.settings)
    client=TestClient(dynamic_demo)
    composed=client.post('/api/cloud-texting/demo/compose',json={'phone':phone,'instruction':'Invite them to join.'})
    assert composed.status_code==200,composed.text
    pending=composed.json()['pending_reviews'][0]
    assert pending['body']==WELCOME+' Text STOP to stop.'
    assert client.post('/api/proposals/'+str(pending['id'])+'/approve',json={'content_hash':pending['content_hash']}).status_code==200
    assert client.post('/api/cloud-texting/demo/intake',json={}).status_code==200
    queued_row=client.get('/api/cloud-texting').json()['reviewed_messages'][0]
    assert client.post('/api/cloud-texting/demo/dispatch',json={'message_id':queued_row['id'],'body_hash':queued_row['body_hash']}).json()['step_result']['status']=='submitted'
    assert client.post('/api/cloud-texting/demo/compose',json={'phone':phone,'instruction':'Another invitation'}).status_code==200
    assert len(dynamic_demo.state.google_voice_connector.calls)==1
    dynamic_demo.state.clock.advance(timedelta(seconds=1))
    dynamic_demo.state.google_voice_connector.messages=[{'id':'judge-name','phone':phone,'body':first_reply,'received_at':dynamic_demo.state.clock.now().isoformat()}]
    dynamic_demo.state.google_voice_connector.cursor=1
    response=client.post('/api/cloud-texting/demo/intake',json={})
    assert response.status_code==200,response.text
    if first_reply=='Judge':
        assert any("last name" in row['body'].lower() for row in response.json()['pending_reviews']), response.json()
        assert all('STOP' not in row['body'] for row in response.json()['pending_reviews'])
        with dynamic_demo.state.session_factory() as session:
            assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone==phone)) is None
        dynamic_demo.state.clock.advance(timedelta(seconds=1))
        dynamic_demo.state.google_voice_connector.messages=[{'id':'judge-last-name','phone':phone,'body':'Example','received_at':dynamic_demo.state.clock.now().isoformat()}]
        dynamic_demo.state.google_voice_connector.cursor=2
        response=client.post('/api/cloud-texting/demo/intake',json={})
        assert response.status_code==200,response.text
    with dynamic_demo.state.session_factory() as session:
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==phone))
        assert person and person.sms_opt_in and person.name=='Judge Example'
        record=session.get(m.Policy,RECIPIENT_KEY+phone).value
        assert record['consent_state']=='name_reply_opted_in'
        assert record['consent']['disclosure_message_id']==queued_row['id']
        assert record['consent']['disclosure_body_hash']==queued_row['body_hash']
        from app.core.send_gate import SendGate
        gate=SendGate(session,dynamic_demo.state.clock,dynamic_demo.state.provider)
        repeated=gate.send(body='Your next demo update. Text stop to stop.',purpose='manual',kind='ai',volunteer=person)
        assert repeated.reason=='Demo command notice belongs in the first invitation only'
        assert repeated.approval_id is None
    assert all('Text STOP to stop.' not in row['body'] for row in response.json()['pending_reviews'])


@pytest.mark.parametrize('reason',['expiry','uncertain','pause','restart'])
def test_temporary_window_is_bounded_and_stops_without_replay(demo,monkeypatch,reason):
    from app.integrations.google_voice_demo_window import start_window,tick_demo_window,window_status,WINDOW_KEY
    from app.integrations.google_voice_demo import scope_fingerprint
    class Timer:
        def __init__(self):self.jobs=[]
        def add_job(self,*args,**kwargs):self.jobs.append((args,kwargs))
        def start(self):pass
        def shutdown(self,wait=False):pass
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler',Timer)
    # No timer starts at application creation. It requires an explicit action.
    first,second=queued(demo),queued(demo)
    start_window(demo.state,EMAIL,1,5)
    assert window_status(demo.state)['active']
    if reason=='expiry':demo.state.clock.advance(timedelta(minutes=2))
    elif reason=='uncertain':demo.state.google_voice_connector.outcome='uncertain'
    elif reason=='pause':
        assert TestClient(demo).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    else:demo.state.google_voice_demo_window_id=None
    tick_demo_window(demo.state)
    assert not window_status(demo.state)['active']
    if reason=='uncertain':
        assert status(demo,first)=='uncertain' and status(demo,second)=='queued'
        assert len(demo.state.google_voice_connector.calls)==1
        with demo.state.session_factory() as session:
            assert session.get(m.Policy,WINDOW_KEY).value['reserved_submissions']==1
    else:assert demo.state.google_voice_connector.calls==[]
    tick_demo_window(demo.state)
    assert len(demo.state.google_voice_connector.calls)==(1 if reason=='uncertain' else 0)


def test_window_consumes_durable_operator_budget_before_single_submission(demo,monkeypatch):
    from app.integrations.google_voice_demo_window import start_window,tick_demo_window,window_status,WINDOW_KEY
    class Timer:
        def add_job(self,*a,**k):pass
        def start(self):pass
        def shutdown(self,wait=False):pass
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler',Timer)
    first,second=queued(demo),queued(demo)
    start_window(demo.state,EMAIL,15,1)
    original=demo.state.google_voice_connector.prepare
    def prepare(**kwargs):
        with demo.state.session_factory() as session:
            assert session.get(m.Policy,WINDOW_KEY).value['reserved_submissions']==1
        return original(**kwargs)
    demo.state.google_voice_connector.prepare=prepare
    tick_demo_window(demo.state)
    assert status(demo,first)=='submitted' and status(demo,second)=='queued'
    assert window_status(demo.state)['reserved_submissions']==1
    assert not window_status(demo.state)['active']
    tick_demo_window(demo.state)
    assert len(demo.state.google_voice_connector.calls)==1


def test_expired_participant_renewal_preserves_unseen_stop_and_never_resets_scope(dynamic_demo):
    from app.integrations.google_voice_demo import RECIPIENT_KEY
    phone='+12025550155'
    assert register(dynamic_demo,phone).status_code==200
    connector=dynamic_demo.state.google_voice_connector
    dynamic_demo.state.clock.advance(timedelta(hours=3))
    connector.messages=[{'id':'expired-window-stop','phone':phone,'body':'STOP','received_at':dynamic_demo.state.clock.now().isoformat()}]
    connector.cursor=1
    with dynamic_demo.state.session_factory() as session:
        original=session.get(m.Policy,RECIPIENT_KEY+phone).value['session']
    response=register(dynamic_demo,phone)
    assert response.status_code==409
    with dynamic_demo.state.session_factory() as session:
        assert session.get(m.Policy,'sms_opt_out:'+phone).value['value']
        assert session.get(m.Policy,RECIPIENT_KEY+phone).value['session']==original
    assert connector.calls==[] and connector.scans==1

"""Privacy checks use a fabricated Messages database and mocked transport only."""
import json
import sqlite3
from datetime import timedelta, datetime, timezone
from types import SimpleNamespace
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.integrations.mac_messages import MacWorker, TestSessionMessagesReader as SessionReader
from app.integrations.test_sessions import parse_sessions
from tests.session_fixtures import session_id, session_specs
from tests.test_mac_messages import mac_app, post, incoming, PHONE, TOKEN, HEADERS, ReaderFixture, config, setup_invitation_app


def synthetic_database(path, prefix):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE message(guid TEXT, handle_id INTEGER, text TEXT, attributedBody BLOB, is_from_me INTEGER, service TEXT, destination_caller_id TEXT);
            CREATE TABLE handle(id TEXT);
            CREATE TABLE chat(service_name TEXT, guid TEXT, last_addressed_handle TEXT);
            CREATE TABLE chat_message_join(message_id INTEGER, chat_id INTEGER);
            CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
            INSERT INTO handle VALUES ('+15555550101'), ('+15555550999');
            INSERT INTO chat VALUES ('iMessage','selected','+15555550200'), ('iMessage','other-line','+15555550300'), ('iMessage','unrelated','+15555550200'), ('iMessage','group','+15555550200');
            INSERT INTO chat_handle_join VALUES (1,1),(2,1),(3,2),(4,1),(4,2);
        """)
        rows = [
            ("old-marked",1,prefix+"old",0,"+15555550200",1),
            ("app-test",1,prefix+"Am I booked for anything now?",0,"+15555550200",1),
            ("personal-same-line",1,"Synthetic personal conversation",0,"+15555550200",1),
            ("personal-outbound",1,prefix+"Synthetic outgoing",1,"+15555550200",1),
            ("other-line",1,prefix+"Synthetic wrong line",0,"+15555550300",2),
            ("other-person",2,prefix+"Synthetic wrong sender",0,"+15555550200",3),
            ("group",1,prefix+"Synthetic group",0,"+15555550200",4),
            ("bare-stop",1," STOP ",0,"+15555550200",1),
            ("wrong-session",1,"[TEXTY "+"0"*32+"] Synthetic wrong session",0,"+15555550200",1),
            ("attributed-only",1,None,0,"+15555550200",1),
        ]
        for guid, handle, body, outgoing, line, chat in rows:
            row = db.execute("INSERT INTO message VALUES (?,?,?,X'414243',?,'iMessage',?)", (guid,handle,body,outgoing,line))
            db.execute("INSERT INTO chat_message_join VALUES (?,?)", (row.lastrowid,chat))


def test_strict_reader_filters_same_line_personal_content_before_fetching(clock, tmp_path):
    specs = session_specs([PHONE], clock.now())
    prefix = f"[TEXTY {session_id(PHONE)}] "
    path = tmp_path / "synthetic.db"
    synthetic_database(path, prefix)
    reader = SessionReader(path, {PHONE}, tmp_path / "unused", "+15555550200", ["iMessage"], specs, now=clock.now)
    # Refuse even selecting attributed blobs: ambiguous content stays unread.
    reader.connection.set_authorizer(lambda action, table, column, *args:
        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and column == "attributedBody" else sqlite3.SQLITE_OK)
    try:
        rows = reader.new_messages(1)
        assert [r["guid"] for r in rows] == ["app-test", "bare-stop"]
        assert rows[0]["body"] == "Am I booked for anything now?"
        assert rows[0]["session_id"] == session_id(PHONE)
        clock.advance(timedelta(hours=2))
        assert [r["guid"] for r in reader.new_messages(1)] == ["bare-stop"]
    finally:
        reader.connection.close()


@pytest.mark.parametrize("missing", ["session", "receiving_line"])
def test_missing_isolation_fails_before_any_database_is_opened(clock, monkeypatch, missing):
    calls = []
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="explicit test session"):
        SessionReader("/unread/private/database", {PHONE}, "/unused",
            None if missing == "receiving_line" else "+15555550200", ["iMessage"],
            {} if missing == "session" else session_specs([PHONE], clock.now()))
    assert calls == []


@pytest.mark.parametrize("fault", ["missing", "wrong", "other_phone", "expired"])
def test_backend_rejects_unproven_ingress_without_persisting_content(mac_app, fault):
    data = incoming("privacy-"+fault, "Synthetic excluded text")
    if fault == "missing":
        data.pop("session_id")
    elif fault == "wrong":
        data["session_id"] = "0"*32
    elif fault == "other_phone":
        data["phone"] = "+15555550999"
    else:
        mac_app.state.clock.advance(timedelta(hours=2))
    with TestClient(mac_app) as client:
        assert post(client, "/mac/inbound", data).status_code == 403
    with mac_app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None


def test_stop_is_honored_after_expiry_but_start_requires_new_active_session(mac_app):
    mac_app.state.clock.advance(timedelta(hours=2))
    with TestClient(mac_app) as client:
        assert post(client, "/mac/inbound", incoming("expired-stop", "STOP")).json()["intent"] == "stop"
        assert post(client, "/mac/inbound", incoming("expired-start", "START")).status_code == 403
        assert post(client, "/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        assert not session.scalar(select(m.Volunteer)).sms_opt_in


def test_authenticated_history_and_dashboard_exclude_legacy_and_other_session_messages(mac_app):
    from app.web.texty import admin
    calls = []
    def compose(**kwargs):
        facts = json.loads(kwargs['input'])
        calls.append(facts)
        assert facts['schedule_context']['schedule']['assignments'] == []
        assert facts['approved_message'] == "Hi Synthetic! You're not booked for any shifts right now."
        return SimpleNamespace(output_text=facts['approved_message'])
    mac_app.state.gloo = SimpleNamespace(settings=mac_app.state.settings, create_response=compose)
    with mac_app.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer))
        for purpose, body, phone in [(None,"synthetic legacy private",PHONE),
            ("test:"+"0"*32,"synthetic old test",PHONE),
            ("test:"+session_id(PHONE),"synthetic unrelated phone","+15555550999")]:
            session.add(m.Message(volunteer_id=person.id, phone=phone, direction="in", kind="inbound",
                purpose=purpose, body=body, status="received", created_at=mac_app.state.clock.now()))
        session.commit()
    path = f"/mac/test-history?phone={PHONE.replace('+','%2B')}&session_id={session_id(PHONE)}"
    with TestClient(mac_app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path.replace(session_id(PHONE),"0"*32), headers=HEADERS).status_code == 403
        assert post(client,"/mac/inbound",incoming("owned-question","Am I booked for anything now?")).status_code == 200
        history = client.get(path, headers=HEADERS).json()["messages"]
        assert len(history) == 2
        assert len(calls) == 1
        assert [r['direction'] for r in history] == ['in', 'out']
        assert not any("legacy" in r["body"] or "unrelated" in r["body"] or "old test" in r["body"] for r in history)
        mac_app.dependency_overrides[admin] = lambda: {"email":"coordinator@example.test"}
        assert [r["body"] for r in client.get("/api/state").json()["messages"]] == [r["body"] for r in history]
    mac_app.dependency_overrides.clear()


def test_gloo_context_excludes_legacy_history_even_for_same_sender(session, clock, make_volunteer):
    person = make_volunteer("Alpha Synthetic")
    selected = parse_sessions(session_specs([person.phone], clock.now()), {person.phone})[person.phone]
    session.info["mac_test_session"] = selected
    for purpose, body in [(None,"synthetic legacy personal"),("test:"+selected.id,"synthetic owned input")]:
        session.add(m.Message(volunteer_id=person.id, phone=person.phone, body=body, direction="in", kind="inbound",
            purpose=purpose, status="received", created_at=clock.now()))
    session.flush()
    calls = []
    def response(**kwargs):
        calls.append(json.loads(kwargs["input"]))
        return SimpleNamespace(output_text="Your current approved facts.")
    model = SimpleNamespace(settings=Settings(gloo_signup_replies=True), create_response=response)
    compose_signup_reply(session, clock, model, "Your current approved facts.", volunteer=person)
    assert [r["body"] for r in calls[0]["recent_messages"]] == ["synthetic owned input"]


@pytest.mark.parametrize("fault", ["missing", "wrong", "expired"])
def test_worker_never_calls_native_sender_without_active_session_proof(tmp_path, fault):
    spec = config(tmp_path)
    now = datetime.now(timezone.utc)
    if fault == "expired":
        spec["test_sessions"] = session_specs([PHONE], now-timedelta(hours=2))
    item = {"id": 1, "phone": PHONE, "body": "Synthetic outgoing", "token": "a"*64,
            "session_id": session_id(PHONE)}
    if fault == "missing":
        item.pop("session_id")
    elif fault == "wrong":
        item["session_id"] = "0"*32
    sent = []
    transport = httpx.MockTransport(lambda _: httpx.Response(200,json={"messages":[item]}))
    worker = MacWorker(spec, live=True, reader=ReaderFixture(), client=httpx.Client(transport=transport),
                       sender=lambda *args: sent.append(args))
    try:
        with pytest.raises(ValueError, match="test-session proof"):
            worker.once()
        assert sent == []
    finally:
        worker.client.close()


def test_backend_cannot_claim_old_unmarked_queued_messages(mac_app):
    with mac_app.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer))
        row = m.Message(phone=PHONE, volunteer_id=person.id, direction="out", purpose="signup_reply",
            kind="template", body="Synthetic old queue", provider_sid="MAClegacy", status="queued",
            created_at=mac_app.state.clock.now())
        session.add(row)
        session.commit()
        message_id = row.id
    with TestClient(mac_app) as client:
        assert post(client,"/mac/outbound/pull").json()["messages"] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message,message_id).status == "blocked_test_session"


def test_session_rotation_requires_fresh_checkpoint_and_ids_are_per_phone(tmp_path):
    spec = config(tmp_path)
    worker = MacWorker(spec, reader=ReaderFixture())
    worker.client.close()
    rotated = json.loads(json.dumps(spec))
    rotated["test_sessions"][PHONE]["id"] = "f"*32
    with pytest.raises(ValueError, match="fresh checkpoint"):
        MacWorker(rotated, reader=ReaderFixture())
    duplicate = {PHONE: spec["test_sessions"][PHONE], "+15555550999": spec["test_sessions"][PHONE]}
    with pytest.raises(ValueError, match="own session ID"):
        parse_sessions(duplicate, set(duplicate))


def test_admin_setup_scopes_model_history_without_an_inbound_session(mac_app):
    volunteer_id = setup_invitation_app(mac_app)
    selected = mac_app.state.provider.test_sessions[PHONE]
    calls = []
    def response(**kwargs):
        facts = json.loads(kwargs['input']); calls.append(facts)
        return SimpleNamespace(output_text=facts['approved_message'])
    mac_app.state.gloo = SimpleNamespace(settings=mac_app.state.settings, create_response=response)
    with mac_app.state.session_factory() as session:
        for purpose, body, created in [
            (None, 'synthetic unmarked same-phone conversation', mac_app.state.clock.now()),
            ('test:'+selected.id, 'synthetic earlier activation', selected.starts_at-timedelta(minutes=1)),
            ('test:'+selected.id, 'synthetic current test input', mac_app.state.clock.now()),
        ]:
            session.add(m.Message(volunteer_id=volunteer_id, phone=PHONE, direction='in', kind='inbound',
                purpose=purpose, body=body, status='received', created_at=created))
        session.commit()
    with TestClient(mac_app) as client:
        assert client.post(f'/api/volunteers/{volunteer_id}/text-setup').status_code == 200
        history = client.get('/api/state').json()['messages']
        assert not any('unmarked' in row['body'] or 'earlier activation' in row['body'] for row in history)
    assert [row['body'] for row in calls[0]['recent_messages']] == ['synthetic current test input']
    mac_app.dependency_overrides.clear()


@pytest.mark.parametrize('fault', ['missing', 'expired'])
def test_admin_setup_rejects_absent_active_session_before_model_or_stage_change(mac_app, fault):
    volunteer_id = setup_invitation_app(mac_app)
    calls = []
    mac_app.state.gloo = SimpleNamespace(settings=mac_app.state.settings,
        create_response=lambda **kwargs: calls.append(kwargs))
    if fault == 'missing':
        mac_app.state.provider.test_sessions.clear()
    else:
        mac_app.state.clock.advance(timedelta(hours=2))
    with TestClient(mac_app) as client:
        assert client.post(f'/api/volunteers/{volunteer_id}/text-setup').status_code == 409
    with mac_app.state.session_factory() as session:
        assert not session.get(m.Volunteer, volunteer_id).preferences.get('onboarding_stage')
        assert session.scalar(select(m.Message)) is None
    assert calls == []
    mac_app.dependency_overrides.clear()


@pytest.mark.parametrize('fault', ['missing', 'expired'])
def test_mac_reply_writer_requires_recipient_session_even_without_transaction_binding(
    session, clock, make_volunteer, fault
):
    from app.llm.gloo_client import GlooUnavailableError
    person = make_volunteer('Synthetic scoped recipient')
    specs = {} if fault == 'missing' else session_specs([person.phone], clock.now()-timedelta(hours=2))
    calls = []
    model = SimpleNamespace(settings=Settings(sms_provider='mac_messages', mac_bridge_enabled=True,
        mac_bridge_token='synthetic-privacy-token-' + 'x' * 32,
        admin_password='synthetic-privacy-admin-password',
        mac_demo_phones=person.phone, mac_test_sessions=json.dumps(specs), gloo_signup_replies=True),
        create_response=lambda **kwargs: calls.append(kwargs))
    with pytest.raises(GlooUnavailableError, match='active recipient test session'):
        compose_signup_reply(session, clock, model, 'Synthetic approved facts.', volunteer=person)
    assert calls == []
    assert session.scalar(select(m.AgentRun)) is None


def test_backend_cannot_claim_queue_content_from_before_the_active_window(mac_app):
    selected = mac_app.state.provider.test_sessions[PHONE]
    with mac_app.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer))
        row = m.Message(phone=PHONE, volunteer_id=person.id, direction='out', purpose='signup_reply',
            kind='template', body='Synthetic earlier-window queue', provider_sid=selected.outbound_prefix+'previous',
            status='queued', created_at=selected.starts_at-timedelta(seconds=1))
        session.add(row); session.commit(); message_id=row.id
    with TestClient(mac_app) as client:
        assert post(client, '/mac/outbound/pull').json()['messages'] == []
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message, message_id).status == 'blocked_test_session'


@pytest.mark.parametrize('fault, code, status', [
    ('outside_phone', 'outside_approved_scope', 403),
    ('missing', 'session_missing', 409),
    ('expired', 'session_inactive', 409),
    ('future', 'session_inactive', 409),
    ('opt_out', 'consent_required', 409),
    ('inactive', 'consent_required', 409),
    ('interests', 'setup_in_progress', 409),
    ('availability', 'setup_in_progress', 409),
    ('disabled_setting', 'setup_disabled', 503),
    ('disabled_policy', 'setup_disabled', 503),
    ('paused', 'connection_paused', 503),
    ('google_voice', 'provider_policy_hold', 503),
])
def test_welcome_roster_and_action_share_exact_nonmutating_block(mac_app, fault, code, status):
    from dataclasses import replace
    from app.integrations.test_sessions import TestSession
    from app.sms.mock_provider import MockSMSProvider
    volunteer_id = setup_invitation_app(mac_app)
    calls = []
    mac_app.state.gloo = SimpleNamespace(settings=mac_app.state.settings,
        create_response=lambda **kwargs: calls.append(kwargs))
    with mac_app.state.session_factory() as session:
        person = session.get(m.Volunteer, volunteer_id)
        if fault == 'outside_phone':
            person.phone = '+15555550999'
        elif fault == 'opt_out':
            person.sms_opt_in = False
        elif fault == 'inactive':
            person.status = 'inactive'
        elif fault in {'interests', 'availability'}:
            person.preferences = {'onboarding_stage': fault}
        if fault == 'disabled_policy':
            session.get(m.Policy, 'full_text_onboarding').value = {'value': False}
        session.commit()
        before = dict(person.preferences)
    if fault == 'missing':
        mac_app.state.provider.test_sessions.clear()
    elif fault in {'expired', 'future'}:
        now = mac_app.state.mac_delivery_clock.now()
        start = now - timedelta(hours=2) if fault == 'expired' else now + timedelta(minutes=1)
        mac_app.state.provider.test_sessions[PHONE] = TestSession(session_id(PHONE), start, start+timedelta(minutes=59))
    elif fault == 'disabled_setting':
        mac_app.state.settings = replace(mac_app.state.settings, gloo_signup_replies=False)
    elif fault == 'paused':
        mac_app.state.provider = MockSMSProvider()
    elif fault == 'google_voice':
        mac_app.state.provider = SimpleNamespace(transport_name='google_voice', test_sessions={})
    try:
        with TestClient(mac_app) as client:
            person = client.get('/api/state').json()['volunteers'][0]
            assert not person['can_start_text_setup']
            assert person['text_setup_block_code'] == code
            assert '\u2014' not in person['text_setup_block_reason']
            result = client.post(f'/api/volunteers/{volunteer_id}/text-setup')
            assert result.status_code == status
            assert result.json()['detail'] == person['text_setup_block_reason']
        with mac_app.state.session_factory() as session:
            assert session.get(m.Volunteer, volunteer_id).preferences == before
            for model in (m.Message, m.Approval, m.AgentRun, m.Assignment, m.Qualification):
                assert session.scalar(select(model)) is None
        assert calls == []
    finally:
        mac_app.dependency_overrides.clear()


@pytest.mark.parametrize('ongoing', [False, True])
def test_welcome_stays_available_in_approved_active_session_on_get_and_profile_update(mac_app, ongoing):
    from app.integrations.test_sessions import TestSession
    volunteer_id = setup_invitation_app(mac_app)
    now = mac_app.state.mac_delivery_clock.now()
    if ongoing:
        mac_app.state.provider.test_sessions[PHONE] = TestSession(session_id(PHONE), now-timedelta(hours=3), None,
            original_expires_at=now-timedelta(hours=2), ongoing_since=now-timedelta(hours=1))
    # Business/demo time must not determine whether an authorized delivery session is active.
    mac_app.state.clock = SimpleNamespace(now=lambda: now+timedelta(days=10))
    try:
        with TestClient(mac_app) as client:
            person = client.get('/api/state').json()['volunteers'][0]
            assert person['can_start_text_setup']
            assert person['text_setup_block_code'] is None
            assert person['text_setup_block_reason'] is None
            result = client.post(f'/api/volunteers/{volunteer_id}', json={
                'first_name': 'Synthetic', 'last_name': 'Tester', 'phone': PHONE,
                'consent': True, 'status': 'active', 'ministry': 'Welcome',
            })
            assert result.status_code == 200
            assert result.json()['can_start_text_setup']
            assert result.json()['text_setup_block_reason'] is None
            new_phone = '+15555550999'
            mac_app.state.provider.phones = mac_app.state.provider.phones | {new_phone}
            mac_app.state.provider.test_sessions[new_phone] = TestSession(session_id(new_phone), now-timedelta(minutes=1), now+timedelta(minutes=59))
            created = client.post('/api/volunteers', json={
                'first_name': 'Synthetic', 'last_name': 'New Person', 'phone': new_phone,
                'consent': True, 'ministry': 'Welcome',
            })
            assert created.status_code == 200
            assert created.json()['can_start_text_setup']
            assert created.json()['text_setup_block_reason'] is None
        with mac_app.state.session_factory() as session:
            assert not session.get(m.Volunteer, volunteer_id).preferences.get('onboarding_stage')
            assert session.scalar(select(m.Message)) is None
    finally:
        mac_app.dependency_overrides.clear()

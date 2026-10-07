from types import SimpleNamespace
import json
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.main import create_app
from app.db import models as m
from app.core.signup import request_signup, approve_signup
from app.web.texty import admin, check_user
import pytest


class SignupGloo:
    settings = Settings()

    def __init__(self, data):
        self.data = data
        self.calls = 0

    def create_response(self, **kwargs):
        self.calls += 1
        facts = json.loads(kwargs['input'])
        if isinstance(facts, dict) and 'approved_message' in facts:
            return SimpleNamespace(output_text=facts['approved_message'])
        return SimpleNamespace(output_text=json.dumps(self.data))


def test_signup_by_text_waits_for_consent_and_grants_no_privileges(session, clock):
    gloo = SignupGloo(
        {
            "signup": True,
            "first_name": "Alex",
            "last_name": "Morgan",
            "sensitive": False,
        }
    )
    assert (
        request_signup(session, clock, gloo, "+15555550199", "Join Alex Morgan")
        == "signup_consent_pending"
    )
    assert session.scalar(select(m.Approval)) is None
    v = session.scalar(select(m.Volunteer))
    assert v.name == "Alex Morgan"
    assert not v.is_coordinator and not v.is_pastor and not v.sms_opt_in
    assert v.qualifications == [] and v.status == "inactive"


def test_duplicate_signup_does_not_create_second_request(session, clock):
    gloo = SignupGloo({"signup": True, "first_name": "Alex", "last_name": "Morgan"})
    for _ in range(2):
        request_signup(session, clock, gloo, "+15555550199", "Join Alex Morgan")
    assert len(session.scalars(select(m.Volunteer)).all()) == 1
    assert gloo.calls == 1


def test_unknown_sensitive_sender_goes_to_human_without_signup(session, clock):
    gloo = SignupGloo(
        {
            "signup": True,
            "first_name": "Alex",
            "last_name": "Morgan",
            "sensitive": False,
        }
    )
    assert (
        request_signup(
            session,
            clock,
            gloo,
            "+15555550199",
            "Join Alex Morgan. My dad is in hospital.",
        )
        == "escalated_sensitive"
    )
    assert session.scalar(select(m.Approval)) is None
    assert session.scalar(select(m.Escalation)).category == "sensitive"
    assert session.scalar(select(m.Message)) is None


def test_signup_control_words_never_call_ai(session, clock):
    gloo = SignupGloo({})
    assert request_signup(session, clock, gloo, "+15555550199", "STOP") is None
    assert gloo.calls == 0


def test_signup_incomplete_name_is_not_invented(session, clock):
    gloo = SignupGloo({"signup": True, "first_name": "Alex", "last_name": ""})
    assert (
        request_signup(session, clock, gloo, "+15555550199", "Join Alex")
        == "signup_name_needed"
    )
    assert session.scalar(select(m.Approval)) is None


def test_unverified_or_unlisted_users_cannot_be_admins():
    settings = Settings(admin_email_allowlist="coordinator@example.test")
    for user in (
        {"email": "coordinator@example.test"},
        {"email": "outsider@example.test", "email_confirmed_at": "2026-10-01"},
    ):
        with pytest.raises(Exception) as error:
            check_user(user, settings)
        assert error.value.status_code == 403


def test_texty_login_fails_closed_without_supabase():
    app = create_app(Settings(database_url="sqlite://"))
    with TestClient(app) as client:
        assert client.get("/texty").status_code == 200
        assert client.get("/api/config").json()["provider"] == "gloo"
        assert client.get("/api/state").status_code == 503
        assert (
            client.post(
                "/api/login", json={"email": "someone@example.test", "password": "test"}
            ).status_code
            == 503
        )


def test_live_classifier_stops_on_failed_preflight(monkeypatch, capsys):
    from app.llm import classify_samples
    from app.llm.gloo_client import GlooUnavailableError

    class FailedGloo:
        def __init__(self, settings):
            pass

        def create_response(self, **kwargs):
            raise GlooUnavailableError("test authentication failure")

    monkeypatch.setattr(
        classify_samples,
        "get_settings",
        lambda: Settings(gloo_api_key="synthetic-test-only"),
    )
    monkeypatch.setattr(classify_samples, "GlooClient", FailedGloo)
    assert classify_samples.main() == 1
    assert "preflight failed" in capsys.readouterr().out


def test_texty_simulator_uses_mock_even_if_app_provider_is_live():
    app = create_app(Settings(database_url="sqlite://", allow_text_signup=True))
    app.dependency_overrides[admin] = lambda: {"email": "coordinator@example.test"}
    app.state.gloo = SignupGloo(
        {"signup": True, "first_name": "Alex", "last_name": "Morgan"}
    )

    class ForbiddenLiveProvider:
        def send(self, *args, **kwargs):
            raise AssertionError("Simulator called live SMS")

    app.state.provider = ForbiddenLiveProvider()
    with TestClient(app) as client:
        r = client.post(
            "/api/simulate", json={"phone": "+15555550199", "body": "Join Alex Morgan"}
        )
        assert (
            r.status_code == 200
            and r.json()["decision"]["intent"] == "signup_consent_pending"
        )
        state = client.get("/api/state").json()
        assert state["proposals"] == [] and len(state["volunteers"]) == 1
        assert (
            client.post(
                "/api/simulate", json={"phone": "+15555550199", "body": "YES"}
            ).json()["decision"]["intent"]
            == "signup_complete"
        )
        volunteer = client.get("/api/state").json()["volunteers"][0]
        assert volunteer["consent"] and not volunteer["qualified"]


def test_text_signup_consent_stop_and_duplicate(session, clock, provider):
    from app.agents.fill_agent import FillContext
    from app.core.inbound import handle_inbound

    gloo = SignupGloo({"signup": True, "first_name": "Alex", "last_name": "Morgan"})
    ctx = FillContext(session, clock, provider, gloo)
    parser = lambda text: (_ for _ in ()).throw(
        AssertionError("Signup should bypass scheduling parser")
    )

    def text(body):
        return handle_inbound(
            session,
            clock,
            provider,
            "+15555550199",
            body,
            parser,
            ctx=ctx,
            allow_signup=True,
        )

    assert text("JOIN Alex Morgan").routed_to == "signup_consent_pending"
    v = session.scalar(select(m.Volunteer))
    assert not v.sms_opt_in and v.status == "inactive"
    assert "Reply YES" in provider.sent[-1].body
    assert text("YES").routed_to == "signup_complete"
    assert v.sms_opt_in and v.status == "active" and not v.is_coordinator
    assert not v.qualifications
    assert text("STOP").routed_to == "stop"
    assert not v.sms_opt_in
    before = len(provider.sent)
    assert text("STOP").routed_to == "stop"
    assert len(provider.sent) == before
    assert len(session.scalars(select(m.Volunteer)).all()) == 1


def test_signup_name_then_consent_entirely_by_text(session, clock, provider):
    from app.agents.fill_agent import FillContext
    from app.core.inbound import handle_inbound

    gloo = SignupGloo({"signup": True, "first_name": "", "last_name": ""})
    ctx = FillContext(session, clock, provider, gloo)

    def text(body):
        return handle_inbound(
            session,
            clock,
            provider,
            "+15555550199",
            body,
            None,
            ctx=ctx,
            allow_signup=True,
        )

    assert text("JOIN").routed_to == "signup_name_needed"
    assert session.scalar(select(m.Volunteer)) is None
    gloo.data = {"signup": True, "first_name": "Alex", "last_name": "Morgan"}
    assert text("Alex Morgan").routed_to == "signup_consent_pending"
    assert text("YES").routed_to == "signup_complete"
    assert session.scalar(select(m.Approval)) is None


def test_stopped_pending_signup_cannot_receive_more_consent_asks(
    session, clock, provider
):
    from app.core.signup import finish_signup
    from app.core.send_gate import SendGate, handle_stop_start, SendStatus

    gloo = SignupGloo({"signup": True, "first_name": "Alex", "last_name": "Morgan"})
    gate = SendGate(session, clock, provider)
    request_signup(session, clock, gloo, "+15555550199", "JOIN Alex Morgan", gate=gate)
    v = session.scalar(select(m.Volunteer))
    handle_stop_start(session, clock, provider, v, "STOP")
    assert finish_signup(session, clock, gate, v, "YES") is None
    result = gate.send(body="Signup reminder", purpose="signup_reply", volunteer=v)
    assert result.status == SendStatus.BLOCKED_OPT_OUT


@pytest.mark.parametrize(
    "email,password,status",
    [
        ("outsider@example.test", "a-valid-test-password", 403),
        ("coordinator@example.test", "short", 422),
    ],
)
def test_admin_registration_rejects_uninvited_or_weak_credentials(
    email, password, status
):
    app = create_app(
        Settings(
            database_url="sqlite://", admin_email_allowlist="coordinator@example.test"
        )
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/register", json={"email": email, "password": password}
            ).status_code
            == status
        )


def test_admin_registration_and_recovery_use_supabase(monkeypatch):
    import app.web.texty as texty

    calls = []

    async def fake_auth(settings, path, data, **kwargs):
        calls.append((path, data, kwargs))
        return {}

    monkeypatch.setattr(texty, "auth_request", fake_auth)
    app = create_app(
        Settings(
            database_url="sqlite://", admin_email_allowlist="coordinator@example.test"
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/register",
            json={
                "email": "coordinator@example.test",
                "password": "synthetic-password-only",
            },
        )
        assert response.status_code == 200 and "confirm" in response.json()["message"]
        assert calls[-1][0] == "signup"
        assert (
            client.post(
                "/api/recover", json={"email": "outsider@example.test"}
            ).status_code
            == 200
        )
        assert len(calls) == 1
        assert (
            client.post(
                "/api/recover", json={"email": "coordinator@example.test"}
            ).status_code
            == 200
        )
        assert calls[-1][0] == "recover"
        assert (
            client.post(
                "/api/reset-password", json={"password": "synthetic-password-only"}
            ).status_code
            == 503
        )


def test_unknown_sender_stop_blocks_future_signup_replies_until_start(session, clock, provider):
    from app.agents.fill_agent import FillContext
    from app.core.inbound import handle_inbound
    from app.core.send_gate import SendGate, SendStatus
    gloo=SignupGloo({"signup":True,"first_name":"Alex","last_name":"Morgan"})
    ctx=FillContext(session,clock,provider,gloo)
    def text(body):return handle_inbound(session,clock,provider,'+15555550199',body,None,ctx=ctx,allow_signup=True)
    assert text('STOP').routed_to == 'stop'
    assert text('JOIN Alex Morgan').routed_to == 'stop'
    assert not provider.sent and gloo.calls == 0
    assert SendGate(session,clock,provider).send(body='No bypass',purpose='signup_reply',phone='+15555550199').status == SendStatus.BLOCKED_OPT_OUT
    assert text('START').routed_to == 'signup_invitation'
    assert text('JOIN Alex Morgan').routed_to == 'signup_consent_pending'


def test_admin_password_reset_and_logout_validate_identity(monkeypatch):
    import httpx
    import app.web.texty as texty

    calls = []
    confirmed = True

    def handler(request):
        if request.url.path == "/auth/v1/user" and request.method == "GET":
            return httpx.Response(200, json={
                "email": "coordinator@example.test",
                "email_confirmed_at": "2026-10-01" if confirmed else None,
            })
        calls.append(request)
        return httpx.Response(204) if request.url.path.endswith("logout") else httpx.Response(200, json={})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(texty.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handler)
    ))
    app = create_app(Settings(database_url="sqlite://", supabase_url="https://auth.example.test",
        supabase_publishable_key="synthetic-publishable-key", admin_email_allowlist="coordinator@example.test"))
    with TestClient(app) as client:
        body = {"password": "synthetic-password-only"}
        assert client.post("/api/reset-password", json=body).status_code == 401
        headers = {"Authorization": "Bearer synthetic-session"}
        confirmed = False
        assert client.post("/api/reset-password", json=body, headers=headers).status_code == 403
        assert not calls
        confirmed = True
        assert client.post("/api/reset-password", json=body, headers=headers).status_code == 200
        assert calls[-1].method == "PUT" and calls[-1].url.path == "/auth/v1/user"
        assert calls[-1].headers["Authorization"] == headers["Authorization"]
        assert json.loads(calls[-1].content) == body
        assert client.post("/api/logout", headers=headers).status_code == 200
        assert calls[-1].url.path == "/auth/v1/logout"


def test_registration_preserves_confirmation_destination_and_rate_limit(monkeypatch):
    import httpx
    import app.web.texty as texty

    def handler(request):
        assert request.url.path == "/auth/v1/signup"
        assert request.url.params["redirect_to"] == "https://texty.example.test"
        return httpx.Response(429, json={"message": "synthetic limit"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(texty.httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handler)
    ))
    app = create_app(Settings(database_url="sqlite://", supabase_url="https://auth.example.test",
        supabase_publishable_key="synthetic-publishable-key", admin_email_allowlist="coordinator@example.test",
        admin_site_url="https://texty.example.test"))
    with TestClient(app) as client:
        assert client.post("/api/register", json={"email": "coordinator@example.test",
            "password": "synthetic-password-only"}).status_code == 429


def test_registration_validates_church_details_before_signup_and_retains_email_confirmation(monkeypatch):
    import app.web.texty as texty
    from tests.test_admin_setup import DETAILS
    from app.admin_setup.models import Workspace
    calls = []
    async def fake_auth(settings,path,data,**kwargs):
        calls.append((path,data))
        return {}
    monkeypatch.setattr(texty,'auth_request',fake_auth)
    app=create_app(Settings(database_url='sqlite://',admin_email_allowlist='coordinator@example.test'))
    payload={'email':'coordinator@example.test','password':'synthetic-password-only','church_details':{k:v for k,v in DETAILS.items() if k!='affiliation'}}
    with TestClient(app) as client:
        assert client.post('/api/register',json={**payload,'church_details':{'church_name':'Incomplete'}}).status_code == 422
        assert calls == []
        result=client.post('/api/register',json=payload)
        assert result.status_code == 200 and 'confirm' in result.json()['message']
        assert 'access_token' not in result.json()
        assert calls[0][0] == 'signup' and calls[0][1]['data']['church_setup']['church_name'] == DETAILS['church_name']
        assert set(calls[0][1]) == {'email','password','data'}
        assert client.post('/api/setup/from-account',json={}).status_code in {401,503}
    with app.state.session_factory() as session:
        assert session.scalar(select(Workspace)) is None


def test_shift_state_returns_actual_role_requirements_without_changing_records(session, clock, make_shift):
    from app.web.routes import db

    expected = {
        'Production': ['sound_training'],
        'Child Care': ['background_check', 'child_safety_training'],
        'Greeter': [],
    }
    for name, requirements in expected.items():
        make_shift(name, required=requirements)
    session.commit()
    app = create_app(Settings(database_url='sqlite://', demo_mode=True, automation_enabled=False))
    app.state.clock = clock
    app.dependency_overrides[admin] = lambda: {'email': 'coordinator@example.test'}
    app.dependency_overrides[db] = lambda: session
    with TestClient(app) as client:
        response = client.get('/api/state')
    assert response.status_code == 200, response.text
    rows = {row['role']: row for row in response.json()['shifts']}
    assert {name: row['required_qualifications'] for name, row in rows.items()} == expected
    assert {name: row['sensitive'] for name, row in rows.items()} == {
        'Production': True, 'Child Care': True, 'Greeter': False,
    }
    assert {role.name: role.required_qualifications for role in session.scalars(select(m.Role))} == expected
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Qualification)) is None
    assert session.scalar(select(m.Message)) is None


@pytest.mark.parametrize('entry', ['/app.js', '/texty/app.js'])
def test_public_app_module_import_closure_is_served_by_actual_backend(entry):
    import re
    from urllib.parse import urljoin

    app = create_app(Settings(database_url='sqlite://', demo_mode=True, automation_enabled=False))
    with TestClient(app) as client:
        pending, visited = [entry], set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            response = client.get(path)
            assert response.status_code == 200, f'{entry} imports unavailable module {path}'
            assert 'javascript' in response.headers['content-type'], path
            visited.add(path)
            imports = re.findall(r'''\b(?:from\s*|import\s*)['"](\.\.?/[^'"]+\.js)['"]''', response.text)
            pending.extend(urljoin(path, dependency) for dependency in imports)
        prefix = '/texty' if entry.startswith('/texty/') else ''
        assert prefix+'/volunteer-history.js' in visited
        assert prefix+'/planning-center-blockouts.js' in visited
        assert len(visited) >= 15  # Exercise the real import graph, not just one named route.
        assert client.get('/texty/fictional_history.py').status_code == 404
        assert client.get('/texty/AGENTS.md').status_code == 404
        assert client.get('/api/state').status_code != 200

def test_manual_volunteer_add_leaves_ministry_for_text_setup():
    app = create_app(Settings(database_url="sqlite://", automation_enabled=False, sms_provider="mock"))
    app.dependency_overrides[admin] = lambda: {"email": "coordinator@example.test"}
    data = {"first_name": "Alex", "last_name": "Sample", "phone": "+12025550199", "consent": False}
    with TestClient(app) as client:
        response = client.post("/api/volunteers", json=data)
        assert response.status_code == 200, response.text
        assert response.json()["ministry"] == "Not set"
        volunteer_id = response.json()["id"]
        with app.state.session_factory() as session:
            volunteer = session.get(m.Volunteer, int(volunteer_id))
            assert "preferred_ministry" not in volunteer.preferences
            assert not volunteer.sms_opt_in
            assert session.scalar(select(m.Message)) is None
            volunteer.preferences = {"preferred_ministry": "Production"}
            session.commit()
        response = client.post(f"/api/volunteers/{volunteer_id}", json={**data, "status": "active"})
        assert response.status_code == 200, response.text
        assert response.json()["ministry"] == "Production"

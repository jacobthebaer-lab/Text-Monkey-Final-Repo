"""Fake Supabase responses only. No real login, browser storage or token reads."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

EMAIL='coordinator@example.test'
USER={'email':EMAIL,'email_confirmed_at':'2026-10-01'}
SESSION={'user':USER,'access_token':'synthetic-access','refresh_token':'synthetic-rotated',
         'expires_in':3600,'expires_at':2000000000}


@pytest.fixture
def auth(monkeypatch):
    upstream=SimpleNamespace(status=200,result=SESSION,calls=[])
    class Response:
        @property
        def status_code(self):return upstream.status
        def json(self):
            if isinstance(upstream.result,Exception):raise upstream.result
            return upstream.result
    class Client:
        def __init__(self,**_kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*_args):pass
        async def post(self,url,**kwargs):
            upstream.calls.append((url,kwargs));return Response()
        async def get(self,url,**kwargs):
            upstream.calls.append((url,kwargs));return Response()
    monkeypatch.setattr('app.web.texty.httpx.AsyncClient',Client)
    app=create_app(Settings(database_url='sqlite://',demo_mode=True,supabase_url='https://supabase.example.test',
        supabase_publishable_key='synthetic-publishable',admin_email_allowlist=EMAIL))
    return TestClient(app),upstream


def test_password_login_returns_granted_rotating_session_no_store(auth):
    client,up=auth
    response=client.post('/api/login',json={'email':EMAIL,'password':'synthetic-test-password'})
    assert response.status_code==200
    assert response.headers['Cache-Control']=='no-store'
    assert response.json()=={k:v for k,v in SESSION.items() if k!='user'}|{'email':EMAIL}
    assert up.calls[0][0].endswith('/auth/v1/token?grant_type=password')


def test_refresh_uses_publishable_key_and_granted_expiry(auth):
    client,up=auth
    response=client.post('/api/session/refresh',json={'refresh_token':'synthetic-parent'})
    assert response.status_code==200
    assert response.headers['Cache-Control']=='no-store'
    url,options=up.calls[0]
    assert url.endswith('/auth/v1/token?grant_type=refresh_token')
    assert options=={'headers':{'apikey':'synthetic-publishable'},'json':{'refresh_token':'synthetic-parent'}}
    assert response.json()['expires_in']==3600


@pytest.mark.parametrize('status,expected',[(400,401),(401,401),(403,401),(422,401),(429,429),(500,503),(503,503)])
def test_refresh_classifies_terminal_and_transient_failures_without_echo(auth,status,expected):
    client,up=auth;up.status=status
    up.result={'error_description':'synthetic-secret-that-must-not-echo'}
    response=client.post('/api/session/refresh',json={'refresh_token':'synthetic-parent'})
    assert response.status_code==expected
    assert 'synthetic-parent' not in response.text and 'must-not-echo' not in response.text


@pytest.mark.parametrize('data',[{},[],None,{'refresh_token':True},{'refresh_token':''},{'refresh_token':'x'*4097},
    {'refresh_token':'synthetic-parent','expires_in':999999}])
def test_invalid_refresh_request_never_calls_supabase(auth,data):
    client,up=auth
    response=client.post('/api/session/refresh',json=data)
    assert response.status_code==400 and up.calls==[]
    assert 'synthetic-parent' not in response.text


@pytest.mark.parametrize('user',[{'email':EMAIL},{'email':'outsider@example.test','email_confirmed_at':'2026-10-01'}])
def test_refresh_still_requires_verified_allowlisted_account(auth,user):
    client,up=auth;up.result={**SESSION,'user':user}
    response=client.post('/api/session/refresh',json={'refresh_token':'synthetic-parent'})
    assert response.status_code==403
    assert response.headers['X-Texty-Auth-Invalid']=='1'
    assert 'synthetic-access' not in response.text and 'synthetic-rotated' not in response.text


@pytest.mark.parametrize('status,expected',[(401,401),(403,401),(429,429),(500,503),(502,503),(200,200)])
def test_current_user_outage_is_not_expiry(auth,status,expected):
    client,up=auth;up.status=status;up.result=USER
    response=client.get('/api/state',headers={'Authorization':'Bearer synthetic-access'})
    assert response.status_code==expected
    assert all(url.endswith('/auth/v1/user') for url,_ in up.calls)


@pytest.mark.parametrize('result',[[],{'access_token':'synthetic-access','user':USER},ValueError('synthetic invalid json')])
def test_bad_refresh_response_fails_closed_as_service_failure(auth,result):
    client,up=auth;up.result=result
    response=client.post('/api/session/refresh',json={'refresh_token':'synthetic-parent'})
    assert response.status_code==503 and 'synthetic-access' not in response.text


def test_network_outage_preserves_service_error(auth,monkeypatch):
    client,up=auth
    class Broken:
        def __init__(self,**_kwargs):pass
        async def __aenter__(self):raise httpx.ConnectError('synthetic connection failure')
        async def __aexit__(self,*_args):pass
    monkeypatch.setattr('app.web.texty.httpx.AsyncClient',Broken)
    assert client.get('/api/state',headers={'Authorization':'Bearer synthetic-access'}).status_code==503
    assert client.post('/api/session/refresh',json={'refresh_token':'synthetic-parent'}).status_code==503


@pytest.mark.parametrize('path', ['/api/login', '/api/register', '/api/recover'])
@pytest.mark.parametrize('body', ['null', '[]', '"secret-input"', '{', '{"email":null}', '{"email":42}'])
def test_auth_rejects_malformed_input_without_upstream_or_secret_echo(auth, path, body):
    client, up = auth
    response = client.post(path, content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 422
    assert up.calls == []
    assert 'secret-input' not in response.text


@pytest.mark.parametrize('status,expected', [(400,401), (401,401), (403,401), (422,401),
                                           (429,429), (500,503), (502,503), (503,503)])
def test_login_distinguishes_invalid_credentials_from_upstream_outage(auth, status, expected):
    client, up = auth
    up.status = status
    response = client.post('/api/login', json={'email': EMAIL, 'password': 'private-password'})
    assert response.status_code == expected
    assert 'private-password' not in response.text


def test_login_normalizes_email_and_rejects_nonstring_password(auth):
    client, up = auth
    bad = client.post('/api/login', json={'email': EMAIL, 'password': {'secret': 'value'}})
    assert bad.status_code == 422 and not up.calls
    good = client.post('/api/login', json={'email': ' '+EMAIL.upper()+' ', 'password': 'valid-password'})
    assert good.status_code == 200
    assert up.calls[-1][1]['json']['email'] == EMAIL


@pytest.mark.parametrize('path', ['/api/login', '/api/register', '/api/recover'])
def test_auth_caps_body_before_contacting_supabase(auth, path):
    client, up = auth
    response = client.post(path, content=b' ' * 32769, headers={'Content-Type':'application/json'})
    assert response.status_code == 413 and up.calls == []


@pytest.mark.parametrize('status,expected', [(429,429), (500,503), (502,503), (503,503)])
def test_recovery_distinguishes_upstream_outage(auth, monkeypatch, status, expected):
    client, up = auth
    async def request(self, method, url, **kwargs):
        up.calls.append((url,kwargs))
        return httpx.Response(status, json={'error':'private-upstream-detail'})
    monkeypatch.setattr('app.web.texty.httpx.AsyncClient.request',request,raising=False)
    response = client.post('/api/recover',json={'email':EMAIL})
    assert response.status_code == expected
    assert 'private-upstream-detail' not in response.text


def test_recovery_malformed_success_is_sanitized_service_failure(auth, monkeypatch):
    client, up = auth
    async def request(self, method, url, **kwargs):
        return httpx.Response(200, content=b'not-json-private-upstream-detail')
    monkeypatch.setattr('app.web.texty.httpx.AsyncClient.request',request,raising=False)
    response = client.post('/api/recover',json={'email':EMAIL})
    assert response.status_code == 503
    assert 'private-upstream-detail' not in response.text

"""Real HTTPError redirect responses, with no network or credential-bearing hops."""
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.response import addinfourl

import pytest

from tests.test_public_connection_supervisor import tool

ALIAS = 'https://text-monkey-demo.pages.dev'
DEPLOYMENT = 'https://a7e17d0a.text-monkey-demo.pages.dev'


class Responses:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def open(self, request, timeout):
        self.requests.append(request)
        status, location, body = self.routes[request.full_url]
        headers = Message()
        if isinstance(location, list):
            for value in location: headers['Location'] = value
        elif location is not None:
            headers['Location'] = location
        if status != 200:
            raise HTTPError(request.full_url, status, 'Synthetic HTTP response', headers, BytesIO(body))
        return addinfourl(BytesIO(body), headers, request.full_url, code=200)


def host_for(routes):
    host = tool.Host({'backend_bridge_key': 'never-forward-this-secret'})
    host.opener = Responses(routes)
    return host


@pytest.mark.parametrize('base', [ALIAS, DEPLOYMENT])
@pytest.mark.parametrize('status', [301, 308])
@pytest.mark.parametrize('asset,target', [('index.html','/'),('404.html','/404'),('onboarding-copy.html','/onboarding-copy')])
def test_pages_pretty_url_redirects_hash_exact_bytes_anonymously(base,status,asset,target):
    payload = b'Exact reviewed public bytes\x00\xff'
    host = host_for({base+'/'+asset:(status,target,b'redirect body is not the asset'), base+target:(200,None,payload)})
    assert host.asset_hash(base,asset) == tool.digest(payload)
    assert [r.full_url for r in host.opener.requests] == [base+'/'+asset,base+target]
    for request in host.opener.requests:
        assert request.get_method() == 'GET'
        assert dict(request.header_items()) == {'User-agent':'Mozilla/5.0','Cache-control':'no-cache'}
        assert request.data is None


@pytest.mark.parametrize('target', [
    'https://foreign.example.test/file', '//foreign.example.test/file',
    'http://text-monkey-demo.pages.dev/file', 'file:///private/credential', 'javascript:alert(1)',
    'https://user:password@text-monkey-demo.pages.dev/file', 'https://@text-monkey-demo.pages.dev/file',
    'https://text-monkey-demo.pages.dev:444/file', 'https://text-monkey-demo.pages.dev./file',
    'https://[broken/file', '/bad%escape', '/bad%0d%0aheader', '/bad%5cpath',
    '/bad\\path', '\nhttps://text-monkey-demo.pages.dev/file', '/bad path', '/file#fragment', '',
])
def test_unsafe_or_malformed_asset_redirect_stops_before_second_request(target):
    host = host_for({ALIAS+'/index.html':(308,target,b'')})
    assert host.asset_hash(ALIAS,'index.html') is None
    assert len(host.opener.requests) == 1


@pytest.mark.parametrize('location',[None,['/one','/two']])
def test_missing_or_ambiguous_location_holds(location):
    host = host_for({ALIAS+'/index.html':(301,location,b'')})
    assert host.asset_hash(ALIAS,'index.html') is None
    assert len(host.opener.requests) == 1


def test_relative_and_absolute_same_origin_redirect_chain_preserves_hash():
    payload = b'reviewed font'
    host = host_for({ALIAS+'/brand/font.ttf':(301,'../font.ttf',b''),
        ALIAS+'/font.ttf':(308,ALIAS+':443/fonts/font.ttf',b''),
        ALIAS+'/fonts/font.ttf':(200,None,payload)})
    assert host.asset_hash(ALIAS,'brand/font.ttf') == tool.digest(payload)
    assert len(host.opener.requests) == 3


def test_redirect_loop_is_detected_without_repeating_a_get():
    host = host_for({ALIAS+'/index.html':(301,'/one',b''),ALIAS+'/one':(308,'/index.html',b'')})
    assert host.asset_hash(ALIAS,'index.html') is None
    assert len(host.opener.requests) == 2


def test_asset_redirect_limit_is_five_hops():
    routes = {ALIAS+'/index.html':(301,'/0',b'')}
    routes.update({ALIAS+'/'+str(i):(308,'/'+str(i+1),b'') for i in range(6)})
    host = host_for(routes)
    assert host.asset_hash(ALIAS,'index.html') is None
    assert len(host.opener.requests) == 6


def test_fifth_redirect_can_reach_the_asset():
    routes = {ALIAS+'/index.html':(301,'/0',b'')}
    routes.update({ALIAS+'/'+str(i):(308,'/'+str(i+1),b'') for i in range(4)})
    routes[ALIAS+'/4'] = (200,None,b'approved asset')
    host = host_for(routes)
    assert host.asset_hash(ALIAS,'index.html') == tool.digest(b'approved asset')
    assert len(host.opener.requests) == 6


@pytest.mark.parametrize('status',[301,308])
def test_api_and_authenticated_requests_do_not_follow_redirects(status):
    host = host_for({ALIAS+'/api/state':(status,'/private-target',b'{"held":true}')})
    assert host.request(ALIAS,'/api/state',headers={'Authorization':'Bearer synthetic-token'}) == (status,{'held':True})
    assert len(host.opener.requests) == 1
    request = host.opener.requests[0]
    assert tool.NoRedirects().redirect_request(request,None,status,'Redirect',Message(),ALIAS+'/private-target') is None


@pytest.mark.parametrize('base',[ 'https://foreign.example.test', 'http://text-monkey-demo.pages.dev',
    'https://text-monkey-demo.pages.dev/private', 'https://user@text-monkey-demo.pages.dev'])
def test_asset_initial_origin_must_be_the_public_alias_or_immutable_deployment(base):
    host = host_for({})
    assert host.asset_hash(base,'index.html') is None
    assert host.opener.requests == []


def test_non_success_final_response_is_not_hashed():
    host = host_for({ALIAS+'/index.html':(301,'/',b''),ALIAS+'/':(503,None,b'offline')})
    assert host.asset_hash(ALIAS,'index.html') is None

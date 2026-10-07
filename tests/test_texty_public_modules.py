"""Serve the real portal dependency graph without exposing other files."""
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.web.texty import STATIC


class References(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.paths = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {'src', 'href'} and value:
                self.paths.append(value)


@pytest.fixture
def client():
    app = create_app(Settings(database_url='sqlite://', sms_provider='mock',
                              automation_enabled=False, mac_bridge_enabled=False))
    with TestClient(app) as connection:
        yield connection


@pytest.mark.parametrize('prefix', ['/', '/texty/'])
def test_portal_module_graph_loads_at_both_public_routes(client, prefix):
    page = client.get('/texty')
    assert page.status_code == 200
    pending = [PurePosixPath(path).name for path in References(page.text).paths if path.endswith('.js')]
    visited = set()
    while pending:
        asset = pending.pop()
        if asset in visited:
            continue
        visited.add(asset)
        response = client.get(prefix + asset)
        assert response.status_code == 200, prefix + asset
        assert 'javascript' in response.headers['content-type']
        assert response.content == (STATIC / asset).read_bytes()
        imports = re.findall(r'''(?:from\s+|import\s+)['"](\./[^'"]+\.js)['"]''', response.text)
        pending.extend(PurePosixPath(path).name for path in imports)
    assert {'accessibility.js', 'admin-readiness.js', 'onboarding-copy-nav.js',
            'setup.js', 'setup-domain.js', 'domain.js', 'coordinator-session.js',
            'acceptance-workflow.js', 'coordinator-workflows.js',
            'planning-center-review.js', 'planning-center-blockouts.js', 'church-presentation.js'} <= visited


@pytest.mark.parametrize('prefix', ['/', '/texty/'])
def test_onboarding_editor_and_its_styles_script_defaults_load(client, prefix):
    response = client.get(prefix + 'onboarding-copy.html')
    assert response.status_code == 200 and 'text/html' in response.headers['content-type']
    assert response.content == (STATIC / 'onboarding-copy.html').read_bytes()
    assets = {PurePosixPath(path).name for path in References(response.text).paths
              if path.endswith(('.css', '.js'))}
    script = client.get(prefix + 'onboarding-copy.js')
    assert script.status_code == 200
    assets.update(re.findall(r'''request\('/([^']+\.json)'\)''', script.text))
    assert {'onboarding-copy.js', 'onboarding-copy.css', 'onboarding-copy-defaults.json'} <= assets
    for asset in assets:
        result = client.get(prefix + asset)
        assert result.status_code == 200, prefix + asset
        assert result.content == (STATIC / asset).read_bytes()
        mime = 'javascript' if asset.endswith('.js') else 'text/css' if asset.endswith('.css') else 'application/json'
        assert mime in result.headers['content-type']
        if asset.endswith('.json'):
            assert isinstance(result.json(), dict)


@pytest.mark.parametrize('path', [
    '/.env', '/private.json', '/app/main.py', '/texty/.env',
    '/texty/private.json', '/texty/app/main.py', '/texty/%2e%2e%2f.env',
    '/texty/%2e%2e%2fapp%2fmain.py', '/texty/onboarding-copy.js%2f..%2f.env',
    '/planning-center-blockouts.js%2f..%2f.env', '/texty/planning-center-blockouts.js%2f..%2f.env',
])
def test_unlisted_and_traversal_paths_remain_denied(client, path):
    response = client.get(path)
    assert response.status_code == 404


def test_allowlist_refuses_unlisted_files_even_inside_public_directory(client, tmp_path, monkeypatch):
    # The directory being public is not enough to expose a file.
    from app.web import texty
    (tmp_path / 'private.json').write_text('{"private":"never expose"}')
    (tmp_path / '.env').write_text('PRIVATE=never expose')
    (tmp_path / 'planning-center-blockouts.private.js').write_text('PRIVATE=never expose')
    monkeypatch.setattr(texty, 'STATIC', tmp_path)
    for prefix in ('/', '/texty/'):
        for asset in ('private.json', '.env', 'planning-center-blockouts.private.js'):
            response = client.get(prefix + asset)
            assert response.status_code == 404 and 'never expose' not in response.text

"""The isolated launcher must not expose real APIs or arbitrary files."""
import importlib.util
from pathlib import Path
import threading
import re
import urllib.error
import urllib.request
import pytest

spec = importlib.util.spec_from_file_location('texty_local_demo', Path(__file__).parents[1] / 'tools/texty_local_demo.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_loopback_and_synthetic_only():
    with pytest.raises(ValueError): module.server('0.0.0.0', 0)
    with pytest.raises(ValueError): module.server(port=0, mode='production')

def test_isolated_demo_routes():
    with module.server(port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(root+'/texty') as response:
                assert b'/local-demo.js' in response.read()
                assert "connect-src 'self'" in response.headers['Content-Security-Policy']
            with urllib.request.urlopen(root+'/api/config') as response:
                config = __import__('json').load(response)
                assert not config['connected'] and not config['liveSms'] and not config['aiReady']
            with urllib.request.urlopen(root+'/cloud-preview') as response:
                assert b'Disconnected preview' in response.read()
                assert "connect-src 'none'" in response.headers['Content-Security-Policy']
            with urllib.request.urlopen(root+'/cloud-preview.js') as response:
                assert b'disconnectedPreview:true' in response.read()
            for asset in ['accessibility.js', 'admin-readiness.js', 'onboarding-copy-nav.js',
                          'onboarding-copy.js', 'onboarding-copy.html', 'onboarding-copy.css',
                          'onboarding-copy-defaults.json', 'cloud-texting.js']:
                with urllib.request.urlopen(root+'/'+asset) as response:
                    assert response.status == 200
                    assert response.read() == (module.PUBLIC / asset).read_bytes()
            for path in ['/api/state','/api/login','/api/cloud-texting','/api/auth/me','/api/messages/1/send','/../.env','/app/main.py']:
                with pytest.raises(urllib.error.HTTPError): urllib.request.urlopen(root+path)
            for method in ['POST','PUT','PATCH','DELETE','OPTIONS']:
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(root+'/api/login', data=b'{}', method=method))
                assert error.value.code == 403
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(root+'/api/config', headers={'Host':'example.com'}))
            assert error.value.code == 403
        finally: server.shutdown(); thread.join()


def test_complete_actual_module_graph_is_served_without_broadening_file_or_api_access():
    with module.server(port=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        try:
            visited, pending = set(), ['app.js']
            while pending:
                name = pending.pop()
                if name in visited:
                    continue
                visited.add(name)
                with urllib.request.urlopen(root+'/'+name) as response:
                    assert response.status == 200
                    source = response.read().decode()
                    assert source == (module.PUBLIC/name).read_text()
                pending.extend(re.findall(r"(?:from\s+|import\s*)['\"]\./([^'\"]+)['\"]", source))
            assert 'planning-center-blockouts.js' in visited
            assert len(visited) >= 20
            for path in ['/api/planning-center/blockouts', '/.env', '/app/web/texty.py']:
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(root+path)
                assert error.value.code in {403,404}
        finally:
            server.shutdown()
            thread.join()

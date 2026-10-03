"""The isolated launcher must not expose real APIs or arbitrary files."""
import importlib.util
from pathlib import Path
import threading
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
            for asset in ['accessibility.js', 'admin-readiness.js', 'onboarding-copy-nav.js',
                          'onboarding-copy.js', 'onboarding-copy.html', 'onboarding-copy.css',
                          'onboarding-copy-defaults.json']:
                with urllib.request.urlopen(root+'/'+asset) as response:
                    assert response.status == 200
                    assert response.read() == (module.PUBLIC / asset).read_bytes()
            for path in ['/api/state','/api/login','/api/messages/1/send','/../.env','/app/main.py']:
                with pytest.raises(urllib.error.HTTPError): urllib.request.urlopen(root+path)
            for method in ['POST','PUT','PATCH','DELETE','OPTIONS']:
                with pytest.raises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(root+'/api/login', data=b'{}', method=method))
                assert error.value.code == 403
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(root+'/api/config', headers={'Host':'example.com'}))
            assert error.value.code == 403
        finally: server.shutdown(); thread.join()

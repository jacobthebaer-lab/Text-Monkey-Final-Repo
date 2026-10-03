"""A deployment receipt must fail on the wrong host, stale files or changed access."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("verify_cloudflare_demo", Path(__file__).parents[1] / "tools/verify_cloudflare_demo.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture
def upload(tmp_path):
    for name in verifier.ASSETS:
        (tmp_path / name).write_bytes(f"tested {name}".encode())
    (tmp_path / "api").mkdir()
    (tmp_path / "api/config.json").write_text(json.dumps({"publicDemo": True, "connected": False, "aiReady": False, "liveSms": False}))
    return tmp_path


def response_for(upload, stale=False, redirect=False):
    def fetch(url):
        path = url.removeprefix(verifier.DEMO_URL)
        headers = {"content-security-policy": "default-src 'self'; connect-src 'self'", "x-content-type-options": "nosniff", "cache-control": "no-store"}
        body = b"" if path == "api/state" else (upload / ("api/config.json" if path == "api/config" else path)).read_bytes()
        if stale and path == "app.js":
            body = b"old release"
        return 404 if path == "api/state" else 200, headers, body, "https://example.com/" if redirect else url
    return fetch


def test_exact_release_yields_receipt(upload):
    result = verifier.verify(verifier.DEMO_URL, upload, response_for(upload))
    assert set(result["asset_sha256"]) == set(verifier.ASSETS) | {"api/config.json"}
    assert result["backend_route_status"] == 404
    assert result["real_text_delivery"] is False


def test_stale_asset_cannot_confirm_publication(upload):
    with pytest.raises(ValueError, match="live bytes differ"):
        verifier.verify(verifier.DEMO_URL, upload, response_for(upload, stale=True))


def test_redirect_cannot_confirm_wrong_site(upload):
    with pytest.raises(ValueError, match="redirected away"):
        verifier.verify(verifier.DEMO_URL, upload, response_for(upload, redirect=True))


@pytest.mark.parametrize("url", ["http://127.0.0.1:58122/", "https://rjc-script.pages.dev/", "https://text-monkey-demo.pages.dev/?token=private"])
def test_wrong_origin_is_rejected_before_requests(upload, url):
    with pytest.raises(ValueError, match="Cloudflare Pages"):
        verifier.verify(url, upload, lambda _: pytest.fail("Unexpected request"))


@pytest.mark.parametrize("name", ["editor.js", "editor.css", "editor.html", "editor-defaults.json"])
def test_additional_bundled_ui_asset_is_also_verified(upload, name):
    module = upload / "components" / name
    module.parent.mkdir()
    module.write_bytes(b"published module")
    fetcher = response_for(upload)
    assert f"components/{name}" in verifier.verify(verifier.DEMO_URL, upload, fetcher)["asset_sha256"]
    def stale_module(url):
        status, headers, body, final_url = fetcher(url)
        return status, headers, b"old module" if url.endswith(f"components/{name}") else body, final_url
    with pytest.raises(ValueError, match="live bytes differ"):
        verifier.verify(verifier.DEMO_URL, upload, stale_module)

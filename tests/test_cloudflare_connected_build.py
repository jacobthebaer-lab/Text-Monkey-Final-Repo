"""The connected package preserves UI bytes and cannot retain a static API shadow."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('cloudflare_build', Path(__file__).parents[1] / 'tools/build_cloudflare_demo.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_connected_package_preserves_assets_and_uses_existing_proxy(tmp_path):
    source = Path(__file__).parents[1] / 'web/texty/public'
    destination = builder.build(tmp_path/'connected', connected=True)
    for asset in source.rglob('*'):
        if asset.is_file():
            assert (destination/asset.relative_to(source)).read_bytes() == asset.read_bytes()
    assert (destination/'_worker.js').read_bytes() == (source.parent/'worker.js').read_bytes()
    assert not (destination/'api/config.json').exists()
    assert not (destination/'_redirects').exists()
    assert json.loads((destination/'_routes.json').read_text()) == {
        'version': 1, 'include': ['/api/*', '/sms/*'], 'exclude': [],
    }


def test_default_remains_disconnected_and_existing_output_is_preserved(tmp_path):
    destination = builder.build(tmp_path/'static')
    assert json.loads((destination/'api/config.json').read_text())['publicDemo']
    assert not (destination/'_worker.js').exists()
    with pytest.raises(ValueError, match='existing files'):
        builder.build(destination, connected=True)
    assert (destination/'api/config.json').is_file()


def test_connected_proxy_symlink_is_rejected_before_packaging(tmp_path):
    source = tmp_path/'public'
    source.mkdir()
    (source/'index.html').write_text('synthetic UI')
    target = tmp_path/'other.js'
    target.write_text('synthetic proxy')
    (tmp_path/'worker.js').symlink_to(target)
    destination = tmp_path/'upload'
    with pytest.raises(ValueError, match='regular existing API proxy'):
        builder.build(destination, source, connected=True)
    assert not destination.exists()


@pytest.mark.parametrize('connected', [False, True])
def test_missing_page_has_plain_product_copy_in_both_packages(tmp_path, connected):
    destination = builder.build(tmp_path/'upload', connected=connected)
    page = (destination/'404.html').read_text()
    assert 'This page is unavailable.' in page
    assert 'Open Text Monkey' in page
    assert not any(word in page.lower() for word in ('synthetic', 'demo', 'test'))

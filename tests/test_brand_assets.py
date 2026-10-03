"""Brand files are reachable on the actual app without exposing arbitrary files."""
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.web.brand import BRAND_ASSETS


def test_actual_app_brand_assets_and_public_only_boundary():
    with TestClient(create_app(Settings(database_url='sqlite://', admin_password='test-only'))) as client:
        # Logos/fonts are public, but existing protected admin data stays protected.
        assert client.get('/').status_code == 401
        for asset in BRAND_ASSETS:
            response = client.get('/brand/' + asset)
            assert response.status_code == 200, asset
            assert response.content
        manifest = client.get('/brand/site.webmanifest').json()
        assert manifest['name'] == 'Text Monkey'
        assert manifest['theme_color'] == '#FFD23F'
        for path in ['/brand/.env', '/brand/fonts/../../app/config.py', '/brand/unknown.png']:
            assert client.get(path).status_code == 404

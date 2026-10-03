"""The real FastAPI dashboard must serve its onboarding module dependencies."""
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.web.texty import STATIC


def test_dashboard_onboarding_imports_are_served_without_exposing_other_files():
    app = create_app(Settings(database_url="sqlite://", sms_provider="mock", automation_enabled=False))
    with TestClient(app) as client:
        main = client.get("/app.js")
        assert main.status_code == 200
        assert 'from "./setup.js"' in main.text
        for asset in ("setup.js", "setup-domain.js", "planning-workflows.js"):
            for prefix in ("/", "/texty/"):
                response = client.get(prefix + asset)
                assert response.status_code == 200
                assert "javascript" in response.headers["content-type"]
                assert response.content == (STATIC / asset).read_bytes()
        assert "./setup-domain.js" in client.get("/setup.js").text
        assert client.get("/texty/.env").status_code == 404
        assert client.get("/texty/private.json").status_code == 404

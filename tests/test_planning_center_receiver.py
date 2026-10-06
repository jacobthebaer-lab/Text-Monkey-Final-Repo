import pytest
from fastapi.testclient import TestClient

from app.db.session import make_engine, init_db
from app.integrations.planning_center import PCOConfig, PlanningCenterError
from tools.planning_center_webhook_server import create_receiver

CONFIG = PCOConfig("fixture", "fixture-secret", "10", ("20",), "fixture-hook")


def test_receiver_exposes_no_admin_or_message_routes(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'synthetic-demo.db')
    engine = make_engine(url)
    init_db(engine)
    engine.dispose()
    app = create_receiver(CONFIG, url)
    with TestClient(app) as client:
        assert client.get('/healthz').json()['texting'] is False
        assert client.post('/integrations/planning-center/webhook', json={}).status_code == 401
        for path in ['/texty', '/api/state', '/api/volunteers', '/api/simulate', '/api/automation/tick', '/api/setup/admin-texts', '/sms/inbound', '/docs', '/openapi.json']:
            assert client.get(path).status_code == 404
            assert client.post(path, json={}).status_code == 404
        assert client.get('/api/planning-center/role-bindings/catalogue').status_code == 404
        for path in ['/api/planning-center/role-bindings/proposal', '/api/planning-center/role-bindings']:
            assert client.post(path, json={}).status_code == 404
    app.state.engine.dispose()


def test_receiver_requires_existing_isolated_database_and_secret(tmp_path):
    with pytest.raises(PlanningCenterError, match='credentials'):
        create_receiver(PCOConfig(organization_id='10', service_type_ids=('20',)), 'sqlite:///./demo.db')
    with pytest.raises(PlanningCenterError, match='isolated'):
        create_receiver(CONFIG, 'postgresql://production')
    with pytest.raises(PlanningCenterError, match='sync'):
        create_receiver(CONFIG, 'sqlite:///' + str(tmp_path / 'missing-demo.db'))


def test_unconfigured_receiver_fails_closed_until_subscription_secret_saved(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'synthetic-demo.db')
    engine = make_engine(url)
    init_db(engine)
    engine.dispose()
    app = create_receiver(PCOConfig('fixture', 'secret', '10', ('20',)), url)
    with TestClient(app) as client:
        assert client.get('/healthz').json()['webhook_configured'] is False
        assert client.post('/integrations/planning-center/webhook', json={}).status_code == 503
    app.state.engine.dispose()

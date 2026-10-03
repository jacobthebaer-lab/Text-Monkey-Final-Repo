import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db.models import Event, Shift, Role, Assignment, Message, Volunteer
from app.integrations.planning_center import PCOBase, PCOClient, PCOConfig, PlanningCenterError, sync_schedule, PCOEventLink, PCODelivery
from app.main import create_app
from app.web import planning_center as webhook

@pytest.fixture(autouse=True)
def integration_tables(session):
    PCOBase.metadata.create_all(session.get_bind())


CONFIG = PCOConfig("test-app", "private-secret", "10", ("20",), "hook-secret")


def fixture_api(*, quantity=2, times=1, fail=None, org="10"):
    def respond(request):
        path = request.url.path
        if path == fail:
            return httpx.Response(503, json={"secret": "never expose raw responses"})
        root = "/services/v2/service_types/20"
        mapping = {
            "/services/v2": {"data": {"id": org, "attributes": {"name": "Synthetic Church"}}},
            root: {"data": {"id": "20", "attributes": {"name": "Synthetic Sunday"}}},
            root + "/teams": {"data": [{"id": "30", "attributes": {"name": "Demo Greeters"}}]},
            root + "/plans": {"data": [{"id": "40", "attributes": {"title": "Synthetic Sunday"}}]},
            root + "/plans/40/needed_positions": {"data": [{"id": "50", "attributes": {"quantity": quantity, "team_position_name": "Greeter"},
                                                           "relationships": {"team": {"data": {"id": "30"}}}}]},
            root + "/plans/40/plan_times": {"data": [{"id": str(60+i), "attributes": {"time_type": "service", "starts_at": f"2026-10-04T{9+i*2:02d}:00:00-06:00", "ends_at": f"2026-10-04T{10+i*2:02d}:00:00-06:00"}} for i in range(times)]},
        }
        return httpx.Response(200, json=mapping[path])
    return httpx.MockTransport(respond)


def client(**kwargs):
    return PCOClient(CONFIG, transport=fixture_api(**kwargs))


def test_two_times_idempotent_no_people_or_messages(session):
    with client(times=2) as api:
        first = sync_schedule(session, api, CONFIG)
        second = sync_schedule(session, api, CONFIG)
    assert first["events_created"] == 2 and first["shifts_created"] == 4
    assert second["events_created"] == 0 and second["shifts_created"] == 0
    events = list(session.scalars(select(Event)))
    assert events[0].starts_at.hour == 15 and events[1].starts_at.hour == 17
    assert len(list(session.scalars(select(Shift)))) == 4
    assert not list(session.scalars(select(Volunteer)))
    assert not list(session.scalars(select(Message)))
    assert session.scalar(select(Role)).fill_policy == "needs_approval"


def test_complete_snapshot_failure_does_not_write(session):
    with client(fail="/services/v2/service_types/20/plans/40/plan_times") as api:
        with pytest.raises(PlanningCenterError, match="HTTP 503"):
            sync_schedule(session, api, CONFIG)
    assert not list(session.scalars(select(Event)))


def test_wrong_org_blocks_before_import(session):
    with client(org="11") as api:
        with pytest.raises(PlanningCenterError, match="organization"):
            sync_schedule(session, api, CONFIG)
    assert not list(session.scalars(select(Event)))


def test_explicit_scope_required():
    with pytest.raises(PlanningCenterError, match="explicit"):
        PCOConfig(organization_id="10").require_scope()
    assert "private-secret" not in repr(CONFIG) and "hook-secret" not in repr(CONFIG)


def test_pagination_rejects_foreign_host_before_credentials_sent():
    paths = []
    def respond(request):
        paths.append(str(request.url))
        return httpx.Response(200, json={"data": [], "links": {"next": "https://evil.example/steal"}})
    with PCOClient(CONFIG, transport=httpx.MockTransport(respond)) as api:
        with pytest.raises(PlanningCenterError, match="outside"):
            api.collection("/services/v2/service_types")
    assert len(paths) == 1


def test_pagination_includes_every_page():
    def respond(request):
        return httpx.Response(200, json={"data": [{"id": "2" if request.url.params.get("offset") else "1"}],
                                        "links": {"next": None if request.url.params.get("offset") else "https://api.planningcenteronline.com/services/v2/service_types?offset=1"}})
    with PCOClient(CONFIG, transport=httpx.MockTransport(respond)) as api:
        assert [x["id"] for x in api.collection("/services/v2/service_types")] == ["1", "2"]


def test_shrink_preserves_assignment_history(session, make_volunteer):
    with client() as api:
        sync_schedule(session, api, CONFIG)
    shift = session.scalar(select(Shift).order_by(Shift.id.desc()))
    volunteer = make_volunteer()
    now = datetime.now(timezone.utc)
    session.add(Assignment(shift_id=shift.id, volunteer_id=volunteer.id, source="admin", status="approved", created_at=now, updated_at=now))
    session.flush()
    with client(quantity=0) as api:
        result = sync_schedule(session, api, CONFIG)
    assert result["held_occupied"] == 1 and result["shifts_removed"] == 1
    assert session.get(Shift, shift.id) is not None


def envelope(org="10", name="services.v2.events.plan.updated"):
    return {"data": [{"type": "EventDelivery", "id": "delivery-1", "attributes": {"name": name, "attempt": 1},
                      "relationships": {"organization": {"data": {"id": org}}}}]}


def signed_post(tc, body, signature=True):
    raw = json.dumps(body).encode()
    headers = {"X-PCO-Webhooks-Authenticity": hmac.new(CONFIG.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()} if signature else {}
    return tc.post("/integrations/planning-center/webhook", content=raw, headers=headers)


def app_client():
    app = create_app(Settings(database_url="sqlite://", automation_enabled=False))
    app.state.pco_config = CONFIG
    return app, TestClient(app)


def test_unsigned_and_foreign_org_rejected():
    app, tc = app_client()
    assert signed_post(tc, envelope(), signature=False).status_code == 401
    assert signed_post(tc, envelope(org="11")).status_code == 400
    with app.state.session_factory() as s:
        assert not list(s.scalars(select(PCODelivery)))


def test_signed_sync_deduplicated_no_texts(monkeypatch):
    app, tc = app_client()
    monkeypatch.setattr(webhook, "PCOClient", lambda cfg: client())
    response = signed_post(tc, envelope())
    assert response.status_code == 200 and response.json()["deliveries"][0]["status"] == "synced"
    assert signed_post(tc, envelope()).json()["deliveries"][0]["status"] == "duplicate"
    with app.state.session_factory() as s:
        assert len(list(s.scalars(select(Event)))) == 1
        assert len(list(s.scalars(select(PCODelivery)))) == 1
        assert not list(s.scalars(select(Message)))


def test_webhook_failure_retries_without_receipt(monkeypatch):
    app, tc = app_client()
    monkeypatch.setattr(webhook, "PCOClient", lambda cfg: client(fail="/services/v2/service_types/20/plans/40/plan_times"))
    assert signed_post(tc, envelope()).status_code == 503
    with app.state.session_factory() as s:
        assert not list(s.scalars(select(PCODelivery)))
        assert not list(s.scalars(select(Event)))


def test_irrelevant_event_ignored_without_api_call(monkeypatch):
    app, tc = app_client()
    monkeypatch.setattr(webhook, "PCOClient", lambda cfg: pytest.fail("Should not access API"))
    assert signed_post(tc, envelope(name="people.v2.events.person.updated")).json()["deliveries"][0]["status"] == "ignored"


def test_stale_mapping_does_not_overwrite_local_event(session):
    with client() as api:
        sync_schedule(session, api, CONFIG)
    event = session.scalar(select(Event))
    event.gcal_event_id = None
    event.title = "Local event after a reset"
    session.flush()
    with client() as api:
        with pytest.raises(PlanningCenterError, match="stale"):
            sync_schedule(session, api, CONFIG)
    assert event.title == "Local event after a reset"


def test_per_event_subscription_secrets_and_reject_unknown_key(monkeypatch):
    from dataclasses import replace
    app, tc = app_client()
    app.state.pco_config = replace(CONFIG, webhook_secret='', webhook_secrets=('first-key', 'second-key'))
    monkeypatch.setattr(webhook, 'PCOClient', lambda cfg: pytest.fail('Irrelevant event must not call API'))
    raw = json.dumps(envelope(name='people.v2.events.person.updated')).encode()
    for key in ('unknown-key', 'second-key'):
        signature = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
        response = tc.post('/integrations/planning-center/webhook', content=raw, headers={'X-PCO-Webhooks-Authenticity': signature})
        assert response.status_code == (200 if key == 'second-key' else 401)
    assert 'second-key' not in repr(app.state.pco_config)

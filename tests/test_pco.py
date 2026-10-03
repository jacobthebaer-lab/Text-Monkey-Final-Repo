"""Planning Center integration against a faked PCO API (httpx.MockTransport).

No real network calls; live verification needs a PCO account (documented in
the README as a known gap until credentials exist).
"""

import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clock import FakeClock
from app.config import Settings
from app.db import models as m
from app.integrations import pco
from tests.conftest import NOW

PCO_SETTINGS = Settings(pco_app_id="app123", pco_secret="secret456", pco_webhook_secret="hook789")


def _jsonapi(data, next_offset=None):
    body = {"data": data}
    if next_offset is not None:
        body["meta"] = {"next": {"offset": next_offset}}
    return body


class FakePCO:
    """Canned PCO API: one service type, one upcoming plan, teams, people."""

    def __init__(self):
        self.scheduled: list[dict] = []
        self.unscheduled: list[str] = []
        self._next_tm = 9000

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/services/v2/service_types":
            return httpx.Response(200, json=_jsonapi([{"id": "st1", "attributes": {"name": "Sunday Service"}}]))
        if path == "/services/v2/service_types/st1/plans":
            return httpx.Response(200, json=_jsonapi([{"id": "p1", "attributes": {"title": None, "dates": "October 11"}}]))
        if path == "/services/v2/service_types/st1/plans/p1/plan_times":
            starts = (NOW + timedelta(days=10)).isoformat()
            ends = (NOW + timedelta(days=10, hours=1)).isoformat()
            return httpx.Response(200, json=_jsonapi(
                [{"id": "pt1", "attributes": {"starts_at": starts, "ends_at": ends, "time_type": "service"}}]
            ))
        if path == "/services/v2/service_types/st1/teams":
            return httpx.Response(200, json=_jsonapi([
                {"id": "team-ushers", "attributes": {"name": "Usher"}},
                {"id": "team-kids", "attributes": {"name": "Kids"}},
            ]))
        if path == "/services/v2/service_types/st1/plans/p1/team_members":
            payload = json.loads(request.content)
            self._next_tm += 1
            self.scheduled.append(payload["data"])
            return httpx.Response(201, json={"data": {"id": str(self._next_tm)}})
        if path.startswith("/services/v2/service_types/st1/plans/p1/team_members/"):
            self.unscheduled.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if path == "/calendar/v2/event_instances":
            starts = (NOW + timedelta(days=12)).isoformat()
            return httpx.Response(200, json=_jsonapi(
                [{"id": "ci1", "attributes": {"starts_at": starts, "ends_at": starts, "event_name": "Food Drive"}}]
            ))
        if path == "/people/v2/people":
            digits = request.url.params.get("where[search_phone_number]", "")
            if digits.endswith("0001"):
                return httpx.Response(200, json=_jsonapi([{"id": "person-1"}]))
            return httpx.Response(200, json=_jsonapi([]))
        return httpx.Response(404, json={"errors": [{"detail": f"no fake for {path}"}]})


@pytest.fixture
def fake_pco():
    return FakePCO()


@pytest.fixture
def client(fake_pco):
    http = httpx.Client(transport=httpx.MockTransport(fake_pco.handler), base_url=pco.API_BASE)
    return pco.PCOClient(PCO_SETTINGS, http=http)


def test_client_requires_configuration():
    with pytest.raises(pco.PCOError):
        pco.PCOClient(Settings())


def test_sync_events_upserts_and_matches_types(session, client):
    session.add(m.EventType(name="sunday_service", title_patterns=["Sunday Service"]))
    session.flush()
    clock = FakeClock(NOW)

    result = pco.sync_events(session, client, clock)
    assert result["created"] == 2  # plan time + calendar instance
    plan_event = session.scalar(select(m.Event).where(m.Event.pco_id == "st1/p1/pt1"))
    assert plan_event.title == "Sunday Service: October 11"
    assert plan_event.event_type_id is not None  # matched by title pattern
    cal_event = session.scalar(select(m.Event).where(m.Event.pco_id == "cal/ci1"))
    assert cal_event.title == "Food Drive"

    # Second sync updates in place, no duplicates.
    again = pco.sync_events(session, client, clock)
    assert again["created"] == 0 and again["updated"] == 2
    assert len(session.scalars(select(m.Event)).all()) == 2


def test_sync_generates_shifts_from_recipes(session, client):
    et = m.EventType(name="sunday_service", title_patterns=["Sunday Service"])
    role = m.Role(name="usher", ministry="hospitality", required_qualifications=[],
                  criticality="standard", fill_policy="auto")
    session.add_all([et, role])
    session.flush()
    session.add(m.RoleRecipe(event_type_id=et.id, role_id=role.id, count=2))
    session.flush()

    pco.sync_events(session, client, FakeClock(NOW))
    plan_event = session.scalar(select(m.Event).where(m.Event.pco_id == "st1/p1/pt1"))
    assert len(plan_event.shifts) == 2


def test_link_volunteers_by_phone(session, client, make_volunteer):
    linked_vol = make_volunteer()  # phone ends 0001 -> matches person-1
    unmatched = make_volunteer()
    assert pco.link_volunteers(session, client) == 1
    assert linked_vol.pco_person_id == "person-1"
    assert unmatched.pco_person_id is None


def test_push_roster_updates(session, client, fake_pco, make_volunteer, make_shift, assign):
    shift = make_shift("usher")
    shift.event.pco_id = "st1/p1/pt1"
    vol = make_volunteer()
    vol.pco_person_id = "person-1"
    unlinked = make_volunteer()
    assignment = assign(vol, shift, status="confirmed")
    skipped_assignment = assign(unlinked, shift, status="confirmed")
    session.flush()

    result = pco.push_roster_updates(session, client, FakeClock(NOW))
    assert result["pushed"] == 1 and result["skipped"] == 1
    assert assignment.pco_team_member_id is not None
    assert fake_pco.scheduled[0]["relationships"]["person"]["data"]["id"] == "person-1"
    assert fake_pco.scheduled[0]["relationships"]["team"]["data"]["id"] == "team-ushers"

    # Re-push is a no-op; cancellation unschedules.
    assert pco.push_roster_updates(session, client, FakeClock(NOW))["pushed"] == 0
    tm_id = assignment.pco_team_member_id
    assignment.status = "cancelled"
    session.flush()
    result = pco.push_roster_updates(session, client, FakeClock(NOW))
    assert result["removed"] == 1
    assert fake_pco.unscheduled == [tm_id]
    assert assignment.pco_team_member_id is None


def test_poll_if_due_respects_interval_and_config(session):
    clock = FakeClock(NOW)
    assert pco.poll_if_due(session, clock, Settings()) is None  # unconfigured: no-op

    pco._mark_synced(session, clock)
    clock.advance(timedelta(minutes=2))
    # Due check happens before any network call, so this returns None without
    # touching the (unreachable) real API.
    assert pco.poll_if_due(session, clock, PCO_SETTINGS) is None


# --- webhook ---------------------------------------------------------------------


def _signed_headers(body: bytes) -> dict:
    sig = hmac.new(b"hook789", body, hashlib.sha256).hexdigest()
    return {"X-PCO-Webhooks-Authenticity": sig}


@pytest.fixture
def web_app(tmp_path, fake_pco, monkeypatch):
    from app.main import create_app

    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/pco.db",
                              pco_app_id="app123", pco_secret="secret456",
                              pco_webhook_secret="hook789"))

    real_client = pco.PCOClient

    def patched_client(settings=None, http=None):
        return real_client(PCO_SETTINGS, http=httpx.Client(
            transport=httpx.MockTransport(fake_pco.handler), base_url=pco.API_BASE))

    monkeypatch.setattr(pco, "PCOClient", patched_client)
    return app


def test_webhook_valid_signature_triggers_sync(web_app):
    body = b'{"data": [{"attributes": {"name": "services.v2.events.plan.updated"}}]}'
    with TestClient(web_app) as c:
        resp = c.post("/pco/webhook", content=body, headers=_signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    with web_app.state.session_factory() as s:
        assert s.scalar(select(m.Event).where(m.Event.pco_id == "st1/p1/pt1")) is not None


def test_webhook_bad_signature_rejected(web_app):
    with TestClient(web_app) as c:
        resp = c.post("/pco/webhook", content=b"{}", headers={"X-PCO-Webhooks-Authenticity": "nope"})
    assert resp.status_code == 401
    with web_app.state.session_factory() as s:
        assert s.scalar(select(m.Event)) is None  # nothing synced


def test_webhook_unconfigured_returns_503(tmp_path):
    from app.main import create_app

    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/no.db"))
    with TestClient(app) as c:
        assert c.post("/pco/webhook", content=b"{}").status_code == 503

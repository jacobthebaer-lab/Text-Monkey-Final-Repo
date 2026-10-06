"""The web app: every page renders, and the full demo scenario runs in a
browser with no real SMS (Phase 5 done-when)."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import models as m
from app.db.seed import seed
from app.main import create_app
from tests.test_fill_agent import historical_invitation


class DemoGloo:
    """Fake Gloo for parsing, approved-copy composition and the fill tool loop."""

    def create_response(self, *, model, input, instructions=None, tools=None, **kwargs):
        usage = SimpleNamespace(input_tokens=40, output_tokens=15)
        if isinstance(input, str) and input.startswith('{'):
            facts = json.loads(input)
            if 'approved_message' in facts:
                return SimpleNamespace(output=[SimpleNamespace(type='message')],
                                       output_text=facts['approved_message'], usage=usage)
        if tools:  # agent loop: one ask per member, schedule, then done
            answered = any(
                isinstance(i, dict) and i.get("type") == "function_call_output"
                for i in input
            )
            if answered:
                return SimpleNamespace(
                    output=[SimpleNamespace(type="message")],
                    output_text="done",
                    usage=usage,
                )
            payload = json.loads(input[0]["content"])
            if 'flags' in payload:
                calls = [SimpleNamespace(type='function_call', call_id='capacity-'+str(fact['flag_id']),
                    name='narrate_flag', arguments=json.dumps({
                        'flag_id':fact['flag_id'], 'source_hash':fact['source_hash'],
                        'summary':fact['allowed_summaries'][0], 'suggested_action':fact['next_step']}))
                    for fact in payload['flags'] if fact['allowed_summaries']]
                return SimpleNamespace(output=calls, output_text=None, usage=usage)
            calls = [
                SimpleNamespace(
                    type="function_call",
                    call_id=f"c{i}",
                    name="request_send_text",
                    arguments=json.dumps(
                        {
                            "volunteer_id": member["volunteer_id"],
                            "body": f"Hi {member['name'].split()[0]}! Any chance you could cover "
                            f"{payload['shift']['role']} Sunday? No worries if not, reply YES or NO.",
                        }
                    ),
                )
                for i, member in enumerate(
                    payload["candidates"][: payload["max_candidates"]]
                )
            ]
            calls.insert(
                0,
                SimpleNamespace(
                    type="function_call",
                    call_id="choose",
                    name="choose_replacements",
                    arguments=json.dumps(
                        {
                            "volunteer_ids": [
                                c["volunteer_id"]
                                for c in payload["candidates"][
                                    : payload["max_candidates"]
                                ]
                            ],
                            "reason": "These volunteers match the available shift.",
                        }
                    ),
                ),
            )
            calls.append(
                SimpleNamespace(
                    type="function_call",
                    call_id="s",
                    name="schedule_next_tranche",
                    arguments="{}",
                )
            )
            return SimpleNamespace(output=calls, output_text=None, usage=usage)

        # Parser call: input is the raw SMS text.
        text = (input if isinstance(input, str) else str(input)).lower()
        if "cant" in text or "can't" in text:
            intent = "cancel"
        elif "yes" in text or text.strip() == "y":
            intent = "accept"
        elif "no" in text:
            intent = "decline"
        else:
            intent = "question"
        parsed = {
            "intent": intent,
            "shift_hint": "oct 11" if intent == "cancel" else None,
            "dates": [],
            "partial_window": None,
            "sensitive": False,
            "severity": "normal",
            "confidence": 0.95,
        }
        return SimpleNamespace(
            output=[SimpleNamespace(type="message")],
            output_text=json.dumps(parsed),
            usage=usage,
        )


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("webdb") / "web.db"
    app = create_app(Settings(database_url=f"sqlite:///{db_path}", demo_mode=True))
    with app.state.session_factory() as session:
        seed(session)
        session.commit()
    app.state.gloo = DemoGloo()
    with TestClient(app) as c:
        yield c


PAGES = [
    "/",
    "/approvals",
    "/schedule",
    "/needs",
    "/volunteers",
    "/flags",
    "/runs",
    "/simulator",
]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200


def test_schedule_shows_unknown_event(client):
    resp = client.get("/schedule?month=2026-10")
    assert "Fall Festival" in resp.text
    assert "unknown type" in resp.text


def test_full_demo_scenario_in_browser(client):
    app = client.app
    with app.state.session_factory() as session:
        jen = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Jen Hartley"))
        coordinator = session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))
        jen_id, coord_id = jen.id, coordinator.id

    # 1. Jen texts a nursery cancellation from the simulator.
    resp = client.post(f"/simulator/{jen_id}/send", data={"body": "cant make it oct 11, sorry!!"}, follow_redirects=False)
    assert resp.status_code == 303 and "routed=fill_agent" in resp.headers["location"]

    # 2. The gap and bounded search stay visible without outbound chatter.
    dash = client.get("/").text
    assert "nursery" in dash and "in_progress" in dash
    assert client.get("/approvals").status_code == 200
    with app.state.session_factory() as session:
        fill = session.scalar(select(m.FillRequest))
        assert fill.state == "in_progress"
        assert session.scalar(select(m.Approval).where(m.Approval.status == "pending")) is None
        assert session.scalar(select(m.Message).where(m.Message.direction == "out")) is None
        candidate = session.get(m.Volunteer, session.scalar(select(m.Outreach.volunteer_id)))
        candidate_id = candidate.id
        # Legacy web approval remains reviewable, but cannot restore outreach.
        legacy = m.Approval(kind="send_outreach", status="pending", requested_at=app.state.clock.now(),
            payload={"volunteer_id": candidate.id, "purpose": "outreach", "body": "Historical restricted draft"})
        session.add(legacy); session.commit(); legacy_id = legacy.id
    assert "Approve" in client.get("/approvals").text
    response = client.post(f"/approvals/{legacy_id}/approve", follow_redirects=False)
    assert response.status_code == 303
    with app.state.session_factory() as session:
        assert session.get(m.Approval, legacy_id).status == "approved"
        assert session.scalar(select(m.Message).where(m.Message.direction == "out")) is None
        fill = session.scalar(select(m.FillRequest))
        candidate = session.get(m.Volunteer, candidate_id)
        # Replay a genuinely delivered historical invitation, not a new send.
        historical_invitation(session, app.state.clock, candidate, fill)
        session.commit()

    # 4. The candidate sees the ask on their simulated phone and says yes.
    phone = client.get(f"/simulator?as={candidate_id}").text
    assert "Historical synthetic invitation" in phone
    client.post(f"/simulator/{candidate_id}/send", data={"body": "yes!!"}, follow_redirects=False)

    with app.state.session_factory() as session:
        fill = session.scalar(select(m.FillRequest))
        assert fill.state == "filled"

    # 5. The session log shows the runs, steps, and token counts.
    runs_page = client.get("/runs").text
    assert "fill_agent" in runs_page
    with app.state.session_factory() as session:
        run_id = session.scalar(select(m.AgentRun.id).order_by(m.AgentRun.id))
    detail = client.get(f"/runs/{run_id}").text
    assert "request_send_text" in detail or "decision" in detail

    # 6. The winner's phone shows the confirmation; nothing real was sent.
    winner_phone = client.get(f"/simulator?as={candidate_id}").text
    assert "confirmed" in winner_phone


def test_demo_advance_moves_clock(client):
    before = client.app.state.clock.now()
    resp = client.post("/demo/advance", data={"minutes": 20}, follow_redirects=False)
    assert resp.status_code == 303
    assert (client.app.state.clock.now() - before).total_seconds() == 20 * 60
    with client.app.state.session_factory() as session:
        flags = session.scalars(select(m.Flag)).all()
        assert flags and all(flag.evidence['narration']['state']=='ready' for flag in flags)


def test_recipe_update(client):
    with client.app.state.session_factory() as session:
        recipe_id = session.scalar(select(m.RoleRecipe.id))
    resp = client.post(f"/needs/recipe/{recipe_id}", data={"count": 5}, follow_redirects=False)
    assert resp.status_code == 303
    with client.app.state.session_factory() as session:
        assert session.get(m.RoleRecipe, recipe_id).count == 5


def test_demo_reset_restores_seed(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/reset.db", demo_mode=True))
    with app.state.session_factory() as session:
        seed(session)
        session.commit()
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            session.query(m.Volunteer).filter(m.Volunteer.name == "Jen Hartley").delete()
            session.commit()
        resp = c.post("/demo/reset", follow_redirects=False)
        assert resp.status_code == 303
        with app.state.session_factory() as session:
            assert session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Jen Hartley")) is not None


def test_admin_password_gates_pages_but_not_healthz(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/auth.db", admin_password="hunter2"))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("admin", "wrong")).status_code == 401
        assert c.get("/", auth=("admin", "hunter2")).status_code == 200


def test_demo_controls_hidden_outside_demo_mode(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/nodemo.db", demo_mode=False))
    with TestClient(app) as c:
        resp = c.post("/demo/advance", data={"minutes": 20}, follow_redirects=False)
        assert resp.status_code == 404

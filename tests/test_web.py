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
            calls = [
                SimpleNamespace(
                    type="function_call",
                    call_id=f"c{i}",
                    name="request_send_text",
                    arguments=json.dumps(
                        {
                            "volunteer_id": member["volunteer_id"],
                            "body": f"Hi {member['name'].split()[0]}! Any chance you could cover "
                            f"{payload['shift']['role']} Sunday? No worries if not — reply YES or NO.",
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

    # 2. The dashboard shows the fill request, waiting for approval (nursery).
    dash = client.get("/").text
    assert "nursery" in dash and "waiting_approval" in dash

    # 3. The approvals page lists the held asks; approve them on the web.
    approvals_page = client.get("/approvals").text
    assert "Approve" in approvals_page
    with app.state.session_factory() as session:
        approval_id = session.scalar(select(m.Approval.id).where(m.Approval.status == "pending"))
    assert approval_id is not None
    client.post(f"/approvals/{approval_id}/approve", follow_redirects=False)

    with app.state.session_factory() as session:
        fill = session.scalar(select(m.FillRequest))
        assert fill.state == "in_progress"
        outreach = session.scalars(select(m.Outreach).where(m.Outreach.message_id.isnot(None))).all()
        first_candidate = session.get(m.Volunteer, outreach[0].volunteer_id)
        candidate_id, candidate_name = first_candidate.id, first_candidate.name

    # 4. The candidate sees the ask on their simulated phone and says yes.
    phone = client.get(f"/simulator?as={candidate_id}").text
    assert "Any chance you could cover" in phone
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

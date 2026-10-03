"""Exercise shipped fictional CSVs through the existing parser and staging API."""
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.admin_setup.imports import FIELDS, parse_file, preview
from app.admin_setup.models import ImportBatch, StagedContact
from app.db import models as m
from tests.test_admin_setup import OWNER_B, save, setup_client  # noqa: F401

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "demo-import"
MAPPING = {"name": 0, "phone": 1, "email": 2, "ministry": 3}


def import_data(filename):
    return {"rows": parse_file(filename, (FIXTURES / filename).read_bytes())[0]["rows"],
            "mapping": MAPPING, "country": "US", "source": "Fictional Text Monkey demo package"}


def commit_preview(client, data):
    response = client.post("/api/setup/preview", json=data)
    assert response.status_code == 200, response.text
    request = {**data, "preview_hash": response.json()["preview_hash"], "submission_id": str(uuid4())}
    return client.post("/api/setup/import", json=request), request


def test_clean_package_contains_only_supported_fictional_staging_metadata():
    data = import_data("contacts-clean.csv")
    report, ready = preview(data["rows"], MAPPING, "US", data["source"])
    assert set(MAPPING) <= FIELDS
    assert report["counts"] == {"ready": 3, "duplicate": 0, "invalid": 0}
    assert [c["phone"] for c in ready] == ["+12025550111", "+12025550112", "+12025550113"]
    assert all(c["email"].endswith("@example.test") for c in ready)
    assert all(c["ministry"].endswith("(unverified note)") for c in ready)
    assert all(set(c) == {"name", "phone", "email", "ministry", "source"} for c in ready)
    assert all(row["consent"] == "not_recorded" for row in report["rows"])


def test_invalid_fixture_has_actionable_errors_and_cannot_stage(setup_client):
    client, app, _ = setup_client
    assert save(client).status_code == 200
    data = import_data("contacts-needs-fixes.csv")
    report = client.post("/api/setup/preview", json=data).json()
    assert report["counts"] == {"ready": 0, "duplicate": 0, "invalid": 3}
    assert [(r["row"], r["reason"]) for r in report["rows"]] == [
        (2, "Provide a name of 1–160 characters."),
        (3, "Check the email address or leave it blank."),
        (4, "Replace formulas/errors with plain text values."),
    ]
    response, _ = commit_preview(client, data)
    assert response.status_code == 422
    assert response.json()["detail"] == "There are no valid new contacts to save."
    assert client.get("/api/setup/contacts").json() == {"contacts": [], "texts_sent": 0}
    with app.state.session_factory() as session:
        assert session.scalar(select(StagedContact)) is None
        assert session.scalar(select(ImportBatch)) is None


def test_clean_fixture_stage_retry_duplicate_and_zero_live_permissions(setup_client):
    client, app, _ = setup_client
    assert save(client).status_code == 200
    data = import_data("contacts-clean.csv")
    response, request = commit_preview(client, data)
    assert response.status_code == 200, response.text
    assert response.json() == {"imported": 3, "counts": {"ready": 3, "duplicate": 0, "invalid": 0},
                               "consent": "not_recorded", "texts_sent": 0}
    assert client.post("/api/setup/import", json=request).json() == response.json()
    contacts = client.get("/api/setup/contacts").json()["contacts"]
    assert len(contacts) == 3
    assert all(c["status"] == "staged" and c["consent"] == "not_recorded" and not c["can_text"]
               for c in contacts)
    assert {c["ministry"] for c in contacts} == {row[3] for row in data["rows"][1:]}
    repeated = client.post("/api/setup/preview", json=data)
    assert repeated.json()["counts"] == {"ready": 0, "duplicate": 3, "invalid": 0}
    empty, _ = commit_preview(client, data)
    assert empty.status_code == 422
    assert client.get("/api/setup/contacts").json()["contacts"] == contacts
    with app.state.session_factory() as session:
        assert len(session.scalars(select(ImportBatch)).all()) == 1
        for cls in (m.Volunteer, m.Message, m.Assignment, m.Outreach, m.Approval):
            assert session.scalar(select(cls)) is None


def test_package_staging_is_account_scoped_and_new_account_is_empty(setup_client):
    client, app, user = setup_client
    data = import_data("contacts-clean.csv")
    assert client.post("/api/setup/preview", json=data).status_code == 409
    assert save(client).status_code == 200
    assert commit_preview(client, data)[0].status_code == 200
    account_a = client.get("/api/setup/contacts").json()
    user["id"] = OWNER_B
    assert client.get("/api/setup").json()["revision"] == 0
    assert client.get("/api/setup/contacts").json() == {"contacts": [], "texts_sent": 0}
    assert client.delete("/api/setup/contacts/" + account_a["contacts"][0]["id"]).status_code == 404
    assert save(client, {"church_name": "Second Fictional Church"}).status_code == 200
    assert client.post("/api/setup/preview", json=data).json()["counts"]["ready"] == 3
    assert commit_preview(client, data)[0].json()["texts_sent"] == 0
    user["id"] = "11111111-1111-4111-8111-111111111111"
    assert client.get("/api/setup/contacts").json() == account_a

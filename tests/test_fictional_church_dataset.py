"""Bounded fictional-fixture checks, no accounts/API/Gloo/device access."""
from datetime import datetime
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "tools" / "fictional_church_dataset.py"
spec = importlib.util.spec_from_file_location("fictional_church_dataset", MODULE)
dataset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dataset)


def test_manifest_is_bounded_fictional_unconsenting_and_correctly_scheduled():
    manifest = dataset.build_manifest()
    assert manifest["date_range"] == ["2026-10-04", "2026-10-31"]
    assert len(manifest["people"]) == 100
    assert len({p["phone"] for p in manifest["people"]}) == 100
    assert {p["phone"] for p in manifest["people"]} == {f"+120255501{i:02}" for i in range(100)}
    assert all(p["email"].endswith("@example.test") and "[Synthetic " in p["last_name"] for p in manifest["people"])
    assert all(p["sms_opt_in"] is False and p["status"] == "staged" for p in manifest["people"])
    assert len(manifest["events"]) == 24
    assert sum(sum(e["needs"].values()) for e in manifest["events"]) == 68
    sundays = [e for e in manifest["events"] if e["category"] == "sunday"]
    assert len(sundays) == 8
    assert {datetime.fromisoformat(e["starts_at"]).hour for e in sundays} == {9, 11}
    assert all(datetime.fromisoformat(e["starts_at"]).utcoffset().total_seconds() == -21600 for e in manifest["events"])
    assert manifest["protected_service_type_ids"] == ["1826236"]
    assert manifest["protected_plan_ids"] == ["92466235", "92466244"]
    changed = dataset.build_manifest()
    changed["people"][0]["sms_opt_in"] = True
    with pytest.raises(ValueError):
        dataset.validate_manifest(changed)


def test_future_manifest_preserves_local_hours_across_denver_dst():
    manifest = dataset.build_manifest(datetime(2026, 10, 25).date())
    sundays = [e for e in manifest["events"] if e["category"] == "sunday"]
    assert [datetime.fromisoformat(e["starts_at"]).hour for e in sundays] == [9, 11] * 4
    assert datetime.fromisoformat(sundays[0]["starts_at"]).utcoffset().total_seconds() == -21600
    assert datetime.fromisoformat(sundays[2]["starts_at"]).utcoffset().total_seconds() == -25200


class PeopleClient:
    def __init__(self):
        self.people = []
        self.included = {}
        self.posts = []

    def collection(self, path):
        if path == "/people/v2/people?where[status]=inactive":
            return self.people.copy()
        assert path.endswith("/phone_numbers")
        pid = path.split("/")[-2]
        return [p for p in self.included[pid] if p["type"] == "PhoneNumber"]

    def request(self, method, path, data=None, params=None):
        if method == "POST":
            assert path == "/people/v2/people"
            assert set(data["data"]["attributes"]) == {"first_name", "last_name", "status", "child"}
            assert data["data"]["attributes"]["status"] == "inactive"
            self.posts.append(path)
            person = {"id": str(len(self.people) + 1), **data["data"]}
            self.people.append(person)
            self.included[person["id"]] = data["included"]
            return {"data": person}
        assert method == "GET" and params == {"include": "emails,phone_numbers"}
        pid = path.split("/")[-1]
        return {"data": self.people[int(pid) - 1], "included": self.included[pid]}


def test_people_create_uses_inactive_directory_only_and_retry_reconciles():
    client = PeopleClient()
    manifest = dataset.build_manifest()
    assert dataset.apply_people(client, manifest, 100) == {"verified": 100, "created": 100, "services_enrollments": 0}
    assert dataset.apply_people(client, manifest, 100) == {"verified": 100, "created": 0, "services_enrollments": 0}
    assert len(client.posts) == len(client.people) == 100


def test_ambiguous_remote_people_stop_before_any_new_creation():
    client = PeopleClient()
    dataset.apply_people(client, dataset.build_manifest(), 1)
    client.people.append(client.people[0].copy())
    with pytest.raises(ValueError, match="Duplicate fictional"):
        dataset.apply_people(client, dataset.build_manifest(), 100)
    assert len(client.posts) == 1


def test_schedule_refuses_protected_live_fixture_even_if_named_as_dataset():
    class ProtectedClient:
        def collection(self, path):
            assert path == "/services/v2/service_types"
            return [{"id": "1826236", "attributes": {"name": dataset.PREFIX + " (Synthetic)"}}]
    with pytest.raises(ValueError, match="protected live-test service"):
        dataset.apply_schedule(ProtectedClient(), dataset.build_manifest())


def manifest_api(manifest, service_id="1826248", service_name=None):
    import httpx
    from app.integrations.planning_center import PCOClient, PCOConfig
    root = f"/services/v2/service_types/{service_id}"
    teams = {role: str(i + 1) for i, role in enumerate(dataset.ROLES)}
    resources = {
        "/services/v2": {"data": {"id": "545298", "attributes": {"name": dataset.PREFIX}}},
        root: {"data": {"id": service_id, "attributes": {"name": service_name or dataset.PREFIX + " (Synthetic)"}}},
        root + "/teams": {"data": [{"id": tid, "attributes": {"name": "Synthetic " + role}} for role, tid in teams.items()]},
        root + "/team_positions": {"data": [{"id": tid, "attributes": {"name": role}, "relationships": {"team": {"data": {"id": tid}}}} for role, tid in teams.items()]},
        root + "/plans": {"data": [{"id": str(i), "attributes": {"title": e["title"], "public": False, "reminders_disabled": True, "plan_people_count": 0}} for i, e in enumerate(manifest["events"], 1)]},
    }
    resources["/services/v2/service_types"] = {"data": [resources[root]["data"]]}
    for i, e in enumerate(manifest["events"], 1):
        path = root + f"/plans/{i}"
        resources[path] = {"data": resources[root + "/plans"]["data"][i-1]}
        plan_time = {"id": str(i), "attributes": {"name": e["key"], "time_type": "service", "starts_at": e["starts_at"], "ends_at": e["ends_at"]}}
        resources[path + "/plan_times"] = {"data": [plan_time]}
        resources[root + f"/plan_times/{i}"] = {"data": plan_time}
        resources[path + "/needed_positions"] = {"data": [{"id": str(j), "attributes": {"quantity": qty, "team_position_name": role}, "relationships": {"team": {"data": {"id": teams[role]}}}} for j, (role, qty) in enumerate(e["needs"].items(), 1)]}
    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, json=resources[request.url.path])
    return PCOClient(PCOConfig("synthetic", "synthetic", "545298", (service_id,)), transport=httpx.MockTransport(respond))


def test_completed_remote_schedule_reconciles_without_any_writes():
    manifest = dataset.build_manifest()
    with manifest_api(manifest) as client:
        result = dataset.apply_schedule(client, manifest)
    assert result["plans_created"] == result["times_created"] == result["needs_created"] == 0
    assert result["service_types_created"] == 0
    assert result["staffing_slots_verified"] == 68
    assert result["positions_needing_ui_setup"] == []
    assert len(result["plans"]) == 24


def test_local_actual_import_path_stages_100_and_repeats_without_permissions(tmp_path):
    manifest = dataset.build_manifest()
    db_path = tmp_path / "fictional-church.db"
    with manifest_api(manifest) as client:
        first = dataset.sync_local(client, manifest, "1826248", db_path)
        repeated = dataset.sync_local(client, manifest, "1826248", db_path)
    assert first["events"] == repeated["events"] == 24
    assert first["shifts"] == repeated["shifts"] == 68
    assert first["staged_contacts"] == repeated["staged_contacts"] == 100
    assert first["contact_preview"] == {"ready": 100, "duplicate": 0, "invalid": 0}
    assert repeated["contact_preview"] == {"ready": 0, "duplicate": 100, "invalid": 0}
    assert repeated["sync"]["events_created"] == repeated["sync"]["shifts_created"] == 0
    assert all(first[k] == repeated[k] == 0 for k in ["live_volunteers", "assignments", "messages", "outreach", "sms_consent_granted"])


def test_local_import_rejects_protected_foreign_and_unowned_targets(tmp_path):
    manifest = dataset.build_manifest()
    path = tmp_path / "fictional-church.db"
    with manifest_api(manifest) as client:
        with pytest.raises(ValueError, match="protected"):
            dataset.sync_local(client, manifest, dataset.PROTECTED_SERVICE, path)
    assert not path.exists()
    with manifest_api(manifest, service_name="Other service") as client:
        with pytest.raises(ValueError, match="outside"):
            dataset.sync_local(client, manifest, "1826248", path)
    assert not path.exists()
    path.write_text("not our database")
    with manifest_api(manifest) as client:
        with pytest.raises(ValueError, match="unowned"):
            dataset.sync_local(client, manifest, "1826248", path)
    assert path.read_text() == "not our database"


def test_local_import_rejects_symlink_destination_and_parent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    for path in [linked / "fictional-church.db", tmp_path / "wrong-name.db"]:
        with pytest.raises(ValueError, match="isolated"):
            dataset.isolated_database_path(path)
    destination = tmp_path / "fictional-church.db"
    destination.symlink_to(real / "fictional-church.db")
    with pytest.raises(ValueError, match="symlinks"):
        dataset.isolated_database_path(destination)


@pytest.mark.parametrize('start', [datetime(2026, 10, 5).date(), '2026-10-04'])
def test_manifest_does_not_label_a_non_sunday_as_sunday_service(start):
    with pytest.raises(ValueError, match='Sunday date'):
        dataset.build_manifest(start)

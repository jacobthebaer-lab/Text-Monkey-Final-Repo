"""Explicit, isolated fictional church dataset; never enrolls people or sends.

Dry-run requires no configuration. Apply requires an exact organization and
private env file; sync requires an explicitly new isolated SQLite destination.
The existing live-test service/plans are protected and are never mutated.
"""
import argparse
import csv
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import time as clock_time
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PREFIX = "Text Monkey Fictional Church"
TIMEZONE = "America/Denver"
START = date(2026, 10, 4)
END = date(2026, 10, 31)
PROTECTED_SERVICE = "1826236"
PROTECTED_PLANS = ("92466235", "92466244")
ROLES = {"Greeter": 2, "Usher": 2, "Production": 1, "Youth helper": 2,
         "Group host": 1, "Service volunteer": 3}
FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fictional-church"


def build_manifest(start=START):
    tz = ZoneInfo(TIMEZONE)
    first = ["Avery", "Jordan", "Morgan", "Riley", "Casey", "Taylor", "Quinn", "Alex", "Skyler", "Jamie"]
    last = ["Rowan", "Hayes", "Parker", "Bennett", "Rivera", "Brooks", "Ellis", "Chen", "Reed", "Finch"]
    people = [{"key": f"person-{i+1:03}", "first_name": first[i % 10],
               "last_name": f"{last[i // 10]} [Synthetic {i+1:03}]",
               "phone": f"+120255501{i:02}", "email": f"person{i+1:03}@example.test",
               "ministry": f"{list(ROLES)[i % len(ROLES)]} interest; unverified fictional note",
               "sms_opt_in": False, "status": "staged"} for i in range(100)]
    events = []

    def event(day, hour, minute, duration, title, category, needs):
        begins = datetime.combine(day, time(hour, minute), tz)
        events.append({"key": f"{day.isoformat()}-{hour:02}{minute:02}-{category}",
                       "title": f"Synthetic {title} — {day.isoformat()}", "category": category,
                       "starts_at": begins.isoformat(), "ends_at": (begins + timedelta(minutes=duration)).isoformat(),
                       "needs": needs})

    for week in range(4):
        sunday = start + timedelta(days=week * 7)
        for hour in [9, 11]:
            event(sunday, hour, 0, 60, f"Sunday Service {hour} AM", "sunday", {k: ROLES[k] for k in ["Greeter", "Usher", "Production"]})
        event(sunday + timedelta(days=3), 18, 30, 90, "Youth Night", "youth", {"Youth helper": 2})
        event(sunday + timedelta(days=2), 19, 0, 90, "Women's Group", "women", {"Group host": 1})
        event(sunday + timedelta(days=4), 19, 0, 90, "Men's Group", "men", {"Group host": 1})
    for offset, hour, duration, title in [(6, 9, 120, "Food Pantry Packing"), (13, 10, 120, "Neighborhood Cleanup"),
                                         (19, 18, 120, "Community Dinner"), (27, 14, 120, "Fall Family Festival")]:
        event(start + timedelta(days=offset), hour, 0, duration, title, "community", {"Service volunteer": 3})
    events.sort(key=lambda e: e["starts_at"])
    return {"schema_version": 1, "name": PREFIX, "synthetic": True, "timezone": TIMEZONE,
            "date_range": [start.isoformat(), (start + timedelta(days=27)).isoformat()],
            "protected_service_type_ids": [PROTECTED_SERVICE], "protected_plan_ids": list(PROTECTED_PLANS),
            "no_consent_no_delivery": True, "people": people, "events": events}


def validate_manifest(manifest):
    if manifest != build_manifest(date.fromisoformat(manifest["date_range"][0])):
        raise ValueError("Manifest must match the bounded generated fictional dataset")
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def write_fixtures(manifest):
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with (FIXTURES / "contacts.csv").open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Full name", "Mobile", "Email", "Team"])
        for person in manifest["people"]:
            writer.writerow([f"{person['first_name']} {person['last_name']}", person["phone"], person["email"], person["ministry"]])


def load_client(env_file, expected_org):
    from dotenv import dotenv_values
    from app.integrations.planning_center import PCOClient, PCOConfig, PlanningCenterError, ORIGIN
    values = dotenv_values(env_file)
    config = PCOConfig(app_id=values.get("PCO_APP_ID", ""), secret=values.get("PCO_SECRET", ""), organization_id=expected_org)

    class DatasetClient(PCOClient):
        def request(self, method, path, *, data=None, params=None):
            # Preserve the same-origin constraint without relying on global env.
            import httpx
            url = ORIGIN + path if path.startswith("/") else path
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != "api.planningcenteronline.com" or parsed.username or parsed.fragment:
                raise PlanningCenterError("Refused API link outside Planning Center")
            version = "2026-06-04" if parsed.path.startswith("/people/") else "2023-07-10" if parsed.path.startswith("/groups/") else "2018-11-01"
            for attempt in range(4):
                clock_time.sleep(0.4)  # Stay below the API's shared request limit.
                try:
                    response = self.http.request(method, url, json=data, params=params, headers={"X-PCO-API-Version": version})
                except httpx.HTTPError:
                    raise PlanningCenterError("Planning Center network request failed; reconcile before retry") from None
                if response.status_code != 429 or attempt == 3:
                    break
                try:
                    pause = float(response.headers.get("Retry-After", "20"))
                except ValueError:
                    pause = 20
                if not 0 <= pause <= 60:
                    raise PlanningCenterError("API rate limit requires a later retry")
                clock_time.sleep(pause)
            if not 200 <= response.status_code < 300:
                raise PlanningCenterError(f"Planning Center {method} returned HTTP {response.status_code}")
            return response.json()

    return DatasetClient(config)


def capacity(client, expected_org):
    org = client.organization()
    if str(org["id"]) != expected_org or org["attributes"]["time_zone"] != TIMEZONE:
        raise ValueError("Expected demo organization/timezone does not match")
    a = org["attributes"]
    return {"organization_id": str(org["id"]), "timezone": a["time_zone"],
            "services_people_allowed": a["people_allowed"], "services_people_remaining": a["people_remaining"],
            "services_people_count": len(client.collection("/services/v2/people"))}


def protected_snapshot(client):
    # Read only non-personal fixture state; never request private contact details.
    root = f"/services/v2/service_types/{PROTECTED_SERVICE}"
    return [{"id": pid, "attributes": {k: v for k, v in client.request("GET", root + f"/plans/{pid}")["data"]["attributes"].items()
                                       if k in ["title", "public", "reminders_disabled", "plan_people_count"]},
             "times": [{"id": t["id"], "attributes": {k: v for k, v in t["attributes"].items()
                        if k in ["name", "starts_at", "ends_at", "time_type"]}}
                       for t in client.collection(root + f"/plans/{pid}/plan_times")]}
            for pid in PROTECTED_PLANS]


def ensure(client, path, kind, key, attributes):
    matches = [r for r in client.collection(path) if r["attributes"].get(key) == attributes[key]]
    if len(matches) > 1:
        raise ValueError("Ambiguous duplicate fictional resources; review before continuing")
    if matches:
        return matches[0], False
    return client.create(path, kind, attributes), True


def apply_people(client, manifest, limit):
    existing = client.collection("/people/v2/people?where[status]=inactive")
    created = 0
    for processed, person in enumerate(manifest["people"][:limit], 1):
        matches = [p for p in existing if p["attributes"].get("first_name") == person["first_name"]
                   and p["attributes"].get("last_name") == person["last_name"]]
        if len(matches) > 1:
            raise ValueError("Duplicate fictional person names need review")
        if matches:
            saved = matches[0]
        else:
            payload = {"data": {"type": "Person", "attributes": {"first_name": person["first_name"],
                       "last_name": person["last_name"], "status": "inactive", "child": False}},
                       "included": [{"type": "Email", "attributes": {"address": person["email"], "location": "Home", "primary": True}},
                                    {"type": "PhoneNumber", "attributes": {"number": person["phone"], "location": "Mobile", "primary": True}}]}
            saved = client.request("POST", "/people/v2/people", data=payload)["data"]
            existing.append(saved)
            created += 1
        detail = client.request("GET", f"/people/v2/people/{saved['id']}", params={"include": "emails,phone_numbers"})
        if detail["data"]["attributes"]["status"] != "inactive":
            raise ValueError("Fictional person must stay inactive")
        included = detail.get("included", [])
        if not any(r["type"] == "Email" and r["attributes"].get("address") == person["email"] for r in included):
            raise ValueError("Saved reserved email did not verify")
        phones = client.collection(f"/people/v2/people/{saved['id']}/phone_numbers")
        if not any(r["attributes"].get("e164") == person["phone"] or r["attributes"].get("number", "").replace("+", "").replace("-", "").replace("(", "").replace(")", "").replace(" ", "") in [person["phone"].lstrip("+"), person["phone"][2:]] for r in phones):
            raise ValueError("Saved reserved phone did not verify")
        if processed % 20 == 0:
            print(json.dumps({"progress": "fictional People directory records verified", "processed": person["key"]}), flush=True)
    return {"verified": limit, "created": created, "services_enrollments": 0}


def apply_schedule(client, manifest):
    service, service_new = ensure(client, "/services/v2/service_types", "ServiceType", "name",
                                  {"name": PREFIX + " (Synthetic)", "frequency": "weekly", "scheduled_publish": False})
    if str(service["id"]) == PROTECTED_SERVICE:
        raise ValueError("Refused protected live-test service")
    root = f"/services/v2/service_types/{service['id']}"
    teams = {}
    for role in ROLES:
        team, _ = ensure(client, root + "/teams", "Team", "name",
                         {"name": f"Synthetic {role}", "schedule_to": "plan", "assigned_directly": True})
        teams[role] = team
    report = {"service_type_id": str(service["id"]), "service_types_created": int(service_new),
              "plans_created": 0, "times_created": 0, "needs_created": 0, "plans": [], "teams": {k: str(v["id"]) for k,v in teams.items()}}
    positions = client.collection(root + "/team_positions")
    for event in manifest["events"]:
        # Services UI onboarding creates an unnamed empty first plan. Adopt only
        # that exact matching fixture time within our new, isolated service.
        plans = client.collection(root + "/plans")
        if not any(p["attributes"].get("title") == event["title"] for p in plans):
            candidates = []
            for p in plans:
                if p["attributes"].get("title") or p["attributes"].get("plan_people_count"):
                    continue
                times = client.collection(root + f"/plans/{p['id']}/plan_times")
                if len(times) == 1 and datetime.fromisoformat(times[0]["attributes"]["starts_at"].replace("Z", "+00:00")) == datetime.fromisoformat(event["starts_at"]):
                    candidates.append(p)
            if len(candidates) > 1:
                raise ValueError("Ambiguous empty onboarding plans need review")
            if candidates:
                p = candidates[0]
                client.request("PATCH", root + f"/plans/{p['id']}", data={"data": {"type": "Plan", "id": p["id"],
                               "attributes": {"title": event["title"], "public": False, "reminders_disabled": True, "series_title": PREFIX}}})
        plan, new = ensure(client, root + "/plans", "Plan", "title",
                           {"title": event["title"], "public": False, "series_title": PREFIX})
        if str(plan["id"]) in PROTECTED_PLANS:
            raise ValueError("Refused protected live-test plan")
        report["plans_created"] += int(new)
        path = root + f"/plans/{plan['id']}"
        if not plan["attributes"].get("reminders_disabled"):
            client.request("PATCH", path, data={"data": {"type": "Plan", "id": plan["id"], "attributes": {"reminders_disabled": True}}})
        saved_plan = client.request("GET", path)["data"]["attributes"]
        if saved_plan.get("public") or not saved_plan.get("reminders_disabled") or saved_plan.get("plan_people_count"):
            raise ValueError("Saved synthetic plan must remain private, unscheduled and reminders disabled")
        times = client.collection(path + "/plan_times")
        wanted = [t for t in times if t["attributes"].get("name") == event["key"]]
        if len(wanted) > 1:
            raise ValueError("Duplicate fictional service times need review")
        if not wanted:
            matching = [t for t in times if t["attributes"].get("time_type") == "service" and
                        datetime.fromisoformat(t["attributes"]["starts_at"].replace("Z", "+00:00")) == datetime.fromisoformat(event["starts_at"])]
            if len(matching) > 1:
                raise ValueError("Ambiguous matching fixture service times")
            attributes = {"name": event["key"], "time_type": "service",
                          "starts_at": datetime.fromisoformat(event["starts_at"]).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                          "ends_at": datetime.fromisoformat(event["ends_at"]).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "team_reminders": []}
            if matching:
                saved = matching[0]
                client.request("PATCH", root + f"/plan_times/{saved['id']}", data={"data": {"type": "PlanTime", "id": saved["id"], "attributes": attributes}})
            else:
                saved = client.create(path + "/plan_times", "PlanTime", attributes)
                report["times_created"] += 1
        else:
            saved = wanted[0]
        actual = client.request("GET", root + f"/plan_times/{saved['id']}")["data"]["attributes"]
        for key in ["starts_at", "ends_at"]:
            if datetime.fromisoformat(actual[key].replace("Z", "+00:00")) != datetime.fromisoformat(event[key]):
                raise ValueError("Server-saved event time differs from Denver source; stop before importing")
        needs = client.collection(path + "/needed_positions")
        for role, quantity in event["needs"].items():
            team = teams[role]
            matching = [n for n in needs if (n.get("relationships", {}).get("team", {}).get("data") or {}).get("id") == team["id"]]
            if len(matching) > 1 or (matching and matching[0]["attributes"].get("quantity") != quantity):
                raise ValueError("Existing fictional staffing needs differ; review before continuing")
            if not matching:
                available = [p for p in positions if (p.get("relationships", {}).get("team", {}).get("data") or {}).get("id") == team["id"]]
                if len(available) > 1:
                    raise ValueError("Ambiguous fictional team positions need review")
                if available:
                    position = available[0]
                    client.create(path + "/needed_positions", "NeededPosition", {"quantity": quantity, "team_id": str(team["id"]), "team_position_id": str(position["id"])})
                    report["needs_created"] += 1
        report["plans"].append({"key": event["key"], "plan_id": str(plan["id"]), "time_id": str(saved["id"])})
    report["positions_needing_ui_setup"] = [role for role, team in teams.items() if not any((p.get("relationships", {}).get("team", {}).get("data") or {}).get("id") == team["id"] for p in positions)]
    if len(client.collection(root + "/plans")) != len(manifest["events"]):
        raise ValueError("Synthetic service contains unexpected plans; stop before importing")
    if not report["positions_needing_ui_setup"]:
        verified_slots = 0
        for event, saved in zip(manifest["events"], report["plans"]):
            needs = client.collection(root + f"/plans/{saved['plan_id']}/needed_positions")
            wanted = {str(teams[role]["id"]): quantity for role, quantity in event["needs"].items()}
            actual = {(n.get("relationships", {}).get("team", {}).get("data") or {}).get("id"): n["attributes"].get("quantity") for n in needs}
            if len(needs) != len(wanted) or actual != wanted:
                raise ValueError("Server-saved staffing needs differ from manifest")
            verified_slots += sum(actual.values())
        report["staffing_slots_verified"] = verified_slots
    return report


def isolated_database_path(db_path):
    db_path = db_path.absolute()
    if db_path.name != "fictional-church.db" or any(p.is_symlink() for p in [db_path, *db_path.parents]):
        raise ValueError("Use an explicit isolated fictional-church.db path without symlinks")
    return db_path


def sync_local(client, manifest, service_id, db_path):
    from sqlalchemy import select, func
    from app.admin_setup.imports import parse_file, preview
    from app.admin_setup.models import SetupBase, Workspace, StagedContact
    from app.db import models as m
    from app.db.session import init_db, make_engine, make_session_factory
    from app.integrations.planning_center import PCOBase, sync_schedule
    db_path = isolated_database_path(db_path)
    if service_id == PROTECTED_SERVICE:
        raise ValueError("Refused protected service import")
    service = client.request("GET", f"/services/v2/service_types/{service_id}")["data"]
    if str(service["id"]) != service_id or service["attributes"].get("name") != PREFIX + " (Synthetic)":
        raise ValueError("Refused service outside the explicitly named synthetic dataset")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    marker = db_path.with_suffix(".scope.json")
    digest = validate_manifest(manifest)
    if db_path.exists() and (not marker.exists() or json.loads(marker.read_text()) != {"fixture_sha256": digest, "service_type_id": service_id}):
        raise ValueError("Refused existing unowned database")
    if not db_path.exists():
        marker.write_text(json.dumps({"fixture_sha256": digest, "service_type_id": service_id}))
    engine = make_engine("sqlite:///" + str(db_path))
    init_db(engine)
    PCOBase.metadata.create_all(engine)
    SetupBase.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        for model in [m.Volunteer, m.Assignment, m.Message, m.Outreach, m.Approval, m.Notification]:
            if session.scalar(select(func.count()).select_from(model)):
                raise ValueError("Fixture destination contains runtime/person records; stop")
        config = replace(client.config, service_type_ids=(service_id,))
        sync = sync_schedule(session, client, config)
        owner_id = "00000000-0000-4000-8000-000000000100"
        workspace = session.scalar(select(Workspace).where(Workspace.owner_id == owner_id))
        if not workspace:
            workspace = Workspace(id=str(uuid4()), owner_id=owner_id, completed=False, revision=1,
                                  details={"church_name": PREFIX + " (Synthetic)", "timezone": TIMEZONE}, updated_at=datetime.now(timezone.utc))
            session.add(workspace)
            session.flush()
        rows = parse_file("contacts.csv", (FIXTURES / "contacts.csv").read_bytes())[0]["rows"]
        existing = session.scalars(select(StagedContact.phone).where(StagedContact.workspace_id == workspace.id)).all()
        report, ready = preview(rows, {"name": 0, "phone": 1, "email": 2, "ministry": 3}, "US", PREFIX + " synthetic fixture", existing)
        for contact in ready:
            session.add(StagedContact(id=str(uuid4()), workspace_id=workspace.id, created_at=datetime.now(timezone.utc), **contact))
        session.flush()
        events = session.scalars(select(m.Event).order_by(m.Event.starts_at)).all()
        if len(events) != len(manifest["events"]):
            raise ValueError("Local event count differs from manifest")
        expected = {(e["title"], datetime.fromisoformat(e["starts_at"]).astimezone(timezone.utc), datetime.fromisoformat(e["ends_at"]).astimezone(timezone.utc)) for e in manifest["events"]}
        if {(e.title, e.starts_at, e.ends_at) for e in events} != expected:
            raise ValueError("Local schedule differs from actual verified Denver fixture")
        shifts = session.scalar(select(func.count()).select_from(m.Shift))
        if shifts != sum(sum(e["needs"].values()) for e in manifest["events"]):
            raise ValueError("Local staffing count differs from manifest; finish remote positions before importing")
        if session.scalar(select(func.count()).select_from(StagedContact)) != len(manifest["people"]):
            raise ValueError("Local staged contact count differs from manifest")
        session.commit()
        result = {"sync": sync, "contact_preview": report["counts"], "staged_contacts": session.scalar(select(func.count()).select_from(StagedContact)),
                  "events": len(events), "shifts": shifts,
                  "live_volunteers": 0, "assignments": 0, "messages": 0, "outreach": 0, "sms_consent_granted": 0}
    engine.dispose()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["dry-run", "capacity", "people", "schedule", "sync"])
    parser.add_argument("--env-file")
    parser.add_argument("--expected-org")
    parser.add_argument("--people-limit", type=int, default=100)
    parser.add_argument("--write-synthetic", action="store_true")
    parser.add_argument("--service-type-id")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    manifest = build_manifest()
    digest = validate_manifest(manifest)
    if args.command == "dry-run":
        write_fixtures(manifest)
        print(json.dumps({"fixture_sha256": digest, "people": 100, "events": 24,
                          "desired_shifts": sum(sum(e["needs"].values()) for e in manifest["events"]), "date_range": manifest["date_range"],
                          "timezone": TIMEZONE, "remote_writes": 0}, indent=2))
        return
    if not args.env_file or not args.expected_org or not args.expected_org.isdigit():
        parser.error("Use existing private --env-file and explicit numeric --expected-org")
    if args.command in ["people", "schedule"] and not args.write_synthetic:
        parser.error("Remote creation requires --write-synthetic")
    if not 1 <= args.people_limit <= 100:
        parser.error("People limit must be 1–100")
    with load_client(args.env_file, args.expected_org) as client:
        before = capacity(client, args.expected_org)
        protected_before = protected_snapshot(client)
        result = {"verified_at": datetime.now(timezone.utc).isoformat(), "fixture_sha256": digest,
                  "capacity_before": before, "date_range": manifest["date_range"], "timezone": TIMEZONE}
        if args.command == "people":
            result["people"] = apply_people(client, manifest, args.people_limit)
        elif args.command == "schedule":
            result["schedule"] = apply_schedule(client, manifest)
        elif args.command == "sync":
            if not args.service_type_id or not args.service_type_id.isdigit() or not args.database:
                parser.error("Sync needs --service-type-id and explicit --database fictional-church.db")
            result["local"] = sync_local(client, manifest, args.service_type_id, args.database)
        result["capacity_after"] = capacity(client, args.expected_org)
        if result["capacity_after"] != before:
            raise ValueError("Services roster/capacity changed; stop before further bulk creation")
        result["protected_live_fixture_unchanged"] = protected_snapshot(client) == protected_before
        if not result["protected_live_fixture_unchanged"]:
            raise ValueError("Protected fixture changed concurrently; reconcile before proceeding")
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

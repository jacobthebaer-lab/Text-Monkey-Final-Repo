"""Explicit demo setup and real Services API verification, with sanitized output."""
import argparse
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from app.integrations.planning_center import PCOBase, PCOClient, PCOConfig, PlanningCenterError, sync_schedule
from app.db.session import init_db, make_engine, make_session_factory

DEMO_SERVICE = "Text Monkey Synthetic Demo"
TEAM_NAMES = ("Demo Greeters", "Demo Ushers", "Demo Production")


def ensure(client, path, kind, name, attributes):
    key = "title" if kind == "Plan" else "name"
    matches = [r for r in client.collection(path) if r["attributes"].get(key) == name]
    if len(matches) > 1:
        raise PlanningCenterError("Duplicate named demo resources require review")
    if matches:
        return matches[0]
    return client.create(path, kind, {key: name, **attributes})


def seed(client, expected_org):
    org = client.organization()
    if str(org["id"]) != expected_org:
        raise PlanningCenterError("Organization differs from explicitly selected demo organization")
    service = ensure(client, "/services/v2/service_types", "ServiceType", DEMO_SERVICE,
                     {"frequency": "weekly", "scheduled_publish": False})
    root = f"/services/v2/service_types/{service['id']}"
    teams = [ensure(client, root + "/teams", "Team", name, {"schedule_to": "plan", "assigned_directly": True}) for name in TEAM_NAMES]
    today = datetime.now(ZoneInfo("America/Denver")).date()
    sunday = today + timedelta(days=(6 - today.weekday()) % 7)
    if sunday == today:
        sunday += timedelta(days=7)
    plans = []
    for offset in (0, 7):
        day = sunday + timedelta(days=offset)
        title = f"Synthetic Sunday Service {day.isoformat()}"
        plan = ensure(client, root + "/plans", "Plan", title, {"public": False, "series_title": "Text Monkey Demo"})
        path = root + f"/plans/{plan['id']}"
        client.request("PATCH", path, data={"data": {"type": "Plan", "id": plan["id"], "attributes": {"reminders_disabled": True}}})
        start = datetime.combine(day, time(9), ZoneInfo("America/Denver"))
        times = client.collection(path + "/plan_times")
        wanted = [t for t in times if t["attributes"].get("name") == "Synthetic 9 AM service"]
        if not wanted:
            client.create(path + "/plan_times", "PlanTime", {"name": "Synthetic 9 AM service", "time_type": "service",
                          "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat(), "team_reminders": []})
        plans.append({"id": plan["id"], "title": title})
    # Confirm server-saved results using fresh GETs, not creation responses alone.
    actual_teams = client.collection(root + "/teams")
    actual_plans = client.collection(root + "/plans")
    if not all(any(t["id"] == team["id"] for t in actual_teams) for team in teams):
        raise PlanningCenterError("Created teams did not verify")
    for plan in plans:
        actual = next((p for p in actual_plans if p["id"] == plan["id"]), None)
        times = client.collection(root + f"/plans/{plan['id']}/plan_times")
        if not actual or not actual["attributes"].get("reminders_disabled") or not any(t["attributes"].get("time_type") == "service" for t in times):
            raise PlanningCenterError("Saved plan or service time did not verify")
    return {"organization_id": str(org["id"]), "service_type_id": service["id"],
            "teams": [{"id": t["id"], "name": t["attributes"]["name"]} for t in teams], "plans": plans,
            "notifications": "No people scheduled; reminders disabled; plans private"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("inspect", "seed", "sync"))
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--expected-org", required=True, help="Verified numeric Planning Center organization ID")
    ap.add_argument("--database", help="Explicit isolated sqlite:///...demo.db destination for sync")
    ap.add_argument("--write-synthetic", action="store_true", help="Required to create the named synthetic demo resources")
    args = ap.parse_args()
    load_dotenv(args.env_file, override=True)
    config = PCOConfig.from_env()
    if not args.expected_org.isdigit():
        ap.error("--expected-org must be numeric")
    with PCOClient(config) as client:
        org = client.organization()
        if str(org["id"]) != args.expected_org:
            raise PlanningCenterError("Connected organization differs from --expected-org")
        if args.command == "inspect":
            result = {"organization_id": org["id"], "organization_name": org["attributes"].get("name"),
                      "service_types": [{"id": s["id"], "name": s["attributes"].get("name")} for s in client.collection("/services/v2/service_types")]}
        elif args.command == "seed":
            if not args.write_synthetic:
                ap.error("seed requires --write-synthetic")
            result = seed(client, args.expected_org)
        else:
            if config.organization_id != args.expected_org:
                ap.error("PCO_ORGANIZATION_ID must match --expected-org")
            if not args.database or not args.database.startswith("sqlite:///") or not args.database.endswith(".db"):
                ap.error("sync requires an explicit isolated SQLite demo .db destination")
            engine = make_engine(args.database)
            init_db(engine)
            PCOBase.metadata.create_all(engine)
            with make_session_factory(engine)() as session:
                result = sync_schedule(session, client, config)
                session.commit()
            engine.dispose()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except PlanningCenterError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

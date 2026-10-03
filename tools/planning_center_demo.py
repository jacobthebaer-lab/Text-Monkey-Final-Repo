"""Explicit demo setup and real Services API verification, with sanitized output."""
import argparse
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from app.integrations.planning_center import PCOBase, PCOClient, PCOConfig, PlanningCenterError, sync_schedule, _time, relation
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
    try:
        service = ensure(client, "/services/v2/service_types", "ServiceType", DEMO_SERVICE,
                         {"frequency": "weekly", "scheduled_publish": False})
    except PlanningCenterError as exc:
        if "HTTP 500" in str(exc):
            raise PlanningCenterError("Planning Center first-Service-Type API creation failed; create Text Monkey Synthetic Demo once in Services onboarding, then rerun seed") from None
        raise
    root = f"/services/v2/service_types/{service['id']}"
    teams = [ensure(client, root + "/teams", "Team", name, {"schedule_to": "plan", "assigned_directly": True}) for name in TEAM_NAMES]
    today = datetime.now(ZoneInfo("America/Denver")).date()
    sunday = today + timedelta(days=(6 - today.weekday()) % 7)
    if sunday == today:
        sunday += timedelta(days=7)
    positions = client.collection(root + "/team_positions")
    missing_positions = [t["attributes"]["name"] for t in teams if not any(str(relation(p, "team")) == str(t["id"]) for p in positions)]
    plans = []
    for offset in (0, 7):
        day = sunday + timedelta(days=offset)
        title = f"Synthetic Sunday Service {day.isoformat()}"
        start = datetime.combine(day, time(9), ZoneInfo("America/Denver"))
        candidates = []
        for existing in client.collection(root + "/plans"):
            if existing["attributes"].get("title") or existing["attributes"].get("plan_people_count"):
                continue
            existing_times = client.collection(root + f"/plans/{existing['id']}/plan_times")
            if any(t["attributes"].get("time_type") == "service" and _time(t["attributes"].get("starts_at")) == start for t in existing_times):
                candidates.append(existing)
        if len(candidates) > 1:
            raise PlanningCenterError("Ambiguous empty onboarding plans require review")
        plan = candidates[0] if candidates else ensure(client, root + "/plans", "Plan", title, {"public": False, "series_title": "Text Monkey Demo"})
        path = root + f"/plans/{plan['id']}"
        client.request("PATCH", path, data={"data": {"type": "Plan", "id": plan["id"], "attributes": {"title": title, "public": False, "series_title": "Text Monkey Demo", "reminders_disabled": True}}})
        start = datetime.combine(day, time(9), ZoneInfo("America/Denver"))
        times = client.collection(path + "/plan_times")
        wanted = [t for t in times if t["attributes"].get("name") == "Synthetic 9 AM service"]
        matching_time = next((t for t in times if t["attributes"].get("time_type") == "service" and _time(t["attributes"].get("starts_at")) == start), None)
        if not wanted and matching_time:
            client.request("PATCH", root + f"/plan_times/{matching_time['id']}", data={"data": {"type": "PlanTime", "id": matching_time["id"], "attributes": {"name": "Synthetic 9 AM service", "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat(), "team_reminders": []}}})
        elif not wanted:
            client.create(path + "/plan_times", "PlanTime", {"name": "Synthetic 9 AM service", "time_type": "service",
                          "starts_at": start.isoformat(), "ends_at": (start + timedelta(hours=1)).isoformat(), "team_reminders": []})
        existing_needs = client.collection(path + "/needed_positions")
        for team, quantity in zip(teams, (2, 2, 1)):
            team_positions = [p for p in positions if str(relation(p, "team")) == str(team["id"])]
            if len(team_positions) > 1:
                raise PlanningCenterError("Demo team has multiple positions; review before seeding open needs")
            if team_positions and not any(str(relation(n, "team")) == str(team["id"]) for n in existing_needs):
                # Plan-wide teams require team_position_id and forbid time_id.
                client.create(path + "/needed_positions", "NeededPosition",
                              {"quantity": quantity, "team_position_id": team_positions[0]["id"]},
                              {"team": {"data": {"type": "Team", "id": team["id"]}}})
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
            "teams_needing_ui_position_setup": missing_positions,
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

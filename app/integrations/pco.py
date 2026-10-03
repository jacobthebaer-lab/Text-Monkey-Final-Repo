"""Planning Center integration: two-way sync with Services + Calendar.

- Pull: upcoming Services plans (and their times) become our events, matched
  to event types by title patterns; Calendar event instances likewise.
- Link: volunteers are matched to PCO people by phone number.
- Push: roster changes made by the text agent (fills, cancellations) are
  pushed to the PCO plan as team-member schedule/unschedule.
- Live: PCO webhooks (HMAC-validated) trigger an immediate re-sync, and a
  polling sync runs as a fallback.

Auth is a Personal Access Token pair (PCO_APP_ID / PCO_SECRET) over HTTP
Basic against https://api.planningcenteronline.com. Everything no-ops
cleanly when PCO is not configured, and sync failures never break the SMS
flow — they log and escalate, matching the Gloo failure philosophy.
"""

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import Settings, get_settings
from app.db import models as m

logger = logging.getLogger("pco")

API_BASE = "https://api.planningcenteronline.com"
SYNC_HORIZON_DAYS = 60
PCO_SYNC_STATE_KEY = "pco_sync_state"
POLL_INTERVAL = timedelta(minutes=5)
PUSHED_STATUSES = ("approved", "confirmed")


class PCOError(Exception):
    pass


class PCOClient:
    """Thin JSON:API client. `http` is injectable for tests."""

    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.pco_enabled:
            raise PCOError("Planning Center is not configured (PCO_APP_ID / PCO_SECRET)")
        self._http = http or httpx.Client(
            base_url=API_BASE,
            auth=(self.settings.pco_app_id, self.settings.pco_secret),
            timeout=20.0,
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self._http.get(path, params=params)
        if response.status_code >= 400:
            raise PCOError(f"PCO GET {path} -> {response.status_code}: {response.text[:200]}")
        return response.json()

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Follow JSON:API offset pagination."""
        out: list[dict] = []
        params = dict(params or {})
        params.setdefault("per_page", 100)
        offset = 0
        while True:
            params["offset"] = offset
            body = self._get(path, params)
            data = body.get("data", [])
            out.extend(data)
            next_offset = (body.get("meta") or {}).get("next", {}).get("offset")
            if next_offset is None:
                return out
            offset = next_offset

    def _post(self, path: str, payload: dict) -> dict:
        response = self._http.post(path, json=payload)
        if response.status_code >= 400:
            raise PCOError(f"PCO POST {path} -> {response.status_code}: {response.text[:200]}")
        return response.json()

    def _delete(self, path: str) -> None:
        response = self._http.delete(path)
        if response.status_code >= 400 and response.status_code != 404:
            raise PCOError(f"PCO DELETE {path} -> {response.status_code}")

    # -- Services ---------------------------------------------------------------

    def service_types(self) -> list[dict]:
        if self.settings.pco_service_type_id:
            return [{"id": self.settings.pco_service_type_id}]
        return self._get_all("/services/v2/service_types")

    def upcoming_plans(self, service_type_id: str) -> list[dict]:
        return self._get_all(
            f"/services/v2/service_types/{service_type_id}/plans",
            {"filter": "future", "include": "plan_times"},
        )

    def plan_times(self, service_type_id: str, plan_id: str) -> list[dict]:
        return self._get_all(
            f"/services/v2/service_types/{service_type_id}/plans/{plan_id}/plan_times"
        )

    def teams(self, service_type_id: str) -> list[dict]:
        return self._get_all(f"/services/v2/service_types/{service_type_id}/teams")

    def schedule_person(
        self, service_type_id: str, plan_id: str, person_id: str, team_id: str
    ) -> str:
        """Add a confirmed team member to a plan; returns the team_member id."""
        body = self._post(
            f"/services/v2/service_types/{service_type_id}/plans/{plan_id}/team_members",
            {
                "data": {
                    "type": "PlanPerson",
                    "attributes": {"status": "C"},
                    "relationships": {
                        "person": {"data": {"type": "Person", "id": person_id}},
                        "team": {"data": {"type": "Team", "id": team_id}},
                    },
                }
            },
        )
        return body["data"]["id"]

    def unschedule(self, service_type_id: str, plan_id: str, team_member_id: str) -> None:
        self._delete(
            f"/services/v2/service_types/{service_type_id}/plans/{plan_id}/team_members/{team_member_id}"
        )

    # -- Calendar ----------------------------------------------------------------

    def calendar_instances(self, start: datetime, end: datetime) -> list[dict]:
        return self._get_all(
            "/calendar/v2/event_instances",
            {
                "where[starts_at][gte]": start.isoformat(),
                "where[starts_at][lte]": end.isoformat(),
                "include": "event",
            },
        )

    # -- People ------------------------------------------------------------------

    def find_person_by_phone(self, phone: str) -> str | None:
        digits = "".join(c for c in phone if c.isdigit())[-10:]
        body = self._get(
            "/people/v2/people", {"where[search_phone_number]": digits, "per_page": 2}
        )
        data = body.get("data", [])
        return data[0]["id"] if len(data) == 1 else None


# --- sync: PCO -> us ------------------------------------------------------------


def _match_event_type(session: Session, title: str) -> int | None:
    for et in session.scalars(select(m.EventType)):
        for pattern in et.title_patterns:
            if pattern.lower() in title.lower():
                return et.id
    return None


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _upsert_event(session: Session, pco_id: str, title: str, starts, ends) -> tuple[m.Event, bool]:
    event = session.scalar(select(m.Event).where(m.Event.pco_id == pco_id))
    created = event is None
    if event is None:
        event = m.Event(pco_id=pco_id, title=title, starts_at=starts, ends_at=ends,
                        event_type_id=_match_event_type(session, title), status="scheduled")
        session.add(event)
    else:
        event.title = title
        event.starts_at = starts
        event.ends_at = ends
    session.flush()
    return event, created


def sync_events(session: Session, client: PCOClient, clock: Clock) -> dict:
    """Pull upcoming Services plans and Calendar instances into our events."""
    now = clock.now()
    horizon = now + timedelta(days=SYNC_HORIZON_DAYS)
    created = updated = 0

    for service_type in client.service_types():
        st_id = service_type["id"]
        st_name = (service_type.get("attributes") or {}).get("name", "Service")
        for plan in client.upcoming_plans(st_id):
            plan_id = plan["id"]
            attrs = plan.get("attributes") or {}
            title = attrs.get("title") or attrs.get("dates") or st_name
            for pt in client.plan_times(st_id, plan_id):
                pt_attrs = pt.get("attributes") or {}
                if pt_attrs.get("time_type") not in (None, "service"):
                    continue
                starts = _parse_dt(pt_attrs["starts_at"])
                ends = _parse_dt(pt_attrs.get("ends_at") or pt_attrs["starts_at"])
                if starts > horizon or starts < now - timedelta(days=1):
                    continue
                _, was_created = _upsert_event(
                    session, f"{st_id}/{plan_id}/{pt['id']}", f"{st_name}: {title}", starts, ends
                )
                created += was_created
                updated += not was_created

    for instance in client.calendar_instances(now, horizon):
        attrs = instance.get("attributes") or {}
        starts = _parse_dt(attrs["starts_at"])
        ends = _parse_dt(attrs.get("ends_at") or attrs["starts_at"])
        title = attrs.get("event_name") or "Calendar event"
        _, was_created = _upsert_event(session, f"cal/{instance['id']}", title, starts, ends)
        created += was_created
        updated += not was_created

    _generate_shifts_for_synced(session)
    session.flush()
    return {"created": created, "updated": updated}


def _generate_shifts_for_synced(session: Session) -> int:
    """Synced events with a recognized type get shifts from the recipes."""
    made = 0
    for event in session.scalars(
        select(m.Event).where(m.Event.pco_id.isnot(None), m.Event.event_type_id.isnot(None))
    ):
        if event.shifts:
            continue
        for recipe in session.scalars(
            select(m.RoleRecipe).where(m.RoleRecipe.event_type_id == event.event_type_id)
        ):
            for slot in range(recipe.count):
                session.add(m.Shift(event_id=event.id, role_id=recipe.role_id, slot_index=slot))
                made += 1
    return made


def link_volunteers(session: Session, client: PCOClient) -> int:
    """Match our volunteers to PCO people by phone; stores pco_person_id."""
    linked = 0
    for vol in session.scalars(select(m.Volunteer).where(m.Volunteer.pco_person_id.is_(None))):
        person_id = client.find_person_by_phone(vol.phone)
        if person_id:
            vol.pco_person_id = person_id
            linked += 1
    session.flush()
    return linked


# --- push: us -> PCO (the text agent's roster changes) -----------------------------


def _team_for_role(teams_by_name: dict[str, str], role: m.Role) -> str | None:
    return teams_by_name.get(role.name.lower()) or teams_by_name.get(role.ministry.lower())


def push_roster_updates(session: Session, client: PCOClient, clock: Clock) -> dict:
    """Mirror our assignment changes into the PCO plan.

    - active assignment on a PCO-linked event, not yet pushed -> schedule
    - cancelled assignment we previously pushed -> unschedule
    Unlinked volunteers/teams are skipped (counted), never guessed.
    """
    now = clock.now()
    pushed = removed = skipped = 0
    teams_cache: dict[str, dict[str, str]] = {}

    def teams_for(st_id: str) -> dict[str, str]:
        if st_id not in teams_cache:
            teams_cache[st_id] = {
                (t.get("attributes") or {}).get("name", "").lower(): t["id"]
                for t in client.teams(st_id)
            }
        return teams_cache[st_id]

    rows = session.execute(
        select(m.Assignment, m.Shift, m.Event)
        .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .join(m.Event, m.Shift.event_id == m.Event.id)
        .where(m.Event.pco_id.isnot(None), m.Event.pco_id.notlike("cal/%"))
    ).all()

    for assignment, shift, event in rows:
        st_id, plan_id, _ = event.pco_id.split("/", 2)
        if assignment.status in PUSHED_STATUSES and assignment.pco_synced_at is None:
            volunteer = assignment.volunteer
            if not volunteer.pco_person_id:
                skipped += 1
                continue
            team_id = _team_for_role(teams_for(st_id), shift.role)
            if team_id is None:
                skipped += 1
                continue
            try:
                assignment.pco_team_member_id = client.schedule_person(
                    st_id, plan_id, volunteer.pco_person_id, team_id
                )
                assignment.pco_synced_at = now
                pushed += 1
            except PCOError:
                logger.exception("PCO schedule push failed (assignment %s)", assignment.id)
        elif assignment.status == "cancelled" and assignment.pco_team_member_id is not None:
            try:
                client.unschedule(st_id, plan_id, assignment.pco_team_member_id)
                assignment.pco_team_member_id = None
                assignment.pco_synced_at = now
                removed += 1
            except PCOError:
                logger.exception("PCO unschedule push failed (assignment %s)", assignment.id)

    session.flush()
    return {"pushed": pushed, "removed": removed, "skipped": skipped}


# --- orchestration -----------------------------------------------------------------


def full_sync(session: Session, client: PCOClient, clock: Clock) -> dict:
    result = sync_events(session, client, clock)
    result["volunteers_linked"] = link_volunteers(session, client)
    result.update(push_roster_updates(session, client, clock))
    _mark_synced(session, clock)
    return result


def poll_if_due(session: Session, clock: Clock, settings: Settings | None = None) -> dict | None:
    """Polling fallback behind the webhook; cheap no-op when not due/configured."""
    settings = settings or get_settings()
    if not settings.pco_enabled:
        return None
    row = session.get(m.Policy, PCO_SYNC_STATE_KEY)
    if row is not None:
        last = datetime.fromisoformat(row.value["value"]["last_sync"])
        if clock.now() < last + POLL_INTERVAL:
            return None
    try:
        return full_sync(session, PCOClient(settings), clock)
    except (PCOError, httpx.HTTPError):
        logger.exception("PCO poll sync failed; will retry next interval")
        _mark_synced(session, clock)  # don't hammer a failing API
        return None


def _mark_synced(session: Session, clock: Clock) -> None:
    row = session.get(m.Policy, PCO_SYNC_STATE_KEY)
    state = {"value": {"last_sync": clock.now().isoformat()}}
    if row is None:
        session.add(m.Policy(key=PCO_SYNC_STATE_KEY, value=state))
    else:
        row.value = state
    session.flush()

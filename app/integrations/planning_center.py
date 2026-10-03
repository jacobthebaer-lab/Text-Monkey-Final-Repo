"""Explicitly scoped Planning Center Services schedule import.

This integration imports service times and OPEN staffing needs only. It never
imports people, grants SMS consent, starts fill requests, or sends messages.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import JSON, String, UniqueConstraint, Integer, Text

from app.db.models import UTCDateTime, Event, Role, Shift, Assignment

ORIGIN = "https://api.planningcenteronline.com"


class PlanningCenterError(RuntimeError):
    """Sanitized integration failure; never includes credentials or response bodies."""


@dataclass(frozen=True)
class PCOConfig:
    app_id: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)
    organization_id: str = ""
    service_type_ids: tuple[str, ...] = ()
    webhook_secret: str = field(default="", repr=False)
    webhook_secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls):
        return cls(os.getenv("PCO_APP_ID", ""), os.getenv("PCO_SECRET", ""),
                   os.getenv("PCO_ORGANIZATION_ID", ""),
                   tuple(x.strip() for x in os.getenv("PCO_SERVICE_TYPE_IDS", "").split(",") if x.strip()),
                   os.getenv("PCO_WEBHOOK_SECRET", ""),
                   tuple(x.strip() for x in os.getenv("PCO_WEBHOOK_SECRETS", "").split(",") if x.strip()))

    def require_scope(self):
        if not self.organization_id.isdigit() or not self.service_type_ids or any(not x.isdigit() for x in self.service_type_ids):
            raise PlanningCenterError("Set numeric PCO_ORGANIZATION_ID and explicit PCO_SERVICE_TYPE_IDS before sync")


class PCOBase(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict: JSON}


class PCOEventLink(PCOBase):
    __tablename__ = "pco_event_links"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40), index=True)
    service_type_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[int] = mapped_column(unique=True)


class PCOShiftLink(PCOBase):
    __tablename__ = "pco_shift_links"
    __table_args__ = (UniqueConstraint("shift_id"),)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), index=True)
    shift_id: Mapped[int] = mapped_column()


class PCODelivery(PCOBase):
    __tablename__ = "pco_deliveries"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_name: Mapped[str] = mapped_column(String(160))
    received_at: Mapped[datetime]
    result: Mapped[dict]


class PCOVolunteerPerson(PCOBase):
    """An explicit, organization-scoped identity link (never a consent grant)."""
    __tablename__ = "pco_volunteer_people"
    __table_args__ = (
        UniqueConstraint("organization_id", "volunteer_id"),
        UniqueConstraint("organization_id", "person_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40), index=True)
    volunteer_id: Mapped[int] = mapped_column(Integer, index=True)
    person_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime]


class PCOStaffingLink(PCOBase):
    """Last verified relationship between a local assignment and a PlanPerson."""
    __tablename__ = "pco_staffing_links"
    __table_args__ = (UniqueConstraint("organization_id", "plan_person_id"),)
    assignment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40), index=True)
    service_type_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40), index=True)
    team_id: Mapped[str] = mapped_column(String(40))
    person_id: Mapped[str] = mapped_column(String(40))
    plan_person_id: Mapped[str] = mapped_column(String(40))
    remote_status: Mapped[str] = mapped_column(String(30))
    verified_at: Mapped[datetime]
    remote_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class PCOStaffingIntent(PCOBase):
    """Durable application-level idempotency/outbox for staffing writes."""
    __tablename__ = "pco_staffing_intents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True)
    organization_id: Mapped[str] = mapped_column(String(40), index=True)
    service_type_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40), index=True)
    team_id: Mapped[str] = mapped_column(String(40))
    assignment_id: Mapped[int | None] = mapped_column(Integer)
    person_id: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(20))  # accept | cancel
    state: Mapped[str] = mapped_column(String(20), default="pending")
    plan_person_id: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    expected: Mapped[dict] = mapped_column(JSON, default=dict)
    retry_at: Mapped[datetime | None]
    depends_on: Mapped[int | None]


class PCOPositionScope(PCOBase):
    """Explicit reviewed plan-wide position map; never infer from role names."""
    __tablename__ = "pco_position_scopes"
    __table_args__ = (UniqueConstraint("organization_id", "event_id", "role_id"),)
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40))
    service_type_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[int]
    role_id: Mapped[int]
    team_id: Mapped[str] = mapped_column(String(40))
    position_id: Mapped[str] = mapped_column(String(40))
    position_name: Mapped[str] = mapped_column(String(100))
    plan_time_id: Mapped[str] = mapped_column(String(40))
    required_count: Mapped[int | None]
    verified_at: Mapped[datetime]


class PCOStaffingLease(PCOBase):
    __tablename__ = "pco_staffing_leases"
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    owner: Mapped[str] = mapped_column(String(40), default="")
    expires_at: Mapped[datetime]


class PCOStaffingPoll(PCOBase):
    __tablename__ = "pco_staffing_polls"
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    next_at: Mapped[datetime]
    reason: Mapped[str | None] = mapped_column(Text)


class PCOClient:
    def __init__(self, config: PCOConfig, *, transport=None):
        if not config.app_id or not config.secret:
            raise PlanningCenterError("Configure PCO_APP_ID and PCO_SECRET privately in ignored .env")
        self.config = config
        self.http = httpx.Client(auth=(config.app_id, config.secret), timeout=20,
                                 follow_redirects=False, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def request(self, method, path, *, data=None, params=None):
        url = ORIGIN + path if path.startswith("/") else path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.planningcenteronline.com" or parsed.username or parsed.fragment:
            raise PlanningCenterError("Refused API link outside Planning Center")
        version = "2022-10-20" if parsed.path.startswith("/webhooks/") else "2018-11-01"
        try:
            response = self.http.request(method, url, json=data, params=params,
                                         headers={"X-PCO-API-Version": version})
        except httpx.HTTPError:
            raise PlanningCenterError("Planning Center network request failed") from None
        if not 200 <= response.status_code < 300:
            error = PlanningCenterError(f"Planning Center returned HTTP {response.status_code}")
            if response.status_code == 429:
                try:
                    error.retry_after = min(3600, max(1, int(response.headers.get('Retry-After', '60'))))
                except ValueError:
                    error.retry_after = 60
            raise error
        try:
            return response.json()
        except ValueError:
            raise PlanningCenterError("Planning Center returned invalid JSON") from None

    def collection(self, path):
        items, seen = [], set()
        params = {"per_page": 100}
        while path:
            if path in seen or len(seen) >= 100:
                raise PlanningCenterError("Planning Center pagination exceeded safe limit")
            seen.add(path)
            page = self.request("GET", path, params=params)
            if not isinstance(page.get("data"), list):
                raise PlanningCenterError("Expected a Planning Center collection")
            items.extend(page["data"])
            path = page.get("links", {}).get("next")
            params = None
        return items

    def organization(self):
        return self.request("GET", "/services/v2")["data"]

    def create(self, path, kind, attributes, relationships=None):
        data = {"type": kind, "attributes": attributes}
        if relationships:
            data["relationships"] = relationships
        return self.request("POST", path, data={"data": data})["data"]


def _id(value):
    value = str(value)
    if not value.isdigit():
        raise PlanningCenterError("Invalid Planning Center resource ID")
    return value


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        raise PlanningCenterError("Service time must contain a timezone") from None


def relation(row, name):
    return (row.get("relationships", {}).get(name, {}).get("data") or {}).get("id")


def fetch_schedule(client, config):
    """Fetch and validate the complete scoped snapshot before changing local data."""
    config.require_scope()
    org = client.organization()
    if str(org.get("id")) != config.organization_id:
        raise PlanningCenterError("Planning Center organization does not match configured demo scope")
    snapshots = []
    for st_id in config.service_type_ids:
        root = f"/services/v2/service_types/{st_id}"
        service = client.request("GET", root)["data"]
        teams = {str(t["id"]): t["attributes"]["name"] for t in client.collection(root + "/teams")}
        for plan in client.collection(root + "/plans"):
            plan_id = _id(plan["id"])
            path = root + f"/plans/{plan_id}"
            needs = client.collection(path + "/needed_positions")
            times = client.collection(path + "/plan_times")
            for time in times:
                a = time["attributes"]
                if a.get("time_type") != "service":
                    continue
                time_id = _id(time["id"])
                start, end = _time(a.get("starts_at")), _time(a.get("ends_at"))
                if end <= start:
                    raise PlanningCenterError("Service end must follow service start")
                rows = []
                for need in needs:
                    at = need["attributes"]
                    need_time = relation(need, "time")
                    if need_time and str(need_time) != time_id:
                        continue
                    # A named preference cannot safely be applied to every time.
                    if relation(need, "time_preference_option") and not need_time:
                        raise PlanningCenterError("Time-preference staffing needs require an explicit service time")
                    quantity = at.get("quantity")
                    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 0 <= quantity <= 100:
                        raise PlanningCenterError("Invalid open-position quantity")
                    team_id = str(relation(need, "team") or "")
                    if team_id not in teams:
                        raise PlanningCenterError("Staffing need refers to an unknown team")
                    rows.append({"id": _id(need["id"]), "name": at.get("team_position_name") or teams[team_id],
                                 "team": teams[team_id], "team_id": team_id, "quantity": quantity})
                title = plan["attributes"].get("title") or service["attributes"]["name"]
                snapshots.append({"key": f"{config.organization_id}:{st_id}:{plan_id}:{time_id}",
                                  "service_type_id": st_id, "plan_id": plan_id,
                                  "title": str(title)[:200], "start": start, "end": end, "needs": rows})
    return org, snapshots


def sync_schedule(session, client, config):
    org, snapshots = fetch_schedule(client, config)
    report = {"organization_id": str(org["id"]), "service_types": len(config.service_type_ids),
              "events_created": 0, "events_updated": 0, "shifts_created": 0,
              "shifts_removed": 0, "events_cancelled": 0, "held_occupied": 0}
    seen_events = set()
    for snapshot in snapshots:
        key = snapshot["key"]
        seen_events.add(key)
        link = session.get(PCOEventLink, key)
        if link:
            event = session.get(Event, link.event_id)
            if event is None or event.gcal_event_id != "pco:" + key:
                raise PlanningCenterError("Local schedule links are stale; use a fresh isolated import database")
            report["events_updated"] += 1
        else:
            event = Event(gcal_event_id="pco:" + key, title=snapshot["title"], starts_at=snapshot["start"], ends_at=snapshot["end"], status="scheduled")
            session.add(event)
            session.flush()
            link = PCOEventLink(key=key, organization_id=config.organization_id,
                                service_type_id=snapshot["service_type_id"], plan_id=snapshot["plan_id"], event_id=event.id)
            session.add(link)
            session.flush()
            report["events_created"] += 1
        event.title, event.starts_at, event.ends_at = snapshot["title"], snapshot["start"], snapshot["end"]
        event.status = "scheduled"
        wanted = set()
        for need in snapshot["needs"]:
            # Namespace remote roles so local consent/qualification policy is untouched.
            name = f"PCO {need['name'][:35]} ({snapshot['service_type_id']}/{need['team_id']})"[:80]
            role = session.scalar(select(Role).where(Role.name == name))
            if not role:
                role = Role(name=name, ministry=need["team"][:80], required_qualifications=[],
                            criticality="standard", fill_policy="needs_approval")
                session.add(role)
                session.flush()
            prefix = f"{key}:{need['id']}:"
            links = list(session.scalars(select(PCOShiftLink).where(
                PCOShiftLink.event_key == key, PCOShiftLink.key.startswith(prefix))))
            free_links = []
            occupied = []
            for existing in links:
                linked_shift = session.get(Shift, existing.shift_id)
                if linked_shift is None or linked_shift.event_id != event.id or linked_shift.role_id != role.id:
                    raise PlanningCenterError("Local shift links are stale; use a fresh isolated import database")
                active = session.scalar(select(Assignment.id).where(Assignment.shift_id == linked_shift.id,
                    Assignment.status.in_(("proposed", "approved", "confirmed"))))
                (occupied if active else free_links).append(existing)
            # NeededPosition counts unfilled places, in addition to occupied ones.
            wanted.update(link.key for link in occupied)
            report["held_occupied"] += len(occupied)
            free_links.sort(key=lambda link: int(link.key.rsplit(":", 1)[-1]))
            wanted.update(link.key for link in free_links[:need["quantity"]])
            next_slot = max((int(link.key.rsplit(":", 1)[-1]) for link in links), default=-1)+1
            for _ in range(max(0, need["quantity"]-len(free_links))):
                indices = list(session.scalars(select(Shift.slot_index).where(Shift.event_id == event.id, Shift.role_id == role.id)))
                shift = Shift(event_id=event.id, role_id=role.id, slot_index=max(indices, default=-1) + 1)
                session.add(shift)
                session.flush()
                shift_key = f"{prefix}{next_slot}"
                next_slot += 1
                wanted.add(shift_key)
                session.add(PCOShiftLink(key=shift_key, event_key=key, shift_id=shift.id))
                report["shifts_created"] += 1
        session.flush()
        for shift_link in list(session.scalars(select(PCOShiftLink).where(PCOShiftLink.event_key == key))):
            if shift_link.key not in wanted:
                # Preserve any history/assignment, including cancelled assignments.
                if session.scalar(select(Assignment.id).where(Assignment.shift_id == shift_link.shift_id)):
                    report["held_occupied"] += 1
                else:
                    session.delete(session.get(Shift, shift_link.shift_id))
                    session.delete(shift_link)
                    report["shifts_removed"] += 1
    for link in session.scalars(select(PCOEventLink).where(PCOEventLink.organization_id == config.organization_id,
                                                        PCOEventLink.service_type_id.in_(config.service_type_ids))):
        if link.key not in seen_events:
            linked_event = session.get(Event, link.event_id)
            if linked_event is None or linked_event.gcal_event_id != "pco:" + link.key:
                raise PlanningCenterError("Local schedule links are stale; use a fresh isolated import database")
            occupied = session.scalar(select(Assignment.id).join(Shift).where(Shift.event_id == link.event_id))
            if occupied:
                report["held_occupied"] += 1
            else:
                session.get(Event, link.event_id).status = "cancelled"
                report["events_cancelled"] += 1
    session.flush()
    return report

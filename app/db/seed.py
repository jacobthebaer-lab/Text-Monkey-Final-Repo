"""Seed the database with synthetic data (PLAN.md section 17).

Everything is relative to SEED_ANCHOR (the demo "now"), so the deliberate
cases hold no matter when the seed runs:

- Sam Okafor runs sound at every service (single point of failure)
- Dana Whitcomb serves ~6x/month against a stated max of 2 (burnout)
- Frank Miller served 2x/month May-Aug, nothing in the last 6+ weeks (drop-off)
- Tessa Nguyen and Marcus Lee opted in 45/60 days ago, never scheduled (untapped)
- Rosa Alvarez is first-aid certified but never serves first aid (unused skill)
- Three background checks expire within 30 days; one child_safety_training is
  pending (unverified); Grace & Henry Chen serve together; Olivia Grant opted out
- "Fall Festival" has no event type (unknown-event flow); Christmas Eve services
  sit in the capacity horizon
- The 11:00 coffee shift is never filled (chronic gap)

History: 20 weeks of completed Sunday services with completed assignments.
Upcoming: October Sunday shifts are assigned (status approved) so the demo has
a schedule to cancel against; November+ shifts, kids nights, and Christmas Eve
exist but are unassigned - filling them is the planning agent's job (Phase 7).

Run: python -m app.db.seed [--db sqlite:///...]   (drops and recreates: this
is also the reset command used by the demo controls.)
"""

import argparse
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db import models as m
from app.db.session import make_engine, make_session_factory, reset_db

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
REPO_ROOT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("America/Denver")

# The demo "now": Thursday Oct 1, 2026, 9:00 Denver time.
SEED_ANCHOR = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)

HISTORY_WEEKS = 20
# Upcoming assignments stop here; later shifts are left for the planner.
ASSIGN_THROUGH = SEED_ANCHOR + timedelta(days=30)

SERVICE_TIMES = [(time(9, 0), time(10, 15), "sun_9"), (time(11, 0), time(12, 15), "sun_11")]

# Volunteers whose serving pattern is scripted below rather than rotated.
PINNED_IDS = {3, 4, 5, 6, 7}
# Never auto-assigned: coordinator, pastor, untapped pair, opted-out.
NEVER_ASSIGN_IDS = {1, 2, 9, 10, 11}


def _load(name: str):
    with open(DATA_DIR / name) as f:
        return json.load(f)


def _demo_phone_overrides() -> dict[str, str]:
    """Real phones for demo volunteers from gitignored demo_phones.json.

    {"Volunteer Name": "+1..."}. Entries that aren't valid E.164 (e.g. an
    unfilled placeholder) are skipped so the synthetic number stays.
    """
    path = REPO_ROOT / "demo_phones.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        name: phone
        for name, phone in data.items()
        if not name.startswith("_")
        and isinstance(phone, str)
        and phone.startswith("+")
        and phone[1:].isdigit()
        and len(phone) >= 11
    }


def _nth_sunday(d) -> int:
    """1 for the first Sunday of the month, 2 for the second, ..."""
    return (d.day - 1) // 7 + 1


class Rotation:
    """Deterministic round-robin over a fixed pool."""

    def __init__(self, ids: list[int]) -> None:
        self.ids = sorted(ids)
        self.i = 0

    def pick(self, ok) -> int | None:
        for _ in range(len(self.ids)):
            vid = self.ids[self.i % len(self.ids)]
            self.i += 1
            if ok(vid):
                return vid
        return None


class Seeder:
    def __init__(self, session: Session, anchor: datetime = SEED_ANCHOR) -> None:
        self.s = session
        self.anchor = anchor
        self.roles: dict[str, m.Role] = {}
        self.quals: dict[int, dict[str, tuple[str, object]]] = {}  # vid -> {type: (status, expires_on)}
        self.max_per_month: dict[int, int] = {}
        self.monthly_counts: dict[tuple[int, str], int] = {}
        self.used_on_date: dict[object, set[int]] = {}
        self.rotations: dict[str, Rotation] = {}
        self.counts = {"volunteers": 0, "events": 0, "shifts": 0, "assignments": 0}

    # -- loading ------------------------------------------------------------

    def load_roles(self) -> None:
        for row in _load("roles.json"):
            role = m.Role(**row)
            self.s.add(role)
            self.roles[role.name] = role
        self.s.flush()

    def load_event_types(self) -> dict[str, tuple[m.EventType, list]]:
        out = {}
        for row in _load("event_types.json"):
            et = m.EventType(name=row["name"], title_patterns=row["title_patterns"])
            self.s.add(et)
            self.s.flush()
            for item in row["recipe"]:
                self.s.add(m.RoleRecipe(event_type_id=et.id, role_id=self.roles[item["role"]].id, count=item["count"]))
            out[row["name"]] = (et, row["recipe"])
        self.s.flush()
        return out

    def load_volunteers(self) -> None:
        pools: dict[str, list[int]] = {}
        demo_phones = _demo_phone_overrides()
        for row in _load("volunteers.json")["volunteers"]:
            prefs = row.get("preferences", {})
            vol = m.Volunteer(
                id=row["id"],
                name=row["name"],
                phone=demo_phones.get(row["name"], row["phone"]),
                sms_opt_in=row.get("sms_opt_in", True),
                status=row.get("status", "active"),
                is_coordinator=row.get("is_coordinator", False),
                is_pastor=row.get("is_pastor", False),
                preferences=prefs,
                created_at=self.anchor - timedelta(days=row.get("created_days_ago", 400)),
            )
            self.s.add(vol)
            self.counts["volunteers"] += 1
            self.max_per_month[vol.id] = prefs.get("max_per_month", 3)

            self.quals[vol.id] = {}
            for q in row.get("qualifications", []):
                expires = None
                if "expires_in_days" in q:
                    expires = (self.anchor + timedelta(days=q["expires_in_days"])).date()
                verified = q["status"] == "verified"
                self.s.add(
                    m.Qualification(
                        volunteer_id=vol.id,
                        type=q["type"],
                        status=q["status"],
                        verified_by="Maria Delgado" if verified else None,
                        verified_at=self.anchor - timedelta(days=120) if verified else None,
                        expires_on=expires,
                    )
                )
                self.quals[vol.id][q["type"]] = (q["status"], expires)

            if vol.id not in PINNED_IDS and vol.id not in NEVER_ASSIGN_IDS:
                for role_name in prefs.get("interested_roles", []):
                    pools.setdefault(role_name, []).append(vol.id)
        self.s.flush()
        self.rotations = {name: Rotation(ids) for name, ids in pools.items()}

    def load_policies(self) -> None:
        for key, value in _load("policies.json").items():
            if key.startswith("_"):
                continue
            self.s.add(m.Policy(key=key, value={"value": value}))
        self.s.flush()

    # -- events and shifts ----------------------------------------------------

    def add_event(self, title, et, recipe, starts, ends) -> m.Event:
        event = m.Event(
            title=title,
            event_type_id=et.id if et else None,
            starts_at=starts,
            ends_at=ends,
            status="completed" if starts < self.anchor else "scheduled",
        )
        self.s.add(event)
        self.s.flush()
        self.counts["events"] += 1
        for item in recipe or []:
            for slot in range(item["count"]):
                self.s.add(m.Shift(event_id=event.id, role_id=self.roles[item["role"]].id, slot_index=slot))
                self.counts["shifts"] += 1
        self.s.flush()
        return event

    # -- assignment ----------------------------------------------------------

    def eligible(self, vid: int, role: m.Role, on_date) -> bool:
        for q_type in role.required_qualifications:
            status, expires = self.quals.get(vid, {}).get(q_type, (None, None))
            if status != "verified":
                return False
            if expires is not None and expires < on_date:
                return False
        return True

    def assign(self, shift: m.Shift, vid: int, event: m.Event) -> None:
        past = event.starts_at < self.anchor
        created = event.starts_at - timedelta(days=14)
        self.s.add(
            m.Assignment(
                shift_id=shift.id,
                volunteer_id=vid,
                status="completed" if past else "approved",
                source="planner",
                created_at=created,
                updated_at=created,
            )
        )
        self.counts["assignments"] += 1
        month = event.starts_at.astimezone(TZ).strftime("%Y-%m")
        self.monthly_counts[(vid, month)] = self.monthly_counts.get((vid, month), 0) + 1
        self.used_on_date.setdefault(event.starts_at.astimezone(TZ).date(), set()).add(vid)

    def fill_generic(self, shift: m.Shift, event: m.Event) -> None:
        role = self.roles_by_id[shift.role_id]
        rotation = self.rotations.get(role.name)
        if rotation is None:
            return
        event_date = event.starts_at.astimezone(TZ).date()
        month = event.starts_at.astimezone(TZ).strftime("%Y-%m")
        used_today = self.used_on_date.setdefault(event_date, set())

        def ok(vid: int) -> bool:
            return (
                vid not in used_today
                and self.monthly_counts.get((vid, month), 0) < self.max_per_month[vid]
                and self.eligible(vid, role, event_date)
            )

        vid = rotation.pick(ok)
        if vid is not None:
            self.assign(shift, vid, event)

    def fill_sunday_service(self, event: m.Event, service: str) -> None:
        """Pinned deliberate-case assignments first, then round-robin the rest."""
        d = event.starts_at.astimezone(TZ).date()
        nth = _nth_sunday(d)
        drop_off_cutoff = (self.anchor - timedelta(weeks=6)).date()
        shifts_by_role: dict[str, list[m.Shift]] = {}
        for shift in event.shifts:
            shifts_by_role.setdefault(self.roles_by_id[shift.role_id].name, []).append(shift)

        pinned: list[tuple[m.Shift, int]] = []
        pinned.append((shifts_by_role["sound"][0], 3))  # Sam: every service (SPOF)
        if service == "sun_9":
            pinned.append((shifts_by_role["usher"][0], 4))  # Dana: every week (burnout)
            if nth in (1, 3) and d <= drop_off_cutoff:
                pinned.append((shifts_by_role["greeter"][0], 5))  # Frank, then he stops
        if service == "sun_11":
            if nth in (2, 4):
                pinned.append((shifts_by_role["parking"][0], 4))  # Dana again (burnout)
            if nth in (1, 3):
                pinned.append((shifts_by_role["greeter"][0], 6))  # Grace & Henry together
                pinned.append((shifts_by_role["greeter"][1], 7))

        taken = set()
        for shift, vid in pinned:
            self.assign(shift, vid, event)
            taken.add(shift.id)

        for shift in event.shifts:
            if shift.id in taken:
                continue
            role_name = self.roles_by_id[shift.role_id].name
            if role_name == "coffee" and service == "sun_11":
                continue  # deliberate chronic gap
            self.fill_generic(shift, event)

    # -- top level -------------------------------------------------------------

    def run(self) -> dict:
        self.load_roles()
        self.roles_by_id = {r.id: r for r in self.roles.values()}
        event_types = self.load_event_types()
        self.load_volunteers()
        self.load_policies()

        anchor_date = self.anchor.astimezone(TZ).date()

        # Sundays: 20 weeks of history plus every Sunday through the year end.
        last_past_sunday = anchor_date - timedelta(days=(anchor_date.weekday() + 1) % 7 or 7)
        first_sunday = last_past_sunday - timedelta(weeks=HISTORY_WEEKS - 1)
        sunday = first_sunday
        et, recipe = event_types["sunday_service"]
        while sunday.year == self.anchor.year:
            for start_t, end_t, service in SERVICE_TIMES:
                event = self.add_event(
                    f"Sunday Service {start_t.strftime('%-I:%M')}",
                    et,
                    recipe,
                    datetime.combine(sunday, start_t, tzinfo=TZ),
                    datetime.combine(sunday, end_t, tzinfo=TZ),
                )
                if event.starts_at.astimezone(TZ).date() <= ASSIGN_THROUGH.astimezone(TZ).date():
                    self.fill_sunday_service(event, service)
            sunday += timedelta(weeks=1)

        # Wednesday kids nights: upcoming only, unassigned (planner's job).
        et, recipe = event_types["kids_night"]
        wednesday = anchor_date + timedelta(days=(2 - anchor_date.weekday()) % 7 or 7)
        while wednesday <= anchor_date + timedelta(weeks=11):
            self.add_event(
                "Kids Night",
                et,
                recipe,
                datetime.combine(wednesday, time(18, 30), tzinfo=TZ),
                datetime.combine(wednesday, time(20, 0), tzinfo=TZ),
            )
            wednesday += timedelta(weeks=1)

        # Food Drive: the Saturday 9 days out, assigned.
        et, recipe = event_types["food_drive"]
        saturday = anchor_date + timedelta(days=(5 - anchor_date.weekday()) % 7 or 7) + timedelta(weeks=1)
        event = self.add_event(
            "Food Drive",
            et,
            recipe,
            datetime.combine(saturday, time(9, 0), tzinfo=TZ),
            datetime.combine(saturday, time(12, 0), tzinfo=TZ),
        )
        for shift in event.shifts:
            self.fill_generic(shift, event)

        # Fall Festival: intentionally no event type -> no recipe, no shifts.
        # Triggers the unknown-event flow (PLAN.md section 10).
        festival = anchor_date + timedelta(days=(5 - anchor_date.weekday()) % 7 or 7) + timedelta(weeks=3)
        self.add_event(
            "Fall Festival",
            None,
            None,
            datetime.combine(festival, time(15, 0), tzinfo=TZ),
            datetime.combine(festival, time(19, 0), tzinfo=TZ),
        )

        # Christmas Eve services: in the capacity horizon, unassigned.
        et, recipe = event_types["christmas_eve"]
        christmas_eve = anchor_date.replace(month=12, day=24)
        for start_t, end_t in [(time(16, 0), time(17, 0)), (time(18, 0), time(19, 0))]:
            self.add_event(
                f"Christmas Eve Service {start_t.strftime('%-I%p').lower()}",
                et,
                recipe,
                datetime.combine(christmas_eve, start_t, tzinfo=TZ),
                datetime.combine(christmas_eve, end_t, tzinfo=TZ),
            )

        self.s.commit()
        return self.counts


def seed(session: Session, anchor: datetime = SEED_ANCHOR) -> dict:
    return Seeder(session, anchor).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset the database and load synthetic seed data.")
    parser.add_argument("--db", default=None, help="database URL (default: DATABASE_URL from env)")
    args = parser.parse_args()

    engine = make_engine(args.db)
    reset_db(engine)
    with make_session_factory(engine)() as session:
        counts = seed(session)
    print(f"Seeded {engine.url}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


if __name__ == "__main__":
    main()

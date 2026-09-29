"""SQLAlchemy models per PLAN.md section 6.

Conventions:
- All datetimes are stored in UTC and come back timezone-aware (UTCDateTime).
  Callers set created_at/updated_at from the app Clock, never func.now(),
  so tests and demo fast-forward stay deterministic.
- JSON columns hold small structured blobs (preferences, evidence, payloads).
"""

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime, JSON


class UTCDateTime(TypeDecorator):
    """Store tz-aware datetimes as UTC; return them tz-aware.

    SQLite has no timezone support, so without this, aware datetimes round-trip
    as naive ones and comparisons silently break.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime written to UTCDateTime column")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict: JSON, list: JSON}


class Volunteer(Base):
    __tablename__ = "volunteers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # E.164
    sms_opt_in: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | inactive
    is_coordinator: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pastor: Mapped[bool] = mapped_column(Boolean, default=False)
    # {interested_roles, preferred_services, max_per_month, serves_with_volunteer_id, notes}
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]

    qualifications: Mapped[list["Qualification"]] = relationship(back_populates="volunteer")
    assignments: Mapped[list["Assignment"]] = relationship(back_populates="volunteer")


class Qualification(Base):
    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), index=True)
    type: Mapped[str] = mapped_column(String(50))  # background_check, child_safety_training, ...
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | verified | expired
    verified_by: Mapped[str | None] = mapped_column(String(120))
    verified_at: Mapped[datetime | None]
    expires_on: Mapped[date | None] = mapped_column(Date)

    volunteer: Mapped[Volunteer] = relationship(back_populates="qualifications")


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    ministry: Mapped[str] = mapped_column(String(80))
    required_qualifications: Mapped[list] = mapped_column(JSON, default=list)
    criticality: Mapped[str] = mapped_column(String(20))  # critical | standard | optional
    fill_policy: Mapped[str] = mapped_column(String(20))  # auto | needs_approval


class EventType(Base):
    __tablename__ = "event_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    title_patterns: Mapped[list] = mapped_column(JSON, default=list)


class RoleRecipe(Base):
    __tablename__ = "role_recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type_id: Mapped[int] = mapped_column(ForeignKey("event_types.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    count: Mapped[int] = mapped_column(Integer)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    gcal_event_id: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200))
    event_type_id: Mapped[int | None] = mapped_column(ForeignKey("event_types.id"))  # null = unknown type
    starts_at: Mapped[datetime] = mapped_column(index=True)
    ends_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled | completed | cancelled

    shifts: Mapped[list["Shift"]] = relationship(back_populates="event")


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), index=True)
    slot_index: Mapped[int] = mapped_column(Integer, default=0)

    event: Mapped[Event] = relationship(back_populates="shifts")
    role: Mapped[Role] = relationship()
    assignments: Mapped[list["Assignment"]] = relationship(back_populates="shift")


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"), index=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))  # proposed | approved | confirmed | cancelled | completed
    source: Mapped[str] = mapped_column(String(20))  # planner | fill | admin
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    shift: Mapped[Shift] = relationship(back_populates="assignments")
    volunteer: Mapped[Volunteer] = relationship(back_populates="assignments")


class Availability(Base):
    __tablename__ = "availability"

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), index=True)
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    available_dates: Mapped[list] = mapped_column(JSON, default=list)
    unavailable_dates: Mapped[list] = mapped_column(JSON, default=list)
    raw_reply: Mapped[str | None] = mapped_column(Text)
    parsed_at: Mapped[datetime | None]


class FillRequest(Base):
    __tablename__ = "fill_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"), index=True)
    cancelled_assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id"))
    urgency: Mapped[str] = mapped_column(String(20))  # critical | high | normal | skip
    # open | waiting_approval | in_progress | filled | escalated | skipped
    state: Mapped[str] = mapped_column(String(30), default="open")
    current_tranche: Mapped[int] = mapped_column(Integer, default=0)
    next_action_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]


class Outreach(Base):
    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(primary_key=True)
    fill_request_id: Mapped[int] = mapped_column(ForeignKey("fill_requests.id"), index=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), index=True)
    tranche: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    response: Mapped[str] = mapped_column(String(20), default="none")  # none | yes | no | partial | unclear
    responded_at: Mapped[datetime | None]


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))  # send_outreach | publish_schedule | training_invite | ...
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | rejected | expired
    requested_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str | None] = mapped_column(String(120))
    via: Mapped[str | None] = mapped_column(String(10))  # web | sms


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(5))  # in | out
    volunteer_id: Mapped[int | None] = mapped_column(ForeignKey("volunteers.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    body: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))  # template | ai | admin
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    created_at: Mapped[datetime]


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # sensitive | pastoral | unfillable | unclear | system_error | unknown_event
    category: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(10), default="normal")  # normal | urgent
    summary: Mapped[str] = mapped_column(Text)
    related_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("volunteers.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | acknowledged | resolved
    created_at: Mapped[datetime]


class Flag(Base):
    __tablename__ = "flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # concern | opportunity
    # single_point_of_failure | burnout | drop_off | expiring | chronic_gap |
    # untapped | unused_skill | growing_need | rebalance
    type: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    suggested_action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | accepted | dismissed
    created_at: Mapped[datetime]


class Policy(Base):
    __tablename__ = "policies"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)  # {"value": ...} wrapper for scalars


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(40))
    trigger: Mapped[str] = mapped_column(String(200))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    model: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str | None] = mapped_column(String(200))

    step_rows: Mapped[list["AgentStep"]] = relationship(back_populates="run")


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(20))  # model_call | tool_call | tool_result | decision | escalation
    tool_name: Mapped[str | None] = mapped_column(String(60))
    arguments: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime]

    run: Mapped[AgentRun] = relationship(back_populates="step_rows")

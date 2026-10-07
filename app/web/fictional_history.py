"""Read only the explicitly imported fictional history, never native receipts."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from app.db import models as m

DATASET = "fictional-history-2026-jul-sep-v1"
PHONE = re.compile(r"\+120255501[0-9]{2}\Z")
PURPOSES = {"fictional_" + name for name in (
    "availability_request", "availability_reply", "invitation", "decline",
    "replacement_invitation", "accept", "confirmation", "cancellation",
    "cancellation_ack", "replacement_accept", "reminder", "reminder_reply", "admin_status",
)}


def candidate(volunteer):
    prefs = volunteer.preferences or {}
    key = prefs.get("synthetic_person_key")
    person = re.fullmatch(r"person-([0-9]{3})", key) if isinstance(key, str) else None
    return (PHONE.fullmatch(volunteer.phone) is not None and prefs.get("synthetic") is True
            and prefs.get("fictional_seed") is True and volunteer.name.endswith(" [Fictional]")
            and prefs.get("synthetic_dataset") == DATASET and person is not None
            and 1 <= int(person[1]) <= 100)


def manifest(session):
    marker = session.get(m.Policy, "synthetic_dataset:" + DATASET)
    value = marker.value if marker else None
    if (not isinstance(value, dict) or value.get("synthetic") is not True
            or value.get("provenance") != DATASET or value.get("no_consent_no_delivery") is not True
            or value.get("date_range") != ["2026-07-01", "2026-09-30"]
            or value.get("timezone") != "America/Denver"
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("artifact_sha256", "")))):
        return {}
    maps = value.get("id_maps")
    if not isinstance(maps, dict):
        return {}
    result = {}
    for table in ("volunteers", "messages"):
        entries = maps.get(table)
        if (not isinstance(entries, dict) or not entries
                or any(type(v) is not int or v < 1 for v in entries.values())
                or len(set(entries.values())) != len(entries)):
            return {}
        result[table] = set(entries.values())
    return result


def history_query(session, volunteer):
    proof = manifest(session)
    prefs = volunteer.preferences or {}
    if (not candidate(volunteer) or volunteer.sms_opt_in
            or volunteer.id not in proof.get("volunteers", set())
            or not isinstance(prefs.get("synthetic_person_key"), str)
            or not prefs["synthetic_person_key"]):
        return select(m.Message).where(False)
    return select(m.Message).where(
        m.Message.id.in_(proof["messages"]),
        m.Message.volunteer_id == volunteer.id, m.Message.phone == volunteer.phone,
        m.Message.kind == "synthetic", m.Message.status == "simulated",
        m.Message.purpose.in_(PURPOSES), m.Message.provider_sid.is_(None),
        m.Message.direction.in_(["in", "out"]),
        m.Message.body.startswith("[Fictional history] "),
        m.Message.created_at >= datetime(2026, 7, 1, tzinfo=ZoneInfo("America/Denver")),
        m.Message.created_at < datetime(2026, 10, 1, tzinfo=ZoneInfo("America/Denver")),
    )

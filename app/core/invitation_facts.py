"""Church-local facts for replacement copy, without rewriting model output."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.policies import PolicyStore

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def time_label(moment):
    seconds = f":{moment.second:02d}" if moment.second else ""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d}{seconds}{'am' if moment.hour < 12 else 'pm'}"


def shift_labels(session, shift):
    zone = PolicyStore(session).church_tz()
    start, end = shift.starts_at.astimezone(zone), shift.ends_at.astimezone(zone)

    def label(moment):
        return f"{moment.date().isoformat()} {DAYS[moment.weekday()]} {time_label(moment)} {zone.key} (UTC{moment.isoformat()[-6:]})"

    if (shift.parent_shift_id is not None or start.date() != end.date()
            or start.utcoffset() != end.utcoffset()):
        from app.core.offer_windows import interval_label
        invitation = interval_label(session, shift)
    else:
        invitation = (f"{DAYS[start.weekday()]}, {MONTHS[start.month-1]} {start.day} {start.year}, "
                      f"{time_label(start)} to {time_label(end)}")
    return {"timezone": zone.key, "weekday_name": DAYS[start.weekday()],
            "local_starts_at": start.isoformat(), "local_ends_at": end.isoformat(),
            "start_label": label(start), "end_label": label(end),
            "invitation_label": invitation}


def copy_problem(session, shift, body):
    """Require supplied wording, and reject extra explicit scheduling claims.

    This compares dates, weekday names and numeric times, not arbitrary prose.
    Validate the draft before the application adds its independent reply deadline.
    """
    facts = shift_labels(session, shift)
    canonical = facts["invitation_label"]
    if not isinstance(body, str) or canonical not in body:
        return "Include shift.invitation_label verbatim. Regenerate the invitation through Gloo using the supplied church-local facts."
    start, end = (datetime.fromisoformat(facts[key]) for key in ("local_starts_at", "local_ends_at"))
    dates = {start.date().isoformat(), end.date().isoformat()}
    weekdays = {DAYS[start.weekday()].lower(), DAYS[end.weekday()].lower()}
    names = {day.lower(): day.lower() for day in DAYS}
    names.update({day[:3].lower(): day.lower() for day in DAYS})
    for match in re.finditer(r"\b(?:" + "|".join(names) + r")\b", body, re.I):
        if names[match[0].lower()] not in weekdays:
            return "Invitation weekday differs from the supplied church-local shift. Regenerate through Gloo."
    if any(match[0] not in dates for match in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", body)):
        return "Invitation date differs from the supplied church-local shift. Regenerate through Gloo."
    months = {name.lower(): index + 1 for index, name in enumerate(MONTH_NAMES)}
    months.update({name.lower(): index + 1 for index, name in enumerate(MONTHS)})
    for match in re.finditer(r"\b(" + "|".join(months) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", body, re.I):
        if not any(moment.month == months[match[1].lower()] and moment.day == int(match[2])
                   and (not match[3] or moment.year == int(match[3])) for moment in (start, end)):
            return "Invitation date differs from the supplied church-local shift. Regenerate through Gloo."
    endpoints = {moment.hour * 3600 + moment.minute * 60 + moment.second for moment in (start, end)}
    pattern = r"\b(0?[1-9]|1[0-2])(?::([0-5]\d))?(?::([0-5]\d))?\s*([AP])M\b"
    def seconds(match):
        return ((int(match[1]) % 12 + (12 if match[4].lower() == 'p' else 0)) * 3600
                + int(match[2] or 0) * 60 + int(match[3] or 0))
    for match in re.finditer(pattern, body, re.I):
        if seconds(match) not in endpoints:
            return "Invitation time differs from the supplied church-local shift. Regenerate through Gloo."
    token = r"(?:0?[1-9]|1[0-2])(?::[0-5]\d)?(?::[0-5]\d)?\s*[AP]M"
    expected = (start.hour * 3600 + start.minute * 60 + start.second,
                end.hour * 3600 + end.minute * 60 + end.second)
    for match in re.finditer(r"\b(" + token + r")\s*(?:to|[-–])\s*(" + token + r")\b", body, re.I):
        if tuple(seconds(re.fullmatch(pattern, value, re.I)) for value in match.groups()) != expected:
            return "Invitation time range differs from the supplied church-local shift. Regenerate through Gloo."
    # Include explicit 24-hour claims, while leaving AM/PM tokens to the check above.
    bare = r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?![\d:]|\s*[AP]M\b)"
    for match in re.finditer(bare, body, re.I):
        if int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3] or 0) not in endpoints:
            return "Invitation time differs from the supplied church-local shift. Regenerate through Gloo."
    # The canonical split/DST label already contains its verified offset notation.
    extra = body.replace(canonical, "")
    zones = {start.tzname(), end.tzname(), facts['timezone']}
    for match in re.finditer(r"\b(?:UTC|GMT|EST|EDT|CST|CDT|MST|MDT|PST|PDT)\b", extra, re.I):
        if match[0].upper() not in {zone.upper() for zone in zones}:
            return "Invitation timezone differs from the supplied church-local shift. Regenerate through Gloo."
    for match in re.finditer(r"\b[A-Za-z_]+/[A-Za-z_/]+\b", extra):
        try:
            zone = ZoneInfo(match[0])
        except ZoneInfoNotFoundError:
            continue
        if zone.key != facts['timezone']:
            return "Invitation timezone differs from the supplied church-local shift. Regenerate through Gloo."
    return None

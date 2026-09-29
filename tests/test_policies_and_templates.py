from datetime import datetime, time

from app.core import templates
from app.core.policies import PolicyStore, in_quiet_hours, next_send_time
from app.db import models as m
from tests.conftest import DENVER

QUIET = (time(21, 0), time(7, 0))


def test_quiet_hours_wrap_midnight():
    assert in_quiet_hours(datetime(2026, 10, 1, 22, 0, tzinfo=DENVER), *QUIET)
    assert in_quiet_hours(datetime(2026, 10, 1, 6, 59, tzinfo=DENVER), *QUIET)
    assert not in_quiet_hours(datetime(2026, 10, 1, 7, 0, tzinfo=DENVER), *QUIET)
    assert not in_quiet_hours(datetime(2026, 10, 1, 20, 59, tzinfo=DENVER), *QUIET)


def test_next_send_time():
    late = datetime(2026, 10, 1, 22, 0, tzinfo=DENVER)
    assert next_send_time(late, *QUIET) == datetime(2026, 10, 2, 7, 0, tzinfo=DENVER)
    early = datetime(2026, 10, 1, 5, 0, tzinfo=DENVER)
    assert next_send_time(early, *QUIET) == datetime(2026, 10, 1, 7, 0, tzinfo=DENVER)
    daytime = datetime(2026, 10, 1, 12, 0, tzinfo=DENVER)
    assert next_send_time(daytime, *QUIET) == daytime


def test_policy_store_prefers_db_over_defaults(session):
    store = PolicyStore(session)
    assert store.ask_budget() == 4  # default
    session.add(m.Policy(key="monthly_ask_budget_per_volunteer", value={"value": 2}))
    session.flush()
    assert store.ask_budget() == 2


def test_templates_are_sms_sized_and_personal():
    samples = [
        templates.reminder("Jen Hartley", "nursery", "tomorrow at 8:45am", "9am service"),
        templates.assignment_confirmation("Jen Hartley", "nursery", "Sun Oct 4, 9am"),
        templates.filled_thanks("Mark Sowell"),
        templates.availability_ask("Jen Hartley", "November"),
        templates.cancellation_ack("Priya Raman"),
        templates.clarify_which_shift("Sam Okafor", ["Sun 9am sound", "Sun 11am sound"]),
        templates.approval_request("Sarah Jones", "9am nursery Sunday", ["Jen Hartley", "Mark Sowell", "Priya Raman"]),
        templates.unknown_number("Cedar Hills Community Church"),
        templates.stop_confirm("Cedar Hills Community Church"),
        templates.start_confirm("Cedar Hills Community Church"),
    ]
    for text in samples:
        assert len(text) <= templates.MAX_SMS_LEN, text
    assert "Jen" in samples[0] and "Hartley" not in samples[0]
    assert "Reply YES to send" in samples[6]

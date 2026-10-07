from datetime import timedelta

import pytest

from app.core.policies import PolicyStore, DEFAULTS, in_quiet_hours
from app.db.models import Policy
from tests.conftest import NOW


def grant(session, **changes):
    value = {"actor": "Fictional test operator", "starts_at": NOW.isoformat(),
             "expires_at": (NOW+timedelta(hours=72)).isoformat(), **changes}
    session.add(Policy(key="quiet_hours_test_window", value={"value": value}))
    session.flush()


def test_test_window_restores_both_original_settings_at_exact_expiry(session):
    custom = {"start": "20:00", "end": "08:00"}
    session.add(Policy(key="quiet_hours", value={"value": custom}))
    grant(session)
    session.commit(); session.expire_all()
    policies = PolicyStore(session)
    for key, original in (("quiet_hours", custom), ("urgent_quiet_hours", DEFAULTS["urgent_quiet_hours"])):
        assert policies.get(key, now=NOW-timedelta(seconds=1)) == original
        assert policies.get(key, now=NOW) == {"start":"00:00", "end":"00:00"}
        assert policies.get(key, now=NOW+timedelta(hours=72)-timedelta(microseconds=1)) == {"start":"00:00", "end":"00:00"}
        assert policies.get(key, now=NOW+timedelta(hours=72)) == original
    assert session.get(Policy, "quiet_hours").value == {"value": custom}


@pytest.mark.parametrize("change", [
    {"actor": ""}, {"starts_at": "invalid"}, {"expires_at": None},
    {"expires_at": (NOW+timedelta(hours=73)).isoformat()},
    {"expires_at": NOW.isoformat()},
    {"expires_at": (NOW+timedelta(hours=72)).replace(tzinfo=None).isoformat()},
    {"extra": True},
])
def test_invalid_or_unbounded_test_cannot_disable_quiet_hours(session, change):
    grant(session, **change)
    assert not PolicyStore(session).quiet_hours_test_active(NOW)
    assert PolicyStore(session).get("quiet_hours", now=NOW) == DEFAULTS["quiet_hours"]


def test_live_policy_methods_use_current_utc_window(session):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    session.add(Policy(key="quiet_hours_test_window", value={"value": {
        "actor":"Fictional operator", "starts_at":(now-timedelta(minutes=1)).isoformat(),
        "expires_at":(now+timedelta(minutes=1)).isoformat()}}))
    session.flush()
    policies = PolicyStore(session)
    assert policies.quiet_hours_test_active()
    assert not in_quiet_hours(now.replace(hour=22), *policies.quiet_hours())
    assert not in_quiet_hours(now.replace(hour=22), *policies.urgent_quiet_hours())

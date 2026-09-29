"""Candidate ranking: hard filters and score ordering."""

from datetime import timedelta

from app.core.ranking import rank_candidates
from app.db import models as m
from tests.conftest import NOW


def ids(candidates):
    return [c.volunteer.id for c in candidates]


def test_hard_filters(session, make_volunteer, make_shift, assign):
    shift = make_shift("nursery", required=("background_check",))
    ok = make_volunteer(quals=[("background_check", "verified", None)])
    make_volunteer()  # unqualified
    opted_out = make_volunteer(opt_in=False, quals=[("background_check", "verified", None)])
    make_volunteer(coordinator=True, quals=[("background_check", "verified", None)])
    excluded = make_volunteer(quals=[("background_check", "verified", None)])

    result = rank_candidates(session, shift, NOW, exclude_ids=(excluded.id,))
    assert ids(result) == [ok.id]
    assert opted_out.id not in ids(result)


def test_over_monthly_max_excluded(session, make_volunteer, make_shift, assign):
    shift = make_shift("usher")
    busy = make_volunteer(prefs={"max_per_month": 2})
    free = make_volunteer(prefs={"max_per_month": 2})
    for days in (7, 14):  # two other October assignments
        other = make_shift("greeter", starts=NOW + timedelta(days=days))
        assign(busy, other, status="approved")

    assert ids(rank_candidates(session, shift, NOW)) == [free.id]


def test_preferred_role_and_service_rank_higher(session, make_volunteer, make_shift):
    shift = make_shift("usher")  # Sunday 9:00
    plain = make_volunteer()
    keen = make_volunteer(prefs={"interested_roles": ["usher"], "preferred_services": ["sun_9"]})

    result = rank_candidates(session, shift, NOW)
    assert ids(result) == [keen.id, plain.id]
    assert result[0].breakdown["preferred_role"] == 2.0
    assert result[0].breakdown["attends_service"] == 1.0


def test_asked_recently_penalty(session, make_volunteer, make_shift):
    shift = make_shift("usher")
    fresh = make_volunteer()
    pestered = make_volunteer()
    session.add(
        m.Message(
            direction="out",
            volunteer_id=pestered.id,
            phone=pestered.phone,
            body="Can you cover Sunday?",
            kind="ai",
            purpose="outreach",
            status="sent",
            created_at=NOW - timedelta(days=2),
        )
    )
    session.flush()

    result = rank_candidates(session, shift, NOW)
    assert ids(result) == [fresh.id, pestered.id]
    assert result[1].breakdown["asked_recently"] < 0


def test_recent_load_favors_rested(session, make_volunteer, make_shift, assign):
    shift = make_shift("usher")
    worked = make_volunteer(prefs={"max_per_month": 8})
    rested = make_volunteer()
    for days_ago in (7, 14):
        past = make_shift("greeter", starts=NOW - timedelta(days=days_ago))
        assign(worked, past, status="completed")

    result = rank_candidates(session, shift, NOW)
    assert ids(result) == [rested.id, worked.id]


def test_partner_availability_bonus_and_tie_break(session, make_volunteer, make_shift):
    shift = make_shift("greeter")
    solo_a = make_volunteer()
    solo_b = make_volunteer()
    half = make_volunteer(prefs={"serves_with_volunteer_id": None})
    partner = make_volunteer()
    half.preferences = {"serves_with_volunteer_id": partner.id}
    session.flush()

    result = rank_candidates(session, shift, NOW)
    assert result[0].volunteer.id == half.id
    assert result[0].breakdown["partner_available"] == 1.0
    # Equal scores fall back to volunteer id order.
    rest = ids(result)[1:]
    assert rest == sorted(rest)
    assert set(rest) == {solo_a.id, solo_b.id, partner.id}

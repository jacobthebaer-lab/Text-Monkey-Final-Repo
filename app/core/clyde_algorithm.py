"""Text Monkey scheduling primitives (Python 3.10+, standard library only).

All durations are in minutes; timestamps must be timezone-aware.
This module plans requests. The caller sends texts and records successful sends.
No response-time distribution is assumed: supply a PendingProbability model
before using plan_follow_up. Historical acceptance rates include expired requests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import fsum, isfinite
from typing import Callable, Iterable


def _number(name: str, value: float, *, positive: bool = False) -> None:
    if not isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")


def minutes_between(later: datetime, earlier: datetime) -> float:
    """Return elapsed minutes; reject naive timestamps and reversed times."""
    if later.utcoffset() is None or earlier.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware")
    minutes = (later.timestamp() - earlier.timestamp()) / 60
    _number("elapsed minutes", minutes)
    return minutes


@dataclass(frozen=True)
class Volunteer:
    volunteer_id: str
    acceptance_rate: float
    average_response_minutes: float
    last_requested_at: datetime | None
    # Synthetic scoring reference for newcomers; never a recorded text.
    initial_scoring_reference_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.volunteer_id:
            raise ValueError("volunteer_id cannot be empty")
        _number("acceptance_rate", self.acceptance_rate)
        if self.acceptance_rate > 1:
            raise ValueError("acceptance_rate must be at most 1")
        _number("average_response_minutes", self.average_response_minutes, positive=True)
        if self.last_requested_at is not None:
            minutes_between(self.last_requested_at, self.last_requested_at)
        if self.initial_scoring_reference_at is not None:
            minutes_between(self.initial_scoring_reference_at, self.initial_scoring_reference_at)


DEFAULT_ACCEPTANCE_RATE = 1.0
DEFAULT_RESPONSE_MINUTES = 60.0
DEFAULT_ELAPSED_MINUTES = 1440.0


def elapsed_since_request(volunteer: Volunteer, now: datetime) -> float:
    """Use the real last text, or the newcomer's fixed initial reference."""
    minutes_between(now, now)
    reference = volunteer.last_requested_at or volunteer.initial_scoring_reference_at
    return (
        DEFAULT_ELAPSED_MINUTES if reference is None
        else minutes_between(now, reference)
    )


def create_volunteer(
    volunteer_id: str, current_volunteers: Iterable[Volunteer], now: datetime,
) -> Volunteer:
    """Initialize from equal-weight averages of the existing volunteers.

    An empty pool uses 100% acceptance, 60-minute response, and 1440 minutes
    elapsed. These are starting estimates, not fabricated request history.
    Snapshot the averages at enrollment; do not reset them on each scoring pass.
    The synthetic reference lets elapsed time grow until the first actual text.
    """
    minutes_between(now, now)
    pool = list(current_volunteers)
    ids = [volunteer.volunteer_id for volunteer in pool]
    if len(set(ids)) != len(ids) or volunteer_id in ids:
        raise ValueError("Volunteer IDs must be unique")
    if pool:
        count = len(pool)
        acceptance_rate = fsum(v.acceptance_rate for v in pool) / count
        response_minutes = fsum(v.average_response_minutes for v in pool) / count
        elapsed_minutes = fsum(elapsed_since_request(v, now) for v in pool) / count
    else:
        acceptance_rate = DEFAULT_ACCEPTANCE_RATE
        response_minutes = DEFAULT_RESPONSE_MINUTES
        elapsed_minutes = DEFAULT_ELAPSED_MINUTES
    reference = now.astimezone(timezone.utc) - timedelta(minutes=elapsed_minutes)
    return Volunteer(
        volunteer_id=volunteer_id,
        acceptance_rate=acceptance_rate,
        average_response_minutes=response_minutes,
        last_requested_at=None,
        initial_scoring_reference_at=reference,
    )


@dataclass(frozen=True)
class ScoringConfig:
    k: float

    def __post_init__(self) -> None:
        _number("k", self.k, positive=True)


def volunteer_score(volunteer: Volunteer, now: datetime, config: ScoringConfig) -> float:
    """Compute 10*S/(S+k), where S = acceptance_rate * elapsed / response_time."""
    minutes_between(now, now)
    elapsed = elapsed_since_request(volunteer, now)
    raw_score = volunteer.acceptance_rate * elapsed / volunteer.average_response_minutes
    return 10.0 / (1.0 + config.k / raw_score) if raw_score else 0.0


def rank_volunteers(
    volunteers: Iterable[Volunteer], now: datetime, config: ScoringConfig,
    contacted_ids: Iterable[str] = (),
) -> list[Volunteer]:
    """Rank eligible volunteers; break score ties deterministically by ID."""
    pool = list(volunteers)
    if len({v.volunteer_id for v in pool}) != len(pool):
        raise ValueError("Volunteer IDs must be unique")
    excluded = set(contacted_ids)
    return sorted(
        (v for v in pool if v.volunteer_id not in excluded),
        key=lambda v: (-volunteer_score(v, now, config), v.volunteer_id),
    )


@dataclass(frozen=True)
class RequestPlan:
    volunteers: tuple[Volunteer, ...]
    expected_acceptances: float
    target: float

    @property
    def target_met(self) -> bool:
        return self.expected_acceptances >= self.target - 1e-12


def _check_spots(spots: int) -> None:
    if isinstance(spots, bool) or not isinstance(spots, int) or spots < 0:
        raise ValueError("spots must be a nonnegative integer")


@dataclass(frozen=True)
class UrgencyConfig:
    timescale_minutes: float = 120.0
    maximum_buffer: float = 0.5

    def __post_init__(self) -> None:
        _number("timescale_minutes", self.timescale_minutes, positive=True)
        _number("maximum_buffer", self.maximum_buffer)


def urgency_target(
    remaining_spots: int, minutes_remaining: float,
    urgency: UrgencyConfig = UrgencyConfig(),
) -> float:
    """Expected acceptance target R * (1 + b*h/(tau+h)).

    This is an expectation, not a text count or a guarantee. Set maximum_buffer
    to zero to recover the original target. Planners stop at the deadline even
    though the mathematical target remains positive there.
    """
    _check_spots(remaining_spots)
    _number("minutes_remaining", minutes_remaining)
    multiplier = 1.0 + urgency.maximum_buffer / (
        1.0 + minutes_remaining / urgency.timescale_minutes
    )
    return remaining_spots * multiplier


def _select_until_target(
    ranked: Iterable[Volunteer], target: float, expected: float,
    probability: Callable[[Volunteer], float],
) -> RequestPlan:
    selected: list[Volunteer] = []
    probabilities = [expected]
    for volunteer in ranked:
        if fsum(probabilities) >= target - 1e-12:
            break
        chance = probability(volunteer)
        _number("acceptance probability", chance)
        if chance > 1:
            raise ValueError("acceptance probability must be at most 1")
        selected.append(volunteer)
        probabilities.append(chance)
    return RequestPlan(tuple(selected), fsum(probabilities), target)


def plan_initial_batch(
    volunteers: Iterable[Volunteer], spots: int,
    now: datetime, config: ScoringConfig, *, deadline: datetime,
    urgency: UrgencyConfig = UrgencyConfig(),
) -> RequestPlan:
    """Select the shortest ranked prefix reaching the urgency-adjusted target.

    If the pool is insufficient, return the entire pool with target_met=False.
    The expectation is not a guarantee of filling the event.
    """
    _check_spots(spots)
    minutes_between(now, now)
    minutes_between(deadline, deadline)
    if spots == 0 or now >= deadline:
        return RequestPlan((), 0.0, float(spots))
    target = urgency_target(spots, minutes_between(deadline, now), urgency)
    return _select_until_target(
        rank_volunteers(volunteers, now, config), target, 0.0,
        lambda volunteer: volunteer.acceptance_rate,
    )


@dataclass(frozen=True)
class PendingRequest:
    volunteer: Volunteer
    requested_at: datetime


# Arguments: volunteer, minutes since text, minutes until filling deadline.
# Return P(accept by deadline | no reply yet), in [0, 1].
PendingProbability = Callable[[Volunteer, float, float], float]


def next_after_decline(
    volunteers: Iterable[Volunteer], contacted_ids: Iterable[str],
    remaining_spots: int, now: datetime, deadline: datetime,
    config: ScoringConfig,
) -> Volunteer | None:
    """Replace one newly received decline with the next uncontacted volunteer.

    Call once per unique decline, after updating the event's response records.
    Never recontact someone already requested for this event.
    """
    _check_spots(remaining_spots)
    minutes_between(now, now)
    minutes_between(deadline, deadline)
    if remaining_spots == 0 or now >= deadline:
        return None
    ranked = rank_volunteers(volunteers, now, config, contacted_ids)
    return ranked[0] if ranked else None


def plan_follow_up(
    volunteers: Iterable[Volunteer], pending: Iterable[PendingRequest],
    contacted_ids: Iterable[str], remaining_spots: int,
    now: datetime, deadline: datetime, config: ScoringConfig,
    probability_model: PendingProbability,
    urgency: UrgencyConfig = UrgencyConfig(),
) -> RequestPlan:
    """Plan additional requests when pending expected acceptances are insufficient.

    Pass remaining_spots after subtracting confirmed acceptances. contacted_ids
    must contain every previously contacted volunteer for this event, including
    declines and expired requests. Pending IDs are also excluded automatically.
    New requests use the same deadline model with elapsed time zero. The target
    includes the urgency buffer; pending expected acceptances count toward it.

    Record sends before calling this again. In a server, serialize decisions per
    event to prevent simultaneous workers from selecting the same volunteer.
    """
    _check_spots(remaining_spots)
    minutes_between(now, now)
    minutes_between(deadline, deadline)
    if remaining_spots == 0 or now >= deadline:
        return RequestPlan((), 0.0, remaining_spots)
    time_left = minutes_between(deadline, now)
    requests = list(pending)
    pending_ids = {request.volunteer.volunteer_id for request in requests}
    if len(pending_ids) != len(requests):
        raise ValueError("Only one pending request per volunteer is allowed")

    def chance(volunteer: Volunteer, elapsed: float) -> float:
        value = probability_model(volunteer, elapsed, time_left)
        _number("pending probability", value)
        if value > 1:
            raise ValueError("pending probability must be at most 1")
        return value

    expected = fsum(
        chance(request.volunteer, minutes_between(now, request.requested_at))
        for request in requests
    )
    ranked = rank_volunteers(
        volunteers, now, config, set(contacted_ids) | pending_ids,
    )
    return _select_until_target(
        ranked, urgency_target(remaining_spots, time_left, urgency), expected,
        lambda volunteer: chance(volunteer, 0.0),
    )


if __name__ == "__main__":
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    # Illustrative scaling constant; k is still to be chosen.
    config = ScoringConfig(k=100.0)
    volunteers: list[Volunteer] = []
    for i in range(1, 21):
        volunteers.append(create_volunteer(f"V{i:03}", volunteers, now))
    plan = plan_initial_batch(
        volunteers, spots=10, now=now, config=config,
        deadline=now + timedelta(minutes=30),
    )
    print(f"Urgency-adjusted target: {plan.target:.2f}")
    print(f"Request {len(plan.volunteers)} volunteers")
    print(f"Expected acceptances: {plan.expected_acceptances:.2f}")
    print(f"Expectation target met: {plan.target_met}")

"""Partial replies cannot manufacture split staffing or a PCO assignment."""
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core import offer_windows
from app.core.inbound import handle_inbound
from app.db import models as m
from app.integrations.planning_center import PCOBase, PCOStaffingIntent
from app.integrations.planning_center_staffing import CONTEXT, process_staffing_outbox
from tests.test_fill_agent import ScriptedAgentGloo, historical_invitation, parser_returning
from tests.test_planning_center import CONFIG
from tests.test_planning_center_staffing import setup_assignment


def test_complementary_partial_replies_leave_full_pco_slot_unfilled_without_write(
    session, clock, provider, make_volunteer, tmp_path
):
    PCOBase.metadata.create_all(session.get_bind())
    _, cancelled, api, factory = setup_assignment(session, clock, make_volunteer)
    cancelled.status = 'cancelled'  # Historical cancellation, not a new PCO transition.
    session.commit()
    shift = cancelled.shift
    interval = (shift.event.starts_at, shift.event.ends_at)
    fill = m.FillRequest(shift_id=shift.id, cancelled_assignment_id=cancelled.id,
        urgency='normal', state='in_progress', created_at=clock.now())
    session.add(fill)
    session.flush()
    early = make_volunteer('Fictional Early Helper')
    late = make_volunteer('Fictional Late Helper')
    offers = []
    # Use the actual imported 09:00-10:00 service, but keep every provider synthetic.
    replies = [(early, 'I can serve only 9 to 9:30', '09:00-09:30'),
               (late, 'I can serve only 9:30 to 10', '09:30-10:00')]
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo(), log_dir=tmp_path)
    for person, body, partial_window in replies:
        if offers:
            # The current policy allows one active invitation per slot. The
            # second synthetic invitation follows the first offer's deadline.
            clock.set_time(offer_windows.metadata(session, offers[-1]).expires_at)
            offer_windows.close(session, offers[-1], 'expired', clock.now())
        offer = historical_invitation(session, clock, person, fill)
        offers.append(offer)
        result = handle_inbound(session, clock, provider, person.phone, body,
            parser_returning(intent='partial', partial_window=partial_window), ctx=ctx)
        assert result.routed_to == 'fill_agent'
        assert offer.response == 'partial'
        assert fill.state != 'filled' and fill.closed_at is None
        assert session.scalar(select(m.Assignment).where(m.Assignment.status.in_(
            ('proposed', 'approved', 'confirmed')))) is None
    assert [offer.response for offer in offers] == ['expired', 'partial']
    assert (shift.event.starts_at, shift.event.ends_at) == interval
    assert session.scalars(select(m.Shift)).all() == [shift]
    assert session.scalars(select(m.Assignment)).all() == [cancelled]
    assert [message.body for message in session.scalars(select(m.Message).where(
        m.Message.direction == 'in').order_by(m.Message.id))] == [reply[1] for reply in replies]
    assert session.scalar(select(PCOStaffingIntent)) is None
    session.commit()
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert not api.writes and not provider.sent

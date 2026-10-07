"""Authenticated scoped automatic blockout policy and truthful sync status."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.exc import SQLAlchemyError

from app.core.planning_center_held_preview import signing_key
from app.integrations.planning_center import PlanningCenterError
from app.integrations.planning_center_blockouts import (
    blockout_status, issue_blockout_policy, load_blockout_acceptance,
)
from app.web.texty import admin

router = APIRouter()


class BlockoutPolicyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool


def _context(request):
    state = request.app.state
    key = signing_key(state.settings.pco_blockout_signing_key_path)
    acceptance = load_blockout_acceptance(state.settings.pco_blockout_acceptance_path, state.pco_config, key)
    return state, key, acceptance


def _enqueue_existing(session, state, volunteer_id, user):
    from app.core.planning_center_blockout_sources import enqueue_current_availability
    return enqueue_current_availability(session, state.settings, state.pco_config,
        volunteer_id=volunteer_id, user=user, clock=state.clock.now)


def _status(session, state, volunteer_id, key, acceptance):
    return blockout_status(session, state.pco_config, volunteer_id, signing_key=key,
        runtime_enabled=state.settings.pco_blockout_write_enabled, acceptance=acceptance, now=state.clock.now())


@router.get('/api/planning-center/blockouts/{volunteer_id}')
def status(volunteer_id: int, request: Request, user=Depends(admin)):
    if volunteer_id <= 0:
        raise HTTPException(422, 'Invalid volunteer ID.')
    state, key, acceptance = _context(request)
    try:
        with state.session_factory() as session:
            return _status(session, state, volunteer_id, key, acceptance)
    except SQLAlchemyError:
        raise HTTPException(503, {'reason':'blockout_store_unavailable'}) from None


@router.put('/api/planning-center/blockouts/{volunteer_id}/policy')
def policy(volunteer_id: int, data: BlockoutPolicyRequest, request: Request, user=Depends(admin)):
    if volunteer_id <= 0:
        raise HTTPException(422, 'Invalid volunteer ID.')
    state, key, acceptance = _context(request)
    try:
        with state.session_factory() as session:
            issue_blockout_policy(session, state.settings, state.pco_config, volunteer_id, user=user,
                clock=state.clock.now, signing_key=key, enabled=data.enabled, acceptance=acceptance)
            if data.enabled:
                _enqueue_existing(session, state, volunteer_id, user)
            session.commit()
            return _status(session, state, volunteer_id, key, acceptance)
    except PlanningCenterError as error:
        import re
        reason = str(error) if re.fullmatch(r'blockout_[a-z_]+',str(error)) else 'blockout_policy_not_verified'
        raise HTTPException(409, {'reason':reason}) from None
    except (SQLAlchemyError, ValueError, KeyError, TypeError, ImportError):
        raise HTTPException(503, {'reason':'blockout_store_or_acceptance_unavailable'}) from None

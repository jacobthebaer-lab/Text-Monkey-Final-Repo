"""Optional authenticated exact local role mapping, native GET-only."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.db.models import Event, Role, Shift
from app.integrations.planning_center import PCOClient, PCOEventLink, PCOShiftLink, PlanningCenterError, relation
from app.integrations.planning_center_role_bindings import KEY, configure_session, propose_role_binding, apply_role_binding
from app.web.texty import admin

def enabled(request: Request):
    settings = getattr(request.app.state, 'settings', None)
    if settings is None or not settings.pco_position_mapping_enabled:
        raise HTTPException(404, 'Not found.')


router = APIRouter(dependencies=[Depends(enabled)])


class Mapping(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    shift_id: int = Field(gt=0)
    local_role_id: int = Field(gt=0)
    team_id: str = Field(pattern=r'^[1-9][0-9]*$')
    position_id: str = Field(pattern=r'^[1-9][0-9]*$')
    plan_time_id: str = Field(pattern=r'^[1-9][0-9]*$')


class ReviewToken(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document: str = Field(max_length=4096)
    signature: str = Field(pattern=r'^[a-f0-9]{64}$')


class ReviewedMapping(Mapping):
    review_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    review_token: ReviewToken


def _call(request, user, mapping, *, apply=False):
    state = request.app.state
    if not state.settings.pco_position_mapping_enabled:
        raise HTTPException(503, 'Reviewed role mapping is not configured.')
    with state.session_factory() as session:
        configure_session(session, state.settings)
        if session.info[KEY] is None:
            raise HTTPException(503, 'Reviewed role mapping is not configured.')
        try:
            with PCOClient(state.pco_config) as client:
                operation = apply_role_binding if apply else propose_role_binding
                result = operation(session, client, state.pco_config, state.settings,
                    user=user, now=state.mac_delivery_clock.now(), **mapping)
            if apply:
                session.commit()
            else:
                session.rollback()
            return result
        except PlanningCenterError as error:
            session.rollback()
            raise HTTPException(409, str(error)) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            session.rollback()
            raise HTTPException(409, 'Native or local mapping context is invalid.') from None


@router.post('/api/planning-center/role-bindings/proposal')
def proposal(mapping: Mapping, request: Request, user=Depends(admin)):
    return _call(request, user, mapping.model_dump())


@router.get('/api/planning-center/role-bindings/catalogue')
def catalogue(request: Request, user=Depends(admin)):
    state = request.app.state
    from app.core.planning_center_held_preview import signing_key
    if not state.settings.pco_position_mapping_enabled or signing_key(state.settings.pco_review_signing_key_path) is None:
        raise HTTPException(503, 'Reviewed role mapping is not configured.')
    config = state.pco_config
    try:
        config.require_scope()
        with PCOClient(config) as client:
            if str(client.organization()['id']) != config.organization_id:
                raise PlanningCenterError('role_binding_organization_mismatch')
            positions = []
            for service in config.service_type_ids:
                for row in client.collection(f'/services/v2/service_types/{service}/team_positions'):
                    if row.get('type') != 'TeamPosition' or not relation(row, 'team'):
                        raise PlanningCenterError('role_binding_native_catalogue_invalid')
                    positions.append({'service_type_id': service, 'position_id': row['id'],
                        'team_id': relation(row, 'team'), 'name': row['attributes']['name']})
        with state.session_factory() as session:
            rows = session.execute(select(Shift, Event, PCOEventLink).join(Event, Shift.event_id == Event.id)
                .join(PCOEventLink, PCOEventLink.event_id == Event.id).join(PCOShiftLink, PCOShiftLink.shift_id == Shift.id)
                .where(PCOEventLink.organization_id == config.organization_id,
                    PCOEventLink.service_type_id.in_(config.service_type_ids),
                    Event.starts_at > state.mac_delivery_clock.now()).order_by(Event.starts_at, Shift.id).limit(100)).all()
            roles = list(session.scalars(select(Role).order_by(Role.name)))
            return {'shifts': [{'id': shift.id, 'title': event.title, 'role_id': shift.role_id,
                    'service_type_id': link.service_type_id, 'plan_time_id': link.key.rsplit(':', 1)[-1]}
                    for shift, event, link in rows],
                'roles': [{'id': role.id, 'name': role.name, 'required_qualifications': role.required_qualifications}
                    for role in roles if not role.name.startswith('PCO ')],
                'positions': positions, 'execution_enabled': False, 'native_writes': False}
    except PlanningCenterError as error:
        raise HTTPException(409, str(error)) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(409, 'Native or local mapping catalogue is invalid.') from None


@router.post('/api/planning-center/role-bindings')
def apply(mapping: ReviewedMapping, request: Request, user=Depends(admin)):
    return _call(request, user, mapping.model_dump(), apply=True)

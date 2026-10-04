"""Optional authenticated review-only router; deliberately not registered."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.core import planning_center_frequency_reviews as reviews
from app.core.planning_center_committed_source import committed_source_factory
from app.core.planning_center_held_preview import capture_held_preview, require_review_schema
from app.integrations.planning_center import PlanningCenterError
from app.web.texty import admin

router = APIRouter()


class ExactReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    remote_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    operation_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


def review_db(request: Request, user=Depends(admin)):
    try:
        require_review_schema(request.app.state.engine)
    except PlanningCenterError:
        raise HTTPException(503, 'The reviewed schema is incomplete or incompatible.') from None
    with committed_source_factory(request.app.state.engine)() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def context(request):
    state = request.app.state
    return dict(settings=state.settings, config=state.pco_config,
                clock=lambda: datetime.now(timezone.utc))


class CapturePreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    volunteer_id: int = Field(gt=0, strict=True)


@router.post('/api/planning-center/held-previews')
def capture(data: CapturePreview, request: Request, user=Depends(admin)):
    try:
        state = request.app.state
        return capture_held_preview(committed_source_factory(state.engine),
            volunteer_id=data.volunteer_id, user=user, **context(request))
    except PlanningCenterError as exc:
        import re
        reason = str(exc) if re.fullmatch('[a-z_]+', str(exc)) else 'review_capture_not_verified'
        raise HTTPException(503 if reason == 'review_schema_incomplete_or_incompatible' else 409,
                            {'reason': reason, 'execution_enabled': False}) from None
    except SQLAlchemyError:
        raise HTTPException(503, 'The committed review store is unavailable.') from None


@router.get('/api/planning-center/frequency-reviews/{intent_key}')
def proposal(intent_key: str, request: Request, user=Depends(admin), session=Depends(review_db)):
    try:
        return reviews.review_proposal(session, intent_key=intent_key, user=user, **context(request))
    except PlanningCenterError:
        raise HTTPException(409, 'The committed proposal requires fresh review.') from None
    except SQLAlchemyError:
        raise HTTPException(503, 'The reviewed schema is not available.') from None


@router.post('/api/planning-center/frequency-reviews/{intent_key}')
def acknowledge(intent_key: str, data: ExactReview, request: Request,
                user=Depends(admin), session=Depends(review_db)):
    try:
        return reviews.issue_review_receipt(session, intent_key=intent_key, user=user,
            expected=data.model_dump(), signing_key=getattr(request.app.state, 'pco_review_signing_key', None),
            **context(request))
    except PlanningCenterError:
        raise HTTPException(409, 'The exact authenticated proposal or signing configuration is not verified.') from None
    except SQLAlchemyError:
        raise HTTPException(503, 'The reviewed schema is not available.') from None

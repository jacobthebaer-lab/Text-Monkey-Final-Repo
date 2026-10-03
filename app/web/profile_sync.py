"""Read-only authenticated local queue status; no cloud/transport operation."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from app.core.profile_sync import approved_phones
from app.integrations.profile_models import ProfileOutbox
from app.web.routes import db
from app.web.texty import admin

router = APIRouter()


@router.get('/api/profile-sync')
def profile_status(request: Request, user=Depends(admin), session=Depends(db)):
    settings = request.app.state.settings
    rows = session.scalars(select(ProfileOutbox).where(ProfileOutbox.phone.in_(approved_phones(settings)))
                           .order_by(ProfileOutbox.created_at.desc()).limit(100)).all() if settings.profile_sync_enabled else []
    return {'enabled': settings.profile_sync_enabled, 'publisher': 'explicit_only',
            'records': [{'key': row.key, 'state': row.state, 'detail': row.detail,
                         'attempts': row.attempts, 'created_at': row.created_at.isoformat(),
                         'synced_at': row.synced_at.isoformat() if row.synced_at else None}
                        for row in rows]}

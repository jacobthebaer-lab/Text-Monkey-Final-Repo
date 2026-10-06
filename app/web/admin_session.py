"""Supabase token rotation, preserving upstream lifetime and coordinator policy."""
import json
import math

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pathlib import Path

from app.web.texty import bridge, check_user, allowed_emails

router = APIRouter()


def token_payload(result, settings):
    try:
        user = check_user(result.get('user', {}), settings)
        if not all(isinstance(result.get(key), str) and result[key] for key in ('access_token', 'refresh_token')):
            raise ValueError()
        # Return expiry granted by Supabase; no locally invented token lifetime.
        expiry = {key: result[key] for key in ('expires_at', 'expires_in') if key in result}
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in expiry.values()):
            raise ValueError()
        return {'access_token': result['access_token'], 'refresh_token': result['refresh_token'],
                'email': user['email'], **expiry}
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(503, 'Supabase returned an incomplete session. Try again.') from None


@router.get('/coordinator-session.js', include_in_schema=False)
def asset():
    return FileResponse(Path(__file__).resolve().parents[2] / 'web/texty/public/coordinator-session.js')


@router.post('/api/session/refresh')
async def refresh(request: Request, response: Response):
    bridge(request)
    settings = request.app.state.settings
    response.headers['Cache-Control'] = 'no-store'
    if not settings.supabase_url or not settings.supabase_publishable_key or not allowed_emails(settings):
        raise HTTPException(503, 'Supabase coordinator login is not configured yet.')
    try:
        raw = await request.body()
        if len(raw)>8192:
            raise ValueError()
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {'refresh_token'} or
                not isinstance(data['refresh_token'], str) or not 1<=len(data['refresh_token'])<=4096):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, 'Submit a valid session refresh request.') from None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            upstream = await client.post(settings.supabase_url+'/auth/v1/token?grant_type=refresh_token',
                headers={'apikey': settings.supabase_publishable_key},json={'refresh_token': data['refresh_token']})
        if upstream.status_code==429:
            raise HTTPException(429, 'Sign-in is rate limited. Please wait and try again.')
        if upstream.status_code in (400,401,403,422):
            raise HTTPException(401, 'Session expired. Sign in again.')
        if upstream.status_code!=200:
            raise HTTPException(503, 'Supabase sign-in is temporarily unavailable.')
        return token_payload(upstream.json(), settings)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Supabase sign-in is temporarily unavailable.') from None

"""Planning Center webhook receiver — the 'live update' half of the sync.

PCO signs each delivery with HMAC-SHA256 of the raw body using the
subscription's authenticity secret, sent in X-PCO-Webhooks-Authenticity.
A valid delivery triggers an immediate full sync; the polling job remains
the fallback for missed deliveries.
"""

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.integrations import pco

logger = logging.getLogger("pco_webhook")

router = APIRouter()


@router.post("/pco/webhook")
async def pco_webhook(
    request: Request,
    x_pco_webhooks_authenticity: str | None = Header(default=None),
) -> JSONResponse:
    settings = request.app.state.settings
    if not settings.pco_enabled or not settings.pco_webhook_secret:
        raise HTTPException(503, "Planning Center webhooks are not configured")

    body = await request.body()
    expected = hmac.new(
        settings.pco_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, x_pco_webhooks_authenticity or ""):
        logger.warning("rejected PCO webhook with bad signature")
        raise HTTPException(401, "invalid webhook signature")

    state = request.app.state
    session = state.session_factory()
    try:
        result = pco.full_sync(session, pco.PCOClient(settings), state.clock)
        session.commit()
        logger.info("PCO webhook sync: %s", result)
    except (pco.PCOError, Exception):
        session.rollback()
        logger.exception("PCO webhook sync failed")
        # 200 anyway: PCO retries on non-2xx and the polling fallback covers us.
        return JSONResponse({"ok": False})
    finally:
        session.close()
    return JSONResponse({"ok": True, **result})

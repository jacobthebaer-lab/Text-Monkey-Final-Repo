"""Authenticated Planning Center webhook; schedule import only, no texting."""
import hashlib
import hmac
import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from app.integrations.planning_center import PCOConfig, PCOClient, PCODelivery, PlanningCenterError, sync_schedule

router = APIRouter()
_lock = threading.Lock()
MAX_BODY = 1_000_000


def apply_deliveries(app, config, deliveries):
    with _lock, app.state.session_factory() as session:
        results = []
        for delivery in deliveries:
            ident = delivery.get("id")
            attrs = delivery.get("attributes", {})
            name = attrs.get("name", "")
            org_id = str((delivery.get("relationships", {}).get("organization", {}).get("data") or {}).get("id", ""))
            if delivery.get("type") != "EventDelivery" or not isinstance(ident, str) or not 1 <= len(ident) <= 100 or not isinstance(name, str) or len(name) > 160:
                raise ValueError("Invalid delivery envelope")
            if org_id != config.organization_id:
                raise ValueError("Webhook organization differs from configured scope")
            key = config.organization_id + ":" + ident
            if session.get(PCODelivery, key):
                results.append({"id": ident, "status": "duplicate"})
                continue
            allowed = any(name.startswith("services.v2.events." + resource + ".") for resource in ("plan", "plan_time", "needed_position", "team"))
            if allowed:
                with PCOClient(config) as client:
                    result = sync_schedule(session, client, config)
                status = "synced"
            else:
                result, status = {}, "ignored"
            session.add(PCODelivery(key=key, event_name=name, received_at=datetime.now(timezone.utc), result=result))
            results.append({"id": ident, "status": status})
        session.commit()
        return {"deliveries": results}


@router.post("/integrations/planning-center/webhook")
async def receive_webhook(request: Request):
    config = getattr(request.app.state, "pco_config", None) or PCOConfig.from_env()
    if not config.webhook_secret:
        raise HTTPException(503, "Planning Center webhook is not configured", headers={"Retry-After": "60"})
    try:
        config.require_scope()
    except PlanningCenterError:
        raise HTTPException(503, "Planning Center import scope is not configured") from None
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_BODY:
            raise HTTPException(413, "Webhook too large")
    raw = bytes(chunks)
    signature = request.headers.get("X-PCO-Webhooks-Authenticity", "")
    expected = hmac.new(config.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid Planning Center signature")
    try:
        data = json.loads(raw)["data"]
        if not isinstance(data, list) or not 1 <= len(data) <= 100 or any(not isinstance(d, dict) for d in data):
            raise ValueError()
        return await run_in_threadpool(apply_deliveries, request.app, config, data)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid Planning Center delivery") from None
    except (PlanningCenterError, SQLAlchemyError):
        raise HTTPException(503, "Planning Center sync failed; delivery will be retried", headers={"Retry-After": "60"}) from None

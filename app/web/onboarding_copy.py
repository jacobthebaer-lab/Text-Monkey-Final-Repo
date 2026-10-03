"""Verified admin copy drafts and a standalone editor. No Gloo or delivery calls."""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.onboarding_copy import DEFAULTS, copy_key, role_options, validate_messages
from app.db import models as m
from app.web.admin_setup import now, owner, payload
from app.web.routes import db
from app.web.texty import admin

router = APIRouter(tags=["Onboarding message copy"])
STATIC = Path(__file__).resolve().parents[2] / "web/texty/public"


def snapshot(session, row):
    value = row.value if row else {}
    return {"messages": value.get("messages", DEFAULTS), "defaults": DEFAULTS,
            "revision": value.get("revision", 0), "saved_at": value.get("saved_at"),
            "scope": "administrator_draft", "texts_sent": 0,
            "preview": {"first_name": "Alex", "roles": role_options(session)}}


@router.get("/api/setup/onboarding-copy")
def get_copy(response: Response, user=Depends(admin), session=Depends(db)):
    response.headers["Cache-Control"] = "no-store"
    return snapshot(session, session.get(m.Policy, copy_key(owner(user))))


@router.post("/api/setup/onboarding-copy")
async def save_copy(request: Request, response: Response, user=Depends(admin), session=Depends(db)):
    response.headers["Cache-Control"] = "no-store"
    data = await payload(request)
    if set(data) != {"messages", "revision"} or type(data.get("revision")) is not int or data["revision"] < 0:
        raise HTTPException(422, "Provide messages and the saved revision only.")
    try:
        messages = validate_messages(data["messages"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    key = copy_key(owner(user))
    row = session.scalar(select(m.Policy).where(m.Policy.key == key).with_for_update())
    current = row.value if row else {}
    revision = current.get("revision", 0)
    if data["revision"] != revision:
        raise HTTPException(409, "Copy changed in another tab. Reload saved copy before saving.")
    value = {"messages": messages, "revision": revision + 1, "saved_at": now().isoformat()}
    if row is None:
        row = m.Policy(key=key, value=value)
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "Copy was saved by another request. Reload saved copy before saving.")
    else:
        # CAS also prevents lost updates on SQLite, which ignores FOR UPDATE.
        result = session.execute(update(m.Policy).where(m.Policy.key == key,
            m.Policy.value["revision"].as_integer() == revision).values(value=value),
            execution_options={"synchronize_session": False})
        if result.rowcount != 1:
            raise HTTPException(409, "Copy changed in another tab. Reload saved copy before saving.")
        session.expire(row)
    return snapshot(session, row)


@router.get("/onboarding-copy.html")
@router.get("/onboarding-copy.js")
@router.get("/onboarding-copy-nav.js")
@router.get("/onboarding-copy.css")
@router.get("/onboarding-copy-defaults.json")
def editor_asset(request: Request):
    return FileResponse(STATIC / request.url.path.lstrip("/"))

"""Read-only Calendar import. Never creates, updates or deletes Google events."""
import re
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.db import models as m
from app.config import get_settings

SCOPES=["https://www.googleapis.com/auth/calendar.readonly"]

def client(settings):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    if not settings.google_service_account_json or not settings.google_calendar_id:
        raise ValueError("Calendar ID and service-account JSON file path are required")
    credentials=Credentials.from_service_account_file(settings.google_service_account_json,scopes=SCOPES)
    return build("calendar","v3",credentials=credentials,cache_discovery=False)

def event_time(value, tz):
    if "dateTime" in value:
        dt=datetime.fromisoformat(value["dateTime"].replace("Z","+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(value.get("timeZone",tz)))
    return datetime.combine(datetime.fromisoformat(value["date"]).date(),time(),ZoneInfo(tz))

def sync(ctx, service=None, settings=None):
    settings=settings or get_settings();service=service or client(settings);s=ctx.session
    token=None;counts={"created":0,"updated":0,"unknown":0,"cancelled":0,"protected":0}
    while True:
        response=service.events().list(calendarId=settings.google_calendar_id,timeMin=ctx.clock.now().isoformat(),timeMax=(ctx.clock.now()+timedelta(weeks=8)).isoformat(),singleEvents=True,showDeleted=True,orderBy="startTime",pageToken=token).execute()
        for item in response.get("items",[]):
            event=s.scalar(select(m.Event).where(m.Event.gcal_event_id==item["id"]))
            if item.get("status")=="cancelled":
                if event:
                    event.status="cancelled";counts["cancelled"]+=1
                    s.add(m.Escalation(category="unclear",summary=f"Calendar cancelled {event.title}; coordinator reviews assignments.",related_ids={"event_id":event.id},severity="normal",status="open",created_at=ctx.clock.now()))
                continue
            start=event_time(item["start"],settings.church_timezone);end=event_time(item["end"],settings.church_timezone)
            if end<=start:continue
            title=item.get("summary","Untitled event")[:200]
            et=next((t for t in s.scalars(select(m.EventType)) if any(re.search(pattern,title,re.I) for pattern in t.title_patterns)),None)
            if event:
                if (event.starts_at!=start or event.ends_at!=end or event.event_type_id!=(et.id if et else None)) and any(a.status in ("proposed","approved","confirmed") for sh in event.shifts for a in sh.assignments):
                    counts["protected"]+=1
                    if not any(e.related_ids.get("gcal_event_id")==item["id"] for e in s.scalars(select(m.Escalation).where(m.Escalation.category=="unclear",m.Escalation.status=="open"))):
                        s.add(m.Escalation(category="unclear",summary=f"Calendar changed {title}; assigned event protected pending human review.",related_ids={"event_id":event.id,"gcal_event_id":item["id"]},severity="normal",status="open",created_at=ctx.clock.now()))
                    continue
                event.title=title;event.starts_at=start;event.ends_at=end;counts["updated"]+=1
            else:
                event=m.Event(gcal_event_id=item["id"],title=title,starts_at=start,ends_at=end,event_type_id=et.id if et else None,status="scheduled");s.add(event);s.flush();counts["created"]+=1
            if et and event.event_type_id is None:event.event_type_id=et.id
            if et:
                for recipe in s.scalars(select(m.RoleRecipe).where(m.RoleRecipe.event_type_id==et.id)):
                    existing={sh.slot_index for sh in event.shifts if sh.role_id==recipe.role_id}
                    for slot in range(recipe.count):
                        if slot not in existing:s.add(m.Shift(event_id=event.id,role_id=recipe.role_id,slot_index=slot))
            elif not any(e.related_ids.get("event_id")==event.id for e in s.scalars(select(m.Escalation).where(m.Escalation.category=="unknown_event",m.Escalation.status=="open"))):
                s.add(m.Escalation(category="unknown_event",summary=f"What staffing does {title} need? Coordinator must choose a recipe.",related_ids={"event_id":event.id},severity="normal",status="open",created_at=ctx.clock.now()));counts["unknown"]+=1
            s.flush()
        token=response.get("nextPageToken")
        if not token:break
    return counts

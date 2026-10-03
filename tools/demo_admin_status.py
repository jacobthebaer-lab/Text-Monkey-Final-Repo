#!/usr/bin/env python3
"""Fictional admin-status demonstration; delivery is always in-memory mock."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

# Run directly from any directory without installing the application package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Do not implicitly load credentials/transport settings from a local .env.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.clock import FakeClock
from app.config import Settings
from app.core.notifications import staffing_snapshot
from app.db import models as m
from app.db.session import init_db, make_engine, make_session_factory
from app.jobs import process_due_fill_requests
from app.llm.gloo_client import GlooClient, GlooUnavailableError
from app.sms.mock_provider import MockSMSProvider

TZ = ZoneInfo("America/Denver")


class SyntheticComposer:
    """Scripted fixture, NOT Gloo AI and never evidence of a real model call."""
    settings = Settings(sms_provider="mock", live_sms=False)

    def __init__(self, unavailable=False):
        self.calls = 0
        self.unavailable = unavailable

    def create_response(self, **kwargs):
        self.calls += 1
        if self.unavailable:
            raise GlooUnavailableError("Synthetic outage fixture")
        return SimpleNamespace(output_text=json.loads(kwargs["input"])["approved_message"])


class Demo:
    def __init__(self, gloo, hour=10):
        self.engine = make_engine("sqlite:///:memory:")
        init_db(self.engine)
        self.sessions = make_session_factory(self.engine)
        self.session = self.sessions()
        self.clock = FakeClock(datetime(2026, 10, 4, hour, tzinfo=TZ))
        self.provider = MockSMSProvider()  # Never use configurable get_provider().
        self.gloo = gloo
        self.timeline = []
        self.admin = self.volunteer("Fictional Demo Admin", "+12025550101", True)
        self.worker = self.volunteer("Fictional Demo Volunteer", "+12025550102")

    def volunteer(self, name, phone, coordinator=False):
        person = m.Volunteer(name=name, phone=phone, sms_opt_in=True,
                             is_coordinator=coordinator, status="active",
                             preferences={}, created_at=self.clock.now())
        self.session.add(person)
        self.session.flush()
        return person

    def event(self, title, roles, lead=timedelta(hours=3)):
        event = m.Event(title=title, starts_at=self.clock.now()+lead,
                        ends_at=self.clock.now()+lead+timedelta(hours=1), status="scheduled")
        self.session.add(event)
        self.session.flush()
        shifts = []
        for name in roles:
            role = m.Role(name=name, ministry="Fictional Demo", criticality="standard",
                          fill_policy="needs_approval" if name == "Kids check-in" else "auto",
                          required_qualifications=[])
            self.session.add(role)
            self.session.flush()
            shift = m.Shift(event_id=event.id, role_id=role.id, slot_index=0)
            self.session.add(shift)
            self.session.flush()
            shifts.append(shift)
        return event, shifts

    def cover(self, shift):
        self.session.add(m.Assignment(shift_id=shift.id, volunteer_id=self.worker.id,
                                     status="confirmed", source="admin",
                                     created_at=self.clock.now(), updated_at=self.clock.now()))
        self.session.flush()

    def fill(self, shift, state):
        fill = m.FillRequest(shift_id=shift.id, state=state, urgency="high",
                             created_at=self.clock.now(),
                             next_action_at=self.clock.now()+timedelta(hours=2))
        self.session.add(fill)
        self.session.flush()
        return fill

    def tick(self, label):
        # Match a scheduler reading committed UTC records, not uncommitted seed objects.
        self.session.commit()
        self.session.expire_all()
        process_due_fill_requests(FillContext(self.session, self.clock, self.provider, self.gloo))
        self.session.commit()
        rows = self.session.scalars(select(m.Notification).order_by(m.Notification.key)).all()
        self.timeline.append({"step": label, "fake_time": self.clock.now().isoformat(),
                              "mock_text_count": len(self.provider.sent),
                              "notifications": [{"state": r.state, "due_at": r.due_at.isoformat(),
                                                 "message_id": r.message_id} for r in rows]})

    def restart_and_check_dedup(self):
        before = len(self.provider.sent)
        # Fresh session/context against the same durable notification records.
        self.session.close()
        self.session = self.sessions()
        self.tick("Repeated tick with fresh application session")
        assert len(self.provider.sent) == before, "Duplicate admin text"

    def evidence(self, name, event, checks):
        event = self.session.get(m.Event, event.id)
        messages = self.session.scalars(select(m.Message).order_by(m.Message.id)).all()
        assert all(msg.to == self.admin.phone for msg in self.provider.sent)
        assert all(msg.purpose == "coordinator_notify" for msg in messages)
        result = {"scenario": name, "event": event.title,
                  "event_start": event.starts_at.isoformat(),
                  "staffing": staffing_snapshot(self.session, event), "checks_passed": checks,
                  "timeline": self.timeline, "mock_delivery": [asdict(s) for s in self.provider.sent],
                  "application_messages": [{"id": s.id, "status": s.status, "kind": s.kind,
                                            "purpose": s.purpose} for s in messages]}
        self.session.close()
        self.engine.dispose()
        return result


def run(gloo):
    results = []
    demo = Demo(gloo)
    event, shifts = demo.event("DEMO ONLY: All set", ["Greeter"], timedelta(hours=3, minutes=1))
    demo.cover(shifts[0])
    demo.tick("One minute before the three-hour boundary")
    assert not demo.provider.sent
    demo.clock.advance(timedelta(minutes=1))
    demo.tick("Exactly three hours before the event")
    assert len(demo.provider.sent) == 1
    body = demo.provider.sent[0].body
    assert "All set:" in body and "All 1 required spots" in body and "No action needed" in body
    demo.restart_and_check_dedup()
    results.append(demo.evidence("all_set", event, ["three-hour boundary", "readiness", "no action needed", "dedup after session restart"]))

    demo = Demo(gloo)
    event, shifts = demo.event("DEMO ONLY: Replacement underway", ["Greeter"])
    demo.fill(shifts[0], "in_progress")
    demo.tick("Three hours before: replacement search already in progress")
    assert len(demo.provider.sent) == 1
    body = demo.provider.sent[0].body
    assert "0/1" in body and "Greeter (1)" in body and "1 replacement search(es)" in body
    assert "No action needed while those searches continue" in body
    demo.restart_and_check_dedup()
    results.append(demo.evidence("replacement_search", event, ["exact staffing gap", "existing search progress", "no unnecessary admin work", "dedup"]))

    demo = Demo(gloo)
    event, shifts = demo.event("DEMO ONLY: Approval and help", ["Greeter", "Kids check-in", "Sound"])
    demo.fill(shifts[0], "in_progress")
    restricted = demo.fill(shifts[1], "waiting_approval")
    demo.fill(shifts[2], "escalated")
    approval = m.Approval(kind="send_outreach", payload={"fill_request_id": restricted.id},
                          status="pending", requested_at=demo.clock.now())
    demo.session.add(approval)
    demo.session.flush()
    demo.tick("Three hours before: one search, one pending approval, one escalation")
    assert len(demo.provider.sent) == 1
    body = demo.provider.sent[0].body
    assert "0/3" in body and "1 replacement search(es)" in body
    assert f"Reply YES A{approval.id}" in body and "NO to decline" in body
    assert "1 search(es) need your help; review Text Monkey" in body
    assert "No action needed" not in body and approval.status == "pending"
    demo.restart_and_check_dedup()
    results.append(demo.evidence("approval_and_help", event, ["exact gaps", "approval code", "specific next step", "approval remains pending", "dedup"]))

    demo = Demo(gloo, hour=6)
    event, shifts = demo.event("DEMO ONLY: Quiet hours", ["Greeter"])
    demo.tick("06:00: three-hour update held by quiet hours")
    row = demo.session.scalar(select(m.Notification))
    assert not demo.provider.sent and row.state == "pending"
    assert row.due_at.astimezone(TZ).hour == 6 and row.due_at.astimezone(TZ).minute == 30
    # Staffing changes while the update is held; its eventual copy must be fresh.
    demo.cover(shifts[0])
    demo.clock.advance(timedelta(minutes=29))
    demo.tick("06:29: before the initial urgent retry")
    assert not demo.provider.sent
    demo.clock.advance(timedelta(minutes=30))
    demo.tick("06:59: still held")
    assert not demo.provider.sent
    assert row.due_at.astimezone(TZ).hour == 7
    demo.clock.advance(timedelta(minutes=1))
    demo.tick("07:00: quiet hours end; compose from current staffing")
    assert len(demo.provider.sent) == 1 and "All set:" in demo.provider.sent[0].body
    demo.restart_and_check_dedup()
    results.append(demo.evidence("quiet_hours_fresh_facts", event, ["quiet-hours defer", "no early retry", "fresh staffing at retry", "dedup"]))

    # A deliberately failing fixture tests the application policy even in real-Gloo mode.
    demo = Demo(SyntheticComposer(unavailable=True))
    event, _ = demo.event("DEMO ONLY: Synthetic Gloo outage", ["Greeter"])
    for attempt in range(3):
        demo.tick(f"Synthetic unavailable composition attempt {attempt+1}")
        demo.clock.advance(timedelta(minutes=2))
    assert not demo.provider.sent
    assert demo.session.scalar(select(m.Notification)).state == "blocked"
    assert demo.session.scalar(select(m.Escalation)).category == "system_error"
    results.append(demo.evidence("synthetic_outage", event, ["no template fallback", "two-minute retries", "internal escalation after three failures"]))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-gloo", action="store_true", help="Explicitly allow real Gloo composition of fictional facts; transport remains mock")
    parser.add_argument("--env-file", type=Path, help="Explicit private credential file, only with --real-gloo")
    parser.add_argument("--output", type=Path, help="Write portable JSON evidence instead of printing it")
    args = parser.parse_args()
    if args.env_file and not args.real_gloo:
        parser.error("--env-file requires --real-gloo")
    gloo = SyntheticComposer()
    if args.real_gloo:
        from dotenv import dotenv_values
        values = dotenv_values(args.env_file) if args.env_file else os.environ
        key = values.get("GLOO_API_KEY", "")
        if not key:
            parser.error("--real-gloo requires GLOO_API_KEY (environment or explicit --env-file)")
        settings = Settings(gloo_api_key=key, gloo_endpoint=values.get("GLOO_ENDPOINT") or "guarded",
                            parser_model=values.get("PARSER_MODEL") or "gloo-openai-gpt-5-mini",
                            sms_provider="mock", live_sms=False)
        gloo = GlooClient(settings)
    results = run(gloo)
    evidence = {"schema_version": 1,
                "composition_mode": "real_gloo" if args.real_gloo else "synthetic_scripted_NO_REAL_AI",
                "transport": "in_memory_mock_NO_REAL_DELIVERY", "database": "isolated_in_memory_sqlite",
                "clock": "FakeClock", "job": "app.jobs.process_due_fill_requests",
                "synthetic_outage_is_always_scripted": True,
                "composition_usage": gloo.total_usage() if args.real_gloo else {"scripted_calls": gloo.calls, "real_calls": 0},
                "scenarios": results}
    rendered = json.dumps(evidence, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
        print(f"PASS: {len(results)} scenarios; {evidence['composition_mode']}; MOCK delivery only. Evidence: {args.output}")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()

"""Actual isolated Mac progress/publisher/completion boundary, no cloud or native calls."""
import json
from dataclasses import replace
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.core import profile_sync
from app.core.notifications import flush_due
from app.agents.fill_agent import FillContext
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.main import create_app
from tests.test_exact_signup_copy import ExactGloo
from tests.conftest import clock
from tests.test_mac_progress import progress_app,incoming,post,submit_ack,wait_worker


def test_completion_composition_outage_still_captures_publisher_snapshot(progress_app,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('No remote publisher connection in independent review')
    monkeypatch.setattr(profile_sync,'cloud_connection',forbidden)
    original=progress_app.state.gloo.create_response
    failed_completions=[]
    def failing_completion(**kwargs):
        facts=json.loads(kwargs['input'])
        if 'preferences are saved' in facts.get('approved_message',''):
            failed_completions.append(facts)
            raise GlooUnavailableError('Synthetic completion-only outage')
        return original(**kwargs)
    progress_app.state.gloo.create_response=failing_completion
    with progress_app.state.session_factory() as session:
        session.add(m.Role(name='Greeter',ministry='Welcome',required_qualifications=[],
            criticality='standard',fill_policy='auto'))
        session.commit()
    with TestClient(progress_app) as client:
        progress_app.state.settings=replace(progress_app.state.settings,profile_sync_enabled=True,profile_sync_phones=incoming()['phone'])
        accepted=post(client,'/mac/inbound',incoming()).json()
        ack=submit_ack(client)
        progress_app.state.gloo.release.set()
        post(client,'/mac/progress/tick');wait_worker(progress_app)
        assert not failed_completions
        with progress_app.state.session_factory() as session:
            job=session.get(m.Notification,accepted['progress_key'])
            assert job.state=='done'
            assert session.get(m.Notification,f'ordinary-reply:{job.message_id}').state=='pending'
            assert session.scalar(select(m.Volunteer)).preferences['onboarding_stage']=='complete'
            assert len(session.scalars(select(profile_sync.ProfileOutbox)).all())==1
        with progress_app.state.session_factory() as session:
            flush_due(FillContext(session,progress_app.state.clock,progress_app.state.provider,progress_app.state.gloo));session.commit()
        with progress_app.state.session_factory() as session:
            job=session.get(m.Notification,accepted['progress_key'])
            assert job.state=='done' and job.detail.get('route')=='onboarding_complete',json.dumps(job.detail,sort_keys=True)
            notice=session.get(m.Notification,f'ordinary-reply:{job.message_id}')
            assert notice.state=='pending' and notice.detail['gloo_attempts']==1
            due=notice.due_at;notice_key=notice.key
            rows=session.scalars(select(profile_sync.ProfileOutbox)).all()
            assert len(rows)==1 and rows[0].state=='pending'
            assert rows[0].payload['route']=='onboarding_complete'
            assert rows[0].payload['profile']['preferences']['onboarding_stage']=='complete'
            outgoing=session.scalars(select(m.Message).where(m.Message.direction=='out')).all()
            assert len(outgoing)==1 and outgoing[0].id==ack['id'] and outgoing[0].status=='submitted'
            assert session.scalar(select(m.Assignment)) is None

    assert len(failed_completions)==1
    # Restart the app from the committed database, never replaying interpretation.
    replacement=create_app(replace(progress_app.state.settings))
    replacement.state.clock=replacement.state.mac_delivery_clock=progress_app.state.clock
    replacement.state.gloo=ExactGloo()
    replacement.state.clock.set_time(due)
    with TestClient(replacement) as client:
        assert post(client,'/mac/inbound',incoming()).json()['duplicate']
        post(client,'/mac/progress/tick');wait_worker(replacement)
        assert not replacement.state.gloo.calls
        with replacement.state.session_factory() as session:
            ctx=FillContext(session,replacement.state.clock,replacement.state.provider,replacement.state.gloo)
            flush_due(ctx);session.commit()
            notice=session.get(m.Notification,notice_key)
            assert notice.state=='sent' and notice.message_id is not None
            completion_id=notice.message_id
            assert session.get(m.Message,completion_id).status=='queued'
            assert len(session.scalars(select(profile_sync.ProfileOutbox)).all())==1
        batch=post(client,'/mac/outbound/pull').json()['messages']
        assert len(batch)==1 and batch[0]['id']==completion_id
        item=batch[0]
        assert post(client,f'/mac/outbound/{completion_id}/verify',{'token':item['token']}).status_code==200
        assert post(client,f'/mac/outbound/{completion_id}/ack',{'token':item['token'],'outcome':'submitted'}).status_code==200
        with replacement.state.session_factory() as session:
            flush_due(FillContext(session,replacement.state.clock,replacement.state.provider,replacement.state.gloo));session.commit()
            outgoing=session.scalars(select(m.Message).where(m.Message.direction=='out')).all()
            assert len(outgoing)==2 and all(row.status=='submitted' for row in outgoing)
        assert post(client,'/mac/outbound/pull').json()['messages']==[]
        assert len(replacement.state.gloo.calls)==1

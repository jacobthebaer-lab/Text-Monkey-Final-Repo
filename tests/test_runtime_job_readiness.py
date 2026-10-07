"""Actual scheduler lifecycle, with disposable SQLite and no external jobs."""
from dataclasses import replace
from types import SimpleNamespace
import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_admin_text_settings import live_admin_client, enable
from tests.test_admin_setup import save


class Scheduler:
    def __init__(self):
        self.jobs = {}
        self.running = False
        self.state = 0
        self.shutdowns = []

    def add_job(self, function, trigger, **kwargs):
        assert trigger == 'interval'
        assert kwargs['max_instances'] == 1 and kwargs['coalesce'] is True
        self.jobs[kwargs['id']] = SimpleNamespace(func=function, **kwargs)

    def get_jobs(self):
        return list(self.jobs.values())

    def start(self):
        self.running = True
        self.state = 1
        for job in self.jobs.values():
            job.next_run_time = datetime.now(timezone.utc)

    def shutdown(self, wait):
        self.shutdowns.append(wait)
        self.running = False
        self.state = 0


@pytest.fixture
def scheduler(monkeypatch):
    created = []
    def factory():
        instance = Scheduler()
        created.append(instance)
        return instance
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler', factory)
    return created


@pytest.mark.parametrize('demo,automation,pco,blockouts,expected', [
    (True, True, False, False, []),
    (False, False, False, False, []),
    (False, True, False, False, ['fill_tick']),
    (False, False, True, False, ['pco_staffing_tick']),
    (False, False, False, True, ['pco_blockout_tick']),
    (False, True, True, True, ['fill_tick', 'pco_blockout_tick', 'pco_staffing_tick']),
])
def test_status_tracks_started_jobs_and_shutdown(tmp_path, scheduler, demo, automation, pco, blockouts, expected):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path}/jobs.sqlite',
        demo_mode=demo, automation_enabled=automation, pco_sync_enabled=pco,
        pco_blockout_write_enabled=blockouts))
    with TestClient(app) as client:
        status = client.get('/api/config').json()
        assert status['automationRunning'] is ('fill_tick' in expected)
        assert status['backgroundJobs'] == expected
        if scheduler:
            assert scheduler[0].running
            assert scheduler[0].jobs.get('fill_tick', SimpleNamespace(seconds=30)).seconds == 30
            for name in ('pco_staffing_tick', 'pco_blockout_tick'):
                if name in expected: assert scheduler[0].jobs[name].seconds == 60
    assert getattr(app.state, 'background_scheduler', None) is None
    if scheduler: assert scheduler[0].shutdowns == [False]
    app.state.engine.dispose()


def test_flag_change_does_not_invent_running_scheduler(live_admin_client):
    client, app = live_admin_client
    save(client, complete=True)
    enable(client)
    app.state.settings = replace(app.state.settings, automation_enabled=True)
    status = client.get('/api/setup/admin-texts').json()
    scheduling = next(check for check in status['checks'] if check['code'] == 'scheduler')
    assert not scheduling['ready']
    assert 'not running' in scheduling['detail'].lower()
    config = client.get('/api/config').json()
    assert config['automationEnabled'] is True  # Configuration, not process proof.
    assert config['automationRunning'] is False


def test_registered_pco_job_never_establishes_text_scheduling(live_admin_client):
    client, app = live_admin_client
    save(client, complete=True)
    enable(client)
    app.state.settings = replace(app.state.settings, automation_enabled=True)
    isolated = Scheduler()
    isolated.jobs['pco_blockout_tick'] = SimpleNamespace(id='pco_blockout_tick')
    isolated.start()
    app.state.background_scheduler = isolated
    status = client.get('/api/setup/admin-texts').json()
    assert not next(check for check in status['checks'] if check['code'] == 'scheduler')['ready']
    assert client.get('/api/config').json()['backgroundJobs'] == ['pco_blockout_tick']


def test_stopped_scheduler_cannot_keep_ready_status(live_admin_client):
    client, app = live_admin_client
    save(client, complete=True)
    enable(client)
    isolated = Scheduler()
    isolated.jobs['fill_tick'] = SimpleNamespace(id='fill_tick')
    isolated.start()
    app.state.background_scheduler = isolated
    assert next(check for check in client.get('/api/setup/admin-texts').json()['checks']
                if check['code'] == 'scheduler')['ready']
    isolated.shutdown(wait=False)
    assert not next(check for check in client.get('/api/setup/admin-texts').json()['checks']
                    if check['code'] == 'scheduler')['ready']
    assert client.get('/api/config').json()['automationRunning'] is False


def test_exceptional_lifespan_shutdown_cleans_scheduler(tmp_path, scheduler):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path}/exception.sqlite',
                              demo_mode=False, automation_enabled=True))
    async def interrupt():
        async with app.router.lifespan_context(app):
            raise RuntimeError('synthetic lifespan interruption')
    with pytest.raises(RuntimeError, match='synthetic lifespan interruption'):
        asyncio.run(interrupt())
    assert scheduler[0].shutdowns == [False]
    assert getattr(app.state, 'background_scheduler', None) is None
    app.state.engine.dispose()


@pytest.mark.parametrize('paused', ['scheduler', 'job'])
def test_paused_timer_cannot_claim_ready_even_while_scheduler_running(live_admin_client, paused):
    client, app = live_admin_client
    isolated = Scheduler()
    isolated.jobs['fill_tick'] = SimpleNamespace(id='fill_tick')
    isolated.start()
    if paused == 'scheduler': isolated.state = 2  # APScheduler.running remains True.
    else: isolated.jobs['fill_tick'].next_run_time = None
    app.state.background_scheduler = isolated
    assert client.get('/api/config').json()['automationRunning'] is False
    assert not next(check for check in client.get('/api/setup/admin-texts').json()['checks']
                    if check['code'] == 'scheduler')['ready']


@pytest.mark.parametrize('fault', ['signup_start', 'acceptance_start', 'scheduler_start', 'signup_stop', 'acceptance_stop', 'scheduler_stop'])
def test_partial_startup_and_cleanup_faults_close_every_started_owner(tmp_path, monkeypatch, fault):
    calls = []
    failure = RuntimeError('synthetic lifecycle failure')
    def service(name):
        def run(state):
            calls.append(name)
            if name == fault: raise failure
        return run
    monkeypatch.setattr('app.integrations.google_voice_signup.start_service', service('signup_start'))
    monkeypatch.setattr('app.integrations.google_voice_signup.stop_service', service('signup_stop'))
    monkeypatch.setattr('app.integrations.acceptance_workflow.start_service', service('acceptance_start'))
    monkeypatch.setattr('app.integrations.acceptance_workflow.stop_service', service('acceptance_stop'))
    class FaultScheduler(Scheduler):
        def start(self):
            super().start()
            calls.append('scheduler_start')
            if fault == 'scheduler_start': raise failure
        def shutdown(self, wait):
            super().shutdown(wait)
            calls.append('scheduler_stop')
            if fault == 'scheduler_stop': raise failure
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler', FaultScheduler)
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path}/fault.sqlite', demo_mode=False,
                             automation_enabled=True, google_voice_signup_enabled=True))
    async def run():
        async with app.router.lifespan_context(app): pass
    with pytest.raises(RuntimeError) as observed: asyncio.run(run())
    assert observed.value is failure
    assert 'signup_stop' in calls
    if 'acceptance_start' in calls: assert 'acceptance_stop' in calls
    if 'scheduler_start' in calls: assert 'scheduler_stop' in calls
    assert app.state.background_scheduler is None
    app.state.engine.dispose()


def test_primary_lifespan_error_survives_multiple_cleanup_failures(tmp_path, monkeypatch, scheduler):
    calls = []
    def broken(name):
        def stop(state):
            calls.append(name)
            raise RuntimeError('synthetic cleanup error')
        return stop
    monkeypatch.setattr('app.integrations.google_voice_signup.stop_service', broken('signup'))
    monkeypatch.setattr('app.integrations.acceptance_workflow.stop_service', broken('acceptance'))
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path}/primary.sqlite', demo_mode=False,
                             automation_enabled=True))
    original = ValueError('synthetic primary error')
    async def run():
        async with app.router.lifespan_context(app): raise original
    with pytest.raises(ValueError) as observed: asyncio.run(run())
    assert observed.value is original and calls == ['signup', 'acceptance']
    assert scheduler[0].shutdowns == [False]
    assert app.state.background_scheduler is None
    app.state.engine.dispose()

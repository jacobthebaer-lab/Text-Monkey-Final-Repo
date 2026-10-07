"""Actual scheduler lifecycle, with disposable SQLite and no external jobs."""
from dataclasses import replace
from types import SimpleNamespace
import asyncio

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
        self.shutdowns = []

    def add_job(self, function, trigger, **kwargs):
        assert trigger == 'interval'
        assert kwargs['max_instances'] == 1 and kwargs['coalesce'] is True
        self.jobs[kwargs['id']] = SimpleNamespace(func=function, **kwargs)

    def get_jobs(self):
        return list(self.jobs.values())

    def start(self):
        self.running = True

    def shutdown(self, wait):
        self.shutdowns.append(wait)
        self.running = False


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

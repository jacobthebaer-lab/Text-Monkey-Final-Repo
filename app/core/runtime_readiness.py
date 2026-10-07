"""Read current in-process timers without starting jobs or making requests."""


def running_jobs(state):
    from apscheduler.schedulers.base import STATE_RUNNING
    scheduler = getattr(state, 'background_scheduler', None)
    if scheduler is None or scheduler.state != STATE_RUNNING:
        return []
    return sorted(job.id for job in scheduler.get_jobs() if job.next_run_time is not None)

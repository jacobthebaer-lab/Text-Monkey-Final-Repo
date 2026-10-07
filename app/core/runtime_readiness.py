"""Read current in-process timers without starting jobs or making requests."""


def running_jobs(state):
    scheduler = getattr(state, 'background_scheduler', None)
    if scheduler is None or not scheduler.running:
        return []
    return sorted(job.id for job in scheduler.get_jobs())

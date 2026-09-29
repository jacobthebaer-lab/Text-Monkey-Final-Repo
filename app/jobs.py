"""Background job definitions.

Phase 4: the fill-request tick. APScheduler wiring (a BackgroundScheduler
calling these every minute on the real clock) lands with the web app in
Phase 5; tests and demo fast-forward call them directly with the fake clock.
"""

from app.agents import fill_agent


def process_due_fill_requests(ctx: fill_agent.FillContext) -> list:
    """Advance every fill request whose tranche timer has expired."""
    return fill_agent.advance_due(ctx)

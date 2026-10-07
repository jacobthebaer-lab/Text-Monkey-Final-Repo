"""Run the scoped API bridge against an existing private Text Monkey store.

Credentials stay in ignored env files. Postgres migrations are a separate step.
This worker does not enable transport, create accounts, or send texts.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import make_engine, make_session_factory
from app.integrations.planning_center import PCOClient, PCOConfig, PlanningCenterError, sync_schedule
from app.integrations.planning_center_sync import (
    refresh_mapped_availability, sync_linked_events, sync_mapped_names,
)


def sync_once(factory, client, config, now, *, event_writes=False, profile_writes=False):
    with factory() as session:
        imported = sync_schedule(session, client, config, create_only=True)
        availability = refresh_mapped_availability(session, client, config, now)
        session.commit()
    return {'import': imported, 'availability': availability,
        'events': sync_linked_events(factory, client, config, now, write_enabled=event_writes),
        'profiles': sync_mapped_names(factory, client, config, now, write_enabled=profile_writes)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', action='append', required=True)
    parser.add_argument('--database', help='Optional explicit store URL; otherwise DATABASE_URL in env files')
    parser.add_argument('--expected-org', required=True)
    parser.add_argument('--expected-project', help='Required for Supabase, e.g. the existing project reference')
    parser.add_argument('--event-writes', action='store_true')
    parser.add_argument('--profile-writes', action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--journal', required=True, help='Private JSON heartbeat, never a file inside the repository')
    args = parser.parse_args()
    if args.interval < 30:
        parser.error('--interval must be at least 30 seconds')
    values = {}
    for path in args.env_file:
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    config = PCOConfig(values.get('PCO_APP_ID', ''), values.get('PCO_SECRET', ''),
        values.get('PCO_ORGANIZATION_ID', ''),
        tuple(x.strip() for x in values.get('PCO_SERVICE_TYPE_IDS', '').split(',') if x.strip()))
    config.require_scope()
    if config.organization_id != args.expected_org:
        parser.error('Configured organization differs from --expected-org')
    database = args.database or values.get('DATABASE_URL', '')
    if not database:
        parser.error('An existing store URL is required')
    if not database.startswith('sqlite'):
        from urllib.parse import urlparse
        parsed = urlparse(database)
        user = parsed.username or ''
        if (not args.expected_project or
                not (user.endswith('.' + args.expected_project) or
                     parsed.hostname == 'db.' + args.expected_project + '.supabase.co')):
            parser.error('Supabase destination does not match --expected-project')
    journal = Path(args.journal).resolve()
    repo = Path(__file__).resolve().parents[1]
    if journal.is_relative_to(repo):
        parser.error('Journal must be outside the repository')
    engine = make_engine(database)
    schema = None if database.startswith('sqlite') else 'texty'
    if not inspect(engine).has_table('pco_event_links', schema=schema):
        parser.error('Apply the existing PCO migration to the selected store first')
    factory = make_session_factory(engine)
    journal.parent.mkdir(parents=True, exist_ok=True)
    from app.integrations.mac_messages import atomic_json
    try:
        while True:
            now = datetime.now(timezone.utc)
            try:
                with PCOClient(config) as client:
                    result = sync_once(factory, client, config, now,
                        event_writes=args.event_writes, profile_writes=args.profile_writes)
                receipt = {'status': 'running' if args.watch else 'complete', 'checked_at': now.isoformat(),
                    'organization_id': config.organization_id, 'service_type_ids': config.service_type_ids,
                    'event_writes': args.event_writes, 'profile_writes': args.profile_writes, 'result': result}
            except PlanningCenterError as error:
                receipt = {'status': 'held', 'checked_at': now.isoformat(), 'reason': str(error)}
            except (SQLAlchemyError, OSError):
                receipt = {'status': 'held', 'checked_at': now.isoformat(),
                    'reason': 'Private scheduling store unavailable; no delivery enabled'}
            atomic_json(journal, receipt)
            if not args.watch:
                print(json.dumps(receipt)); break
            time.sleep(args.interval)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()

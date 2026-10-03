"""Explicit bounded Supabase profile publisher; no Gloo, Messages or scheduler.

Use private --scope-file with phones/project_ref/optional role_map. Target DSN
comes from an existing private env file and is never printed. Default is status.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', required=True, help='Existing private SQLite URL; never reset')
    parser.add_argument('--scope-file', required=True, type=Path, help='Private JSON recipient/project scope')
    parser.add_argument('--target-env-file', type=Path, help='Existing private env file containing DATABASE_URL')
    parser.add_argument('--initialize-local', action='store_true', help='Create only the additive local queue table')
    parser.add_argument('--catch-up-guid', help='Existing accepted Mac profile receipt, never replayed')
    parser.add_argument('--phone', help='Approved catch-up recipient; must be present in private scope')
    parser.add_argument('--publish', action='store_true', help='Explicitly publish pending/failed rows to configured cloud')
    parser.add_argument('--identity-only', action='store_true', help='Publish identity/consent; retain preferences pending')
    parser.add_argument('--retry-held', action='store_true', help='Explicitly retry held rows after reviewed mapping/correction')
    parser.add_argument('--watch', action='store_true', help='Independently drain scoped profile queue; no scheduler or SMS')
    parser.add_argument('--interval', type=float, default=10, help='Watch interval seconds, at least 5')
    parser.add_argument('--cycles', type=int, help='Optional bounded watch cycles for verification')
    parser.add_argument('--limit', type=int, default=1)
    args = parser.parse_args(argv)
    if not args.source_db.startswith('sqlite:///') or args.source_db in ('sqlite:///', 'sqlite:///:memory:'):
        parser.error('Use the existing private SQLite file URL')
    if (args.identity_only or args.retry_held or args.watch) and not args.publish:
        parser.error('Publisher modes require --publish')
    if args.interval < 5 or (args.cycles is not None and args.cycles < 1):
        parser.error('Use interval >= 5 and positive cycles')
    cloud_engine = None
    local_engine = None
    try:
        from dotenv import dotenv_values
        from sqlalchemy import inspect, select
        from app.config import Settings
        from app.db.session import make_engine, make_session_factory
        from app.integrations.profile_models import ProfileBase, ProfileOutbox
        from app.core.profile_sync import approved_phones, catch_up, cloud_connection, publish_pending
        scope = json.loads(args.scope_file.read_text())
        phones = scope['phones']
        if not isinstance(phones, list) or not phones or any(not isinstance(phone, str) for phone in phones):
            raise ValueError('invalid_scope')
        settings = Settings(profile_sync_enabled=True, profile_sync_phones=','.join(phones),
                    profile_sync_project_ref=scope['project_ref'],
                    profile_sync_role_map=json.dumps(scope.get('role_map', {})))
        approved_phones(settings)
        # Refuse SQLite's normal behavior of creating a missing source database.
        from sqlalchemy.engine import make_url
        source_path = Path(make_url(args.source_db).database)
        if not source_path.is_file() or source_path.is_symlink():
            raise ValueError('source_database_missing')
        local_engine = make_engine(args.source_db)
        if args.initialize_local:
            ProfileBase.metadata.create_all(local_engine)
        if not inspect(local_engine).has_table('profile_sync_outbox'):
            print(json.dumps({'storage': 'queue_not_initialized'}))
            return 2
        with make_session_factory(local_engine)() as local:
            if args.catch_up_guid:
                if not args.phone:
                    raise ValueError('catch_up_recipient_required')
                catch_up(local, settings, phone=args.phone, guid=args.catch_up_guid)
                local.commit()
            if args.publish:
                if not args.target_env_file:
                    raise ValueError('target_configuration_required')
                private = dotenv_values(args.target_env_file)
                target = private.get('PROFILE_SYNC_DATABASE_URL') or private.get('DATABASE_URL') or ''
                settings = replace(settings, profile_sync_database_url=target)
                cloud_engine, factory = cloud_connection(settings)
                cycles = 0
                while True:
                    # Re-read scope each pass so removal takes effect without restart.
                    fresh = json.loads(args.scope_file.read_text())
                    if fresh.get('project_ref') != settings.profile_sync_project_ref:
                        raise ValueError('target_scope_changed')
                    new_phones = fresh['phones']
                    if not isinstance(new_phones, list) or any(not isinstance(phone, str) for phone in new_phones):
                        raise ValueError('invalid_scope')
                    settings = replace(settings, profile_sync_phones=','.join(new_phones),
                        profile_sync_role_map=json.dumps(fresh.get('role_map', {})))
                    approved_phones(settings)
                    result = publish_pending(local, factory, settings, limit=args.limit,
                        identity_only=args.identity_only, retry_held=args.retry_held)
                    if args.watch and result:
                        print(json.dumps({'records': result}), flush=True)
                    cycles += 1
                    if not args.watch or (args.cycles is not None and cycles >= args.cycles):
                        break
                    time.sleep(args.interval)
            rows = local.scalars(select(ProfileOutbox).where(ProfileOutbox.phone.in_(approved_phones(settings)))
                                 .order_by(ProfileOutbox.created_at.desc()).limit(100)).all()
            print(json.dumps({'records': [{'key': row.key, 'state': row.state, 'detail': row.detail,
                                          'attempts': row.attempts} for row in rows]}))
    except KeyboardInterrupt:
        return 0
    except Exception:
        parser.exit(2, 'Profile operation held; check approved scope, local storage and private target configuration.\n')
    finally:
        if cloud_engine is not None:
            cloud_engine.dispose()
        if local_engine is not None:
            local_engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

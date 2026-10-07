"""Explicit database-only registration of future connected-app volunteers.

Initialize a private baseline before enabling watch. No scheduler, Gloo,
Messages, provider configuration, existing-profile backfill or migrations.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def save_journal(path, journal):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.', delete=False) as stream:
        temporary = stream.name
        json.dump(journal, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', required=True, type=Path)
    parser.add_argument('--scope-file', required=True, type=Path)
    parser.add_argument('--target-env-file', required=True, type=Path)
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=float, default=10)
    args = parser.parse_args()
    if not args.source_db.is_file() or args.source_db.is_symlink() or args.interval < 5:
        parser.error('Use an existing source file and interval >= 5')
    if args.journal.is_symlink() or args.scope_file.is_symlink():
        parser.error('Private configuration must not be a symlink')
    from dotenv import dotenv_values
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.config import Settings
    from app.core.cloud_registration import initialize, scan
    from app.core.profile_sync import cloud_connection
    scope = json.loads(args.scope_file.read_text())
    values = dotenv_values(args.target_env_file)
    settings = replace(Settings(), profile_sync_project_ref=scope['project_ref'],
        profile_sync_database_url=values.get('PROFILE_SYNC_DATABASE_URL') or values.get('DATABASE_URL') or '')
    cloud_engine, factory = cloud_connection(settings)
    source = str(args.source_db.resolve())
    # SQLite mode=ro guarantees this worker cannot enqueue or alter local data.
    local_engine = create_engine('sqlite:///file:' + quote(source) + '?mode=ro&uri=true')
    try:
        with open(str(args.journal) + '.lock', 'a', opener=lambda name, flags: os.open(name, flags, 0o600)) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.initialize:
                if args.journal.exists():
                    parser.error('Registration journal already exists; never rebaseline it')
                with sessionmaker(bind=local_engine)() as local:
                    journal = initialize(local, source=source, project_ref=scope['project_ref'])
                stat = args.source_db.stat()
                journal['source_file_id'] = [stat.st_dev, stat.st_ino]
                with open(args.journal, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
                    json.dump(journal, stream)
                print(json.dumps({'state': 'initialized', 'existing_profiles_excluded': len(journal['baseline'])}))
                return
            journal = json.loads(args.journal.read_text())
            while True:
                stat = args.source_db.stat()
                if journal['source_file_id'] != [stat.st_dev, stat.st_ino] or args.source_db.is_symlink():
                    raise ValueError('Source database replaced; registration requires review')
                fresh = json.loads(args.scope_file.read_text())
                with sessionmaker(bind=local_engine)() as local:
                    results = scan(local, factory, journal, source=source,
                        project_ref=fresh['project_ref'], role_map=fresh.get('role_map', {}))
                journal['last_successful_scan'] = datetime.now(timezone.utc).isoformat()
                save_journal(args.journal, journal)
                if results or not args.watch:
                    print(json.dumps({'records': results}), flush=True)
                if not args.watch:
                    break
                time.sleep(args.interval)
    finally:
        local_engine.dispose()
        cloud_engine.dispose()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        sys.exit('Cloud registration stopped; check private configuration and journal. No texts were sent.')

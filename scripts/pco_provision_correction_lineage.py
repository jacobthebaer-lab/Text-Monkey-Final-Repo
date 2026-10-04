"""Private exact-manifest check/provision. Never call this from a runtime route.

No sender, worker, Gloo client, PCO HTTP client or schema creator is imported.
Default is check-only. --apply needs the exact manifest SHA explicitly accepted
by the coordinator after independent code/evidence review and a DB backup.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.config import settings_from_env
from app.core.planning_center_committed_source import committed_source_factory
from app.core.planning_center_correction_lineage import database_identity, owned_database, fail, provision
from app.core.planning_center_held_preview import private_file
from app.db import models as m
from app.integrations.planning_center import PCOConfig, PlanningCenterError
from app.integrations.planning_center_availability import _hash, _json


def accepted_manifest(path, accepted_sha):
    raw = private_file(path, 65536)
    if hashlib.sha256(raw).hexdigest() != accepted_sha:
        fail('accepted_manifest_digest_changed')
    try:
        value = json.loads(raw)
        if raw != _json(value).encode():
            fail('manifest_noncanonical')
        return value
    except (ValueError, TypeError):
        fail('manifest_schema_invalid')


def selected_engine(database_url):
    """Existing exact SQLite file only, including protection against deletion.

    mode=rw refuses creation if the validated path disappears before connect.
    A write reservation is required by the committed reader, including checks;
    no source DML or schema operation is performed in check-only mode.
    """
    try:
        url = make_url(database_url)
        if url.get_backend_name() != 'sqlite' or url.database in (None, '', ':memory:') or url.query:
            raise ValueError()
        path = owned_database(url.database)
        def connect():
            owned_database(path)
            return sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
        return create_engine(url.set(database=str(path)),
            creator=connect)
    except (ValueError, OSError, TypeError):
        fail('existing_file_sqlite_required')


def native_observer(config_path, native):
    """Only one exact selected native row, no polling or history replay."""
    try:
        config = json.loads(private_file(config_path, 65536))
        path = Path(config['messages_db']).expanduser().resolve(strict=True)
        receiving = config['receiving_number']
        phones, services = config['phones'], config['services']
        if (not isinstance(receiving, str) or not receiving or not isinstance(phones, list) or
                not phones or not isinstance(services, list) or not services or
                not set(services) <= {'iMessage', 'SMS'}):
            fail('native_configuration_invalid')
    except (ValueError, KeyError, TypeError, OSError):
        fail('native_configuration_invalid')
    def observe(session, target):
        volunteer = session.get(m.Volunteer, target['volunteer_id'])
        message = session.get(m.Message, target['message_id'])
        if volunteer.phone not in phones:
            fail('native_recipient_not_selected')
        try:
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
                conn.execute('PRAGMA query_only=ON'); conn.row_factory = sqlite3.Row
                rows = conn.execute('''SELECT DISTINCT m.ROWID AS row_id, m.guid, h.id AS phone,
                    m.text, m.attributedBody, m.service FROM message m
                    JOIN handle h ON h.ROWID=m.handle_id
                    JOIN chat_message_join cm ON cm.message_id=m.ROWID
                    JOIN chat c ON c.ROWID=cm.chat_id
                    WHERE m.ROWID=? AND m.guid=? AND m.is_from_me=0 AND h.id=?
                    AND c.service_name=m.service AND m.destination_caller_id=?
                    AND EXISTS(SELECT 1 FROM chat_handle_join ch WHERE ch.chat_id=c.ROWID AND ch.handle_id=m.handle_id)
                    AND (SELECT COUNT(*) FROM chat_handle_join ch WHERE ch.chat_id=c.ROWID)=1''',
                    (native['row_id'], target['guid'], volunteer.phone, receiving)).fetchall()
                if len(rows) != 1 or rows[0]['service'] not in services:
                    fail('native_message_not_unique')
                row = rows[0]; body = row['text']
                if body is None and row['attributedBody']:
                    from app.integrations.mac_messages import decode_body
                    body = decode_body(row['attributedBody'], config['decoder'])
                if not isinstance(body, str) or body != message.body or len(body) > 1600:
                    fail('native_body_changed')
                fingerprint = hashlib.sha256((volunteer.phone+'\0'+row['service']+'\0'+target['session_id']+'\0'+body).encode()).hexdigest()
                if fingerprint != target['fingerprint']:
                    fail('native_fingerprint_changed')
                return {'database_identity_hash': database_identity(path), 'row_id': row['row_id'],
                    'guid': row['guid'], 'service': row['service'], 'body_hash': _hash(body),
                    'sender_hash': _hash(volunteer.phone), 'receiving_line_hash': _hash(receiving)}
        except (sqlite3.Error, OSError, ValueError, KeyError):
            fail('native_read_unavailable')
    return observe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--accepted-manifest-sha256', required=True)
    parser.add_argument('--mac-config', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = settings_from_env()
        manifest = accepted_manifest(args.manifest, args.accepted_manifest_sha256)
        engine = selected_engine(settings.database_url)
        observer = native_observer(args.mac_config, manifest['binding']['native'])
        factory = committed_source_factory(engine)
        with factory() as session:
            result = provision(session, settings, PCOConfig.from_env(), manifest=manifest,
                accepted_manifest_sha256=args.accepted_manifest_sha256,
                clock=lambda: datetime.now(timezone.utc), native_observer=observer, apply=args.apply)
            if args.apply:
                session.commit()
            else:
                session.rollback()
        print(_json(result))
        return 0
    except Exception as exc:
        # Native/HTTP/database failures must never echo credentials or content.
        reason = str(exc) if isinstance(exc, PlanningCenterError) else 'correction_lineage_private_check_failed'
        print(_json({'applied': False, 'reason': reason}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())

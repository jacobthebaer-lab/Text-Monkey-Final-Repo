"""Read one explicitly scoped Supabase profile into a new private receipt, never write DB."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope-file', type=Path, required=True)
    parser.add_argument('--target-env-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    engine = None
    try:
        from dotenv import dotenv_values
        from sqlalchemy import select, text
        from app.config import Settings
        from app.core.profile_sync import cloud_connection
        from app.db import models as m
        scope = json.loads(args.scope_file.read_text())
        if len(scope['phones']) != 1:
            raise ValueError('singleton_scope_required')
        private = dotenv_values(args.target_env_file)
        settings = Settings(profile_sync_project_ref=scope['project_ref'],
            profile_sync_database_url=private.get('PROFILE_SYNC_DATABASE_URL') or private.get('DATABASE_URL') or '')
        engine, factory = cloud_connection(settings)
        with factory() as session:
            session.execute(text('SET TRANSACTION READ ONLY'))
            people = session.scalars(select(m.Volunteer).where(m.Volunteer.phone == scope['phones'][0])).all()
            result = {'project_ref': scope['project_ref'], 'same_phone_count': len(people), 'profiles': []}
            for person in people:
                qualifications = session.scalars(select(m.Qualification).where(m.Qualification.volunteer_id == person.id)).all()
                availability = session.scalars(select(m.Availability).where(m.Availability.volunteer_id == person.id)).all()
                result['profiles'].append({'id': person.id, 'name': person.name, 'phone': person.phone,
                    'sms_opt_in': person.sms_opt_in, 'status': person.status,
                    'is_coordinator': person.is_coordinator, 'is_pastor': person.is_pastor,
                    'preferences': person.preferences,
                    'qualifications': [{'type': q.type, 'status': q.status, 'verified_by': q.verified_by,
                        'verified_at': str(q.verified_at), 'expires_on': str(q.expires_on)} for q in qualifications],
                    'availability': [{'month': a.month, 'available_dates': a.available_dates,
                        'unavailable_dates': a.unavailable_dates} for a in availability]})
            session.rollback()
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as receipt:
            json.dump(result, receipt, indent=2, default=str)
        print(json.dumps({'same_phone_count': len(people), 'private_receipt_written': True}))
        return 0
    except Exception:
        parser.exit(2, 'Baseline held; check private singleton scope, intended project and read-only database access.\n')
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())

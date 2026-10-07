"""Persist new connected-app profiles without granting transport permissions.

The explicit baseline excludes existing fixtures. A private, durable journal
binds this worker to one source database and Supabase project. Existing cloud
profiles belonging to another writer are never overwritten.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select

from app.core import profile_sync
from app.db import models as m
from app.integrations.profile_models import ProfileOutbox


def identity_key(phone):
    return hashlib.sha256(phone.encode()).hexdigest()


def initialize(local, *, source, project_ref):
    return {'version': 1, 'source': source, 'project_ref': project_ref,
            'source_id': str(uuid4()),
            'baseline': [identity_key(phone) for phone in local.scalars(select(m.Volunteer.phone))],
            'records': {}}


def identity_profile(volunteer):
    if (not re.fullmatch(r'\+[1-9][0-9]{7,14}', volunteer.phone)
            or not volunteer.name.strip() or len(volunteer.name) > 120):
        raise profile_sync.ProfileHeld('invalid_identity')
    prefs = volunteer.preferences or {}
    return {'name': volunteer.name, 'phone': volunteer.phone,
            'sms_opt_in': volunteer.sms_opt_in, 'status': volunteer.status,
            'preferences': {key: prefs[key] for key in profile_sync.IDENTITY_KEYS if key in prefs},
            'availability': []}


def register_one(local, cloud_factory, journal, volunteer, role_map):
    identity = identity_profile(volunteer)
    full = profile_sync.safe_snapshot(local, volunteer.phone)
    incomplete = (full.get('_held') or full.get('pending_constraints')
                  or full.get('availability_draft') is not None
                  or full.get('preferences', {}).get('onboarding_stage') != 'complete')
    profile = identity if full.get('_held') else full
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    key = hashlib.sha256((journal['source_id'] + volunteer.phone + digest).encode()).hexdigest()
    row = ProfileOutbox(key=key, source_id=journal['source_id'], source_guid='registration:' + digest,
        phone=volunteer.phone, created_at=datetime.now(timezone.utc),
        payload={'profile': profile, 'route': 'signup_complete' if volunteer.sms_opt_in else 'stop',
                 'changed': ['name', 'sms_opt_in', 'status', 'preferences', 'availability'],
                 'preference_keys': list(profile['preferences']), 'preference_removals': [],
                 'availability_months': [a['month'] for a in profile['availability']]})
    with cloud_factory() as cloud:
        with cloud.begin():
            existing = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == volunteer.phone)
                                    .with_for_update())
            marker = (existing.preferences or {}).get(profile_sync.MARKER, {}) if existing else {}
            if existing and marker.get('source_id') != journal['source_id']:
                return {'state': 'existing_preserved', 'cloud_id': existing.id, 'digest': digest}
            # Avoid needless writes, but do not skip checking cloud ownership.
            if marker.get('key') == key and marker.get('scope') == 'full':
                return {'state': 'saved', 'cloud_id': existing.id, 'digest': digest}
            cloud_id, _ = profile_sync._apply(cloud, row, role_map, identity_only=bool(incomplete))
            saved = cloud.get(m.Volunteer, cloud_id)
            assert saved.phone == volunteer.phone and saved.name == volunteer.name
    return {'state': 'profile_pending' if incomplete else 'saved', 'cloud_id': cloud_id, 'digest': digest}


def scan(local, cloud_factory, journal, *, source, project_ref, role_map):
    if journal.get('version') != 1 or journal.get('source') != source or journal.get('project_ref') != project_ref:
        raise profile_sync.ProfileHeld('registration_binding_mismatch')
    baseline = set(journal['baseline'])
    results = []
    for volunteer in local.scalars(select(m.Volunteer).order_by(m.Volunteer.id)):
        key = identity_key(volunteer.phone)
        if key in baseline:
            continue
        try:
            result = register_one(local, cloud_factory, journal, volunteer, role_map)
        except profile_sync.ProfileHeld as error:
            # Even unresolved role mapping must not prevent a new identity from
            # being stored. Retry full preferences when they can be validated.
            if str(error).startswith(('unresolved_', 'ambiguous_cloud_')):
                try:
                    identity = identity_profile(volunteer)
                    row = ProfileOutbox(key=hashlib.sha256((journal['source_id'] + key + 'identity').encode()).hexdigest(),
                        source_id=journal['source_id'], source_guid='registration:identity', phone=volunteer.phone,
                        created_at=datetime.now(timezone.utc), payload={'profile': identity,
                        'route': 'signup_complete' if volunteer.sms_opt_in else 'stop',
                        'changed': ['name', 'sms_opt_in', 'status', 'preferences'],
                        'preference_keys': list(identity['preferences']), 'preference_removals': [],
                        'availability_months': []})
                    with cloud_factory() as cloud:
                        with cloud.begin():
                            existing = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == volunteer.phone))
                            if existing is not None:
                                result = {'state': 'profile_pending', 'cloud_id': existing.id, 'detail': str(error)}
                            else:
                                cloud_id, _ = profile_sync._apply(cloud, row, role_map, identity_only=True)
                                result = {'state': 'profile_pending', 'cloud_id': cloud_id, 'detail': str(error)}
                except Exception:
                    result = {'state': 'retry', 'detail': 'cloud_save_failed'}
            else:
                result = {'state': 'held', 'detail': str(error)}
        except Exception:
            result = {'state': 'retry', 'detail': 'cloud_save_failed'}
        previous = journal['records'].get(key)
        journal['records'][key] = result
        if previous != result:
            results.append(result)
    return results

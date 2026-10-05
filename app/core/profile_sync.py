"""Bounded sender-profile replication, separate from scheduling and Messages.

Only validated sender fields are queued. A cloud transaction merges by phone,
keeps privilege fields untouched, and records its idempotency marker in the same
volunteer row. No cloud schema, auth user, credential or transport is created.
"""
from datetime import date, datetime, timezone
import hashlib
import json
import re
from uuid import uuid4
from urllib.parse import urlsplit, unquote

from sqlalchemy import case, create_engine, select
from sqlalchemy.orm import sessionmaker
from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from app.integrations.profile_models import ProfileOutbox

MARKER = '_text_monkey_profile_sync'
PROFILE_ROUTES = {'signup_consent_pending', 'signup_complete', 'onboarding_interests',
                  'onboarding_availability', 'onboarding_complete', 'onboarding_clarify',
                  'signup_declined', 'stop', 'start', 'availability', 'onboarding_review'}
PREFERENCE_KEYS = {'signup_source', 'consent_pending', 'consent_at', 'consent_source',
                   'interested_roles', 'onboarding_stage', 'onboarding_completed_at',
                   'availability_weekdays', 'preferred_services', 'availability_all_day',
                   'availability_frequency_known', 'max_per_month', 'recurring_windows', 'role_frequency_caps'}
IDENTITY_KEYS = {'signup_source', 'consent_pending', 'consent_at', 'consent_source'}


class ProfileHeld(ValueError):
    """A fixed reason code, never a database exception or private configuration."""


def cloud_connection(settings):
    """Connect only to the explicitly configured intended Supabase project."""
    ref = settings.profile_sync_project_ref
    target = settings.profile_sync_database_url
    parsed = urlsplit(target)
    host = parsed.hostname or ''
    correct = host == 'db.' + ref + '.supabase.co' or (
        host.endswith('.pooler.supabase.com') and unquote(parsed.username or '').endswith('.' + ref))
    if not re.fullmatch('[a-z]{20}', ref) or not parsed.scheme.startswith('postgresql') or not correct:
        raise ProfileHeld('cloud_target_mismatch')
    url = target.replace('postgresql://', 'postgresql+psycopg://', 1)
    engine = create_engine(url, execution_options={'schema_translate_map': {None: 'texty'}},
                           connect_args={'connect_timeout': 8, 'sslmode': 'require'})
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def approved_phones(settings):
    phones = {p.strip() for p in settings.profile_sync_phones.split(',') if p.strip()}
    if any(not re.fullmatch(r'\+[1-9][0-9]{7,14}', p) for p in phones):
        raise ProfileHeld('invalid_recipient_scope')
    return phones


def window_snapshot(session, windows):
    """Use the shared schema; serialize catalog references by stable names."""
    from app.core.recurring_availability import normalize_recurring_windows
    roles = session.scalars(select(m.Role)).all()
    types = session.scalars(select(m.EventType)).all()
    normalized = normalize_recurring_windows(windows, roles, types)
    role_names = {role.id: role.name for role in roles}
    type_names = {event.id: event.name for event in types}
    result = []
    for window in normalized:
        context = window['event_context']
        if context is not None:
            context = {'label': context['label'],
                       'event_type_names': [type_names[identifier] for identifier in context['event_type_ids']]}
        result.append({**{key: value for key, value in window.items() if key not in {'role_ids', 'event_context'}},
                       'role_names': [role_names[identifier] for identifier in window['role_ids']],
                       'event_context': context})
    return result


def role_cap_snapshot(session, caps):
    from app.core.recurring_availability import normalize_role_frequency_caps
    normalized = normalize_role_frequency_caps(caps, session.scalars(select(m.Role)).all())
    return [{'role_name': cap['role_name'], 'max_per_month': cap['max_per_month']} for cap in normalized]


def snapshot(session, phone):
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    if volunteer is None:
        return None
    prefs = volunteer.preferences or {}
    if prefs.get('serves_with_volunteer_id') is not None:
        raise ProfileHeld('unresolved_volunteer_reference')
    result = {'name': volunteer.name, 'phone': phone, 'sms_opt_in': volunteer.sms_opt_in,
              'status': volunteer.status,
              'preferences': {k: prefs[k] for k in PREFERENCE_KEYS if k in prefs},
              'availability': []}
    if not re.fullmatch(r'\+[1-9][0-9]{7,14}', phone) or not volunteer.name.strip() or len(volunteer.name) > 120:
        raise ProfileHeld('invalid_identity')
    if type(volunteer.sms_opt_in) is not bool or volunteer.status not in {'active', 'inactive'}:
        raise ProfileHeld('invalid_profile')
    roles = result['preferences'].get('interested_roles', [])
    if not isinstance(roles, list) or any(not isinstance(name, str) or not name for name in roles):
        raise ProfileHeld('invalid_role_names')
    for name in roles:
        if len(session.scalars(select(m.Role.id).where(m.Role.name == name)).all()) != 1:
            raise ProfileHeld('ambiguous_local_role')
    if 'recurring_windows' in result['preferences']:
        result['preferences']['recurring_windows'] = window_snapshot(session, result['preferences']['recurring_windows'])
    if 'role_frequency_caps' in result['preferences']:
        result['preferences']['role_frequency_caps'] = role_cap_snapshot(session, result['preferences']['role_frequency_caps'])
    draft = prefs.get('onboarding_availability_draft')
    if draft is not None:
        if not isinstance(draft, dict) or set(draft) - {'availability_known', 'frequency_known', 'max_per_month', 'weekdays', 'all_day', 'preferred_services', 'available_dates', 'unavailable_dates', 'recurring_windows', 'role_frequency_caps'}:
            raise ProfileHeld('invalid_availability_draft')
        for key in ('availability_known', 'frequency_known', 'all_day'):
            if key in draft and type(draft[key]) is not bool:
                raise ProfileHeld('invalid_availability_draft')
        frequency = draft.get('max_per_month')
        if frequency is not None and (type(frequency) is not int or not 1 <= frequency <= 8):
            raise ProfileHeld('invalid_availability_draft')
        if draft.get('frequency_known') and frequency is None and not draft.get('role_frequency_caps'):
            raise ProfileHeld('invalid_availability_draft')
        days = draft.get('weekdays', [])
        if not isinstance(days, list) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ProfileHeld('invalid_availability_draft')
        for key in ('available_dates', 'unavailable_dates'):
            values = draft.get(key, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or date.fromisoformat(value).isoformat() != value for value in values):
                raise ProfileHeld('invalid_availability_draft')
        services = draft.get('preferred_services', [])
        if not isinstance(services, list) or any(value not in {f'sun_{h}' for h in range(24)} for value in services):
            raise ProfileHeld('invalid_availability_draft')
        result['availability_draft'] = dict(draft)
        if 'recurring_windows' in draft:
            result['availability_draft']['recurring_windows'] = window_snapshot(session, draft['recurring_windows'])
        if 'role_frequency_caps' in draft:
            result['availability_draft']['role_frequency_caps'] = role_cap_snapshot(session, draft['role_frequency_caps'])
    days = result['preferences'].get('availability_weekdays', [])
    services = result['preferences'].get('preferred_services', [])
    frequency = result['preferences'].get('max_per_month')
    if (not isinstance(days, list) or any(type(day) is not int or not 0 <= day <= 6 for day in days)
            or not isinstance(services, list) or any(service not in {f'sun_{h}' for h in range(24)} for service in services)
            or (frequency is not None and (type(frequency) is not int or not 1 <= frequency <= 8))):
        raise ProfileHeld('invalid_availability')
    rows = session.scalars(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id)
                           .order_by(m.Availability.month, m.Availability.id)).all()
    seen = set()
    for row in rows:
        if row.month in seen:
            raise ProfileHeld('ambiguous_local_availability')
        seen.add(row.month)
        for values in (row.available_dates or [], row.unavailable_dates or []):
            if not isinstance(values, list) or any(date.fromisoformat(value).strftime('%Y-%m') != row.month for value in values):
                raise ProfileHeld('invalid_availability_dates')
        result['availability'].append({'month': row.month, 'available_dates': row.available_dates or [],
                                       'unavailable_dates': row.unavailable_dates or []})
    return result


def safe_snapshot(session, phone):
    try:
        return snapshot(session, phone)
    except (ProfileHeld, ValueError, TypeError):
        return {'_held': 'profile_validation_requires_review'}


def capture(session, settings, *, phone, guid, route, before, effective_at, catch_up=False):
    if not settings.profile_sync_enabled or phone not in approved_phones(settings) or route not in PROFILE_ROUTES:
        return None
    after = safe_snapshot(session, phone)
    if after is None:
        return None
    held = after.get('_held')
    # A validated correction must not inherit the prior revision's validation
    # hold. Treat its unknown baseline as a full snapshot of approved fields.
    if before and before.get('_held'):
        before = None
    changed = [key for key in after if key != 'phone' and (before is None or before.get(key) != after[key])] if not held else []
    if not changed and not held:
        return None
    source = session.get(m.Policy, 'profile_sync_source')
    if source is None:
        source = m.Policy(key='profile_sync_source', value={'id': str(uuid4())})
        session.add(source)
        session.flush()
    revision = json.dumps(after, sort_keys=True, separators=(',', ':'))
    key = hashlib.sha256((source.value['id'] + '\0' + guid + '\0' + revision).encode()).hexdigest()
    prior = session.get(ProfileOutbox, key)
    if prior:
        return prior
    row = ProfileOutbox(key=key, source_id=source.value['id'], source_guid=guid, phone=phone,
                        payload={'profile': after if not held else None, 'changed': changed, 'catch_up': catch_up, 'route': route,
                                 'preference_keys': [key for key in after.get('preferences', {}) if before is None or
                                      (before.get('preferences', {}) or {}).get(key) != after['preferences'][key]],
                                 'preference_removals': [key for key in (before or {}).get('preferences', {}) if key not in after.get('preferences', {})],
                                 'availability_months': [saved['month'] for saved in after.get('availability', []) if before is None or saved not in before.get('availability', [])]},
                        state='held' if held else 'pending', detail=held or '', created_at=effective_at)
    # Carry unfinished changed sections into the latest revision. Otherwise a
    # newer name-only answer could strand an earlier unsynced role preference.
    if not held:
        unfinished = session.scalars(select(ProfileOutbox).where(ProfileOutbox.phone == phone,
                    ProfileOutbox.source_id == source.value['id'],
                    ProfileOutbox.state.in_(['pending', 'failed', 'held']))).all()
        for previous in unfinished:
            for section in ('changed', 'preference_keys', 'preference_removals', 'availability_months'):
                row.payload[section] = sorted(set(row.payload[section]) | set(previous.payload.get(section, [])))
        # A new supplied value supersedes an older unfinished removal.
        row.payload['preference_removals'] = [key for key in row.payload['preference_removals']
                                             if key not in after['preferences']]
    session.add(row)
    session.flush()
    return row


def catch_up(session, settings, *, phone, guid):
    """Capture existing legitimate signup state using a real receipt, never replay texts."""
    if phone not in approved_phones(settings):
        raise ProfileHeld('recipient_not_approved')
    receipt = session.get(MacInboundReceipt, guid)
    if not receipt or receipt.result.get('intent') not in PROFILE_ROUTES:
        raise ProfileHeld('profile_receipt_required')
    session_id = receipt.result.get('session_id')
    messages = session.scalars(select(m.Message).where(m.Message.phone == phone,
                m.Message.direction == 'in', m.Message.kind == 'mac_test_in',
                m.Message.purpose == 'test:' + str(session_id)).order_by(m.Message.id)).all()
    matching = [msg for msg in messages if any(hashlib.sha256((phone + '\0' + service + '\0' +
                    str(session_id) + '\0' + msg.body).encode()).hexdigest() == receipt.fingerprint
                    for service in ('iMessage', 'SMS'))]
    profile = snapshot(session, phone)
    if not matching or not profile or profile['preferences'].get('signup_source') != 'sms':
        raise ProfileHeld('verified_signup_history_required')
    if profile['sms_opt_in'] and not (profile['preferences'].get('consent_at') and profile['preferences'].get('consent_source')):
        raise ProfileHeld('recorded_consent_required')
    # Names must have been supplied by this sender, not manufactured by a backfill.
    own_history = ' '.join(msg.body for msg in messages)
    if any(not re.search(r'\b' + re.escape(part) + r'\b', own_history, re.I) for part in profile['name'].split()):
        raise ProfileHeld('sender_identity_history_required')
    return capture(session, settings, phone=phone, guid=guid, route=receipt.result['intent'],
                   before=None, effective_at=matching[-1].created_at, catch_up=True)


def _apply(cloud, row, role_map, *, identity_only=False):
    profile = row.payload['profile']
    google_stop = bool(row.payload.get('google_voice_provenance') and row.payload['route'] == 'stop')
    if google_stop:
        identity_only = True  # Withdrawal cannot wait for unrelated availability completion.
    if not identity_only and profile.get('availability_draft') is not None:
        raise ProfileHeld('incomplete_availability_draft')
    changed = set(row.payload['changed'])
    if google_stop:
        changed = {'sms_opt_in'}
    volunteer = cloud.scalar(select(m.Volunteer).where(m.Volunteer.phone == row.phone).with_for_update())
    old = dict(volunteer.preferences or {}) if volunteer else {}
    marker = old.get(MARKER, {})
    if marker.get('key') == row.key and (marker.get('scope', 'full') == 'full' or identity_only):
        return volunteer.id, 'already_applied'
    if marker.get('at') and datetime.fromisoformat(marker['at']) > row.created_at:
        return volunteer.id, 'newer_profile_retained'
    if old.get('consent_at') and datetime.fromisoformat(old['consent_at']) > row.created_at:
        raise ProfileHeld('newer_cloud_consent')
    keys = set(row.payload['preference_keys']) if volunteer else set(PREFERENCE_KEYS)
    if identity_only:
        keys &= IDENTITY_KEYS
        changed.discard('availability')
        if profile['sms_opt_in']:
            changed.discard('status')
    prefs = {key: value for key, value in profile['preferences'].items() if key in PREFERENCE_KEYS and key in keys}
    if google_stop:
        prefs = {}
    for key in ('consent_at', 'onboarding_completed_at'):
        if key in prefs:
            datetime.fromisoformat(prefs[key])
    def mapped_role(name):
        mapped = role_map.get(name, name)
        roles = cloud.scalars(select(m.Role).where(m.Role.name == mapped)).all()
        if len(roles) != 1:
            raise ProfileHeld('unresolved_cloud_role')
        return roles[0]
    if 'interested_roles' in prefs:
        prefs['interested_roles'] = [mapped_role(name).name for name in prefs['interested_roles']]
    if 'role_frequency_caps' in prefs:
        caps, seen = [], set()
        for cap in prefs['role_frequency_caps']:
            role = mapped_role(cap['role_name'])
            if role.id in seen:
                raise ProfileHeld('ambiguous_cloud_role_frequency')
            seen.add(role.id)
            caps.append({'role_id': role.id, 'role_name': role.name, 'max_per_month': cap['max_per_month']})
        prefs['role_frequency_caps'] = caps
    if 'recurring_windows' in prefs:
        windows = []
        for window in prefs['recurring_windows']:
            mapped = [mapped_role(name) for name in window['role_names']]
            if not window['any_role'] and not mapped:
                raise ProfileHeld('unresolved_window_role')
            if window.get('time_mode', 'clock') == 'clock' and not window['all_day'] and window['start_time'] is None:
                raise ProfileHeld('unresolved_window_time')
            context = window['event_context']
            if context is not None:
                ids = []
                for name in context['event_type_names']:
                    events = cloud.scalars(select(m.EventType).where(m.EventType.name == name)).all()
                    if len(events) != 1:
                        raise ProfileHeld('unresolved_cloud_event_context')
                    ids.append(events[0].id)
                if not ids:
                    raise ProfileHeld('unresolved_cloud_event_context')
                context = {'label': context['label'], 'event_type_ids': ids}
            windows.append({**{key: value for key, value in window.items() if key not in {'role_names', 'event_context'}},
                            'role_ids': [role.id for role in mapped], 'event_context': context})
        prefs['recurring_windows'] = windows
    if volunteer:
        suppressed = cloud.get(m.Policy, 'sms_opt_out:' + row.phone)
        # Existing opt-outs require review. Pending profiles created by this
        # publisher may advance only if their baseline is unchanged and unsuppressed.
        own_pending = (marker.get('source_id') == row.source_id and old.get('consent_pending') is True
                       and marker.get('sms_opt_in') is False and volunteer.status == 'inactive')
        if profile['sms_opt_in'] and (suppressed or (not volunteer.sms_opt_in and not own_pending)):
            raise ProfileHeld('cloud_opt_out_requires_review')
        own_identity = (marker.get('source_id') == row.source_id and marker.get('scope') == 'identity'
                        and marker.get('fields', {}) == {field: getattr(volunteer, field) for field in ('name', 'status', 'sms_opt_in')})
        if 'status' in changed and profile['status'] == 'active' and volunteer.status == 'inactive' and not (own_pending or own_identity):
            raise ProfileHeld('cloud_inactive_requires_review')
        if 'sms_opt_in' in changed and volunteer.sms_opt_in and not profile['sms_opt_in'] and row.payload['route'] not in {'stop', 'signup_declined'}:
            raise ProfileHeld('cloud_consent_state_conflict')
        for field in ('name', 'status', 'sms_opt_in'):
            baseline = marker.get('fields', {})
            if field in changed and field in baseline and getattr(volunteer, field) != baseline[field]:
                if field != 'sms_opt_in' or profile['sms_opt_in']:
                    raise ProfileHeld('cloud_profile_changed')
        if 'preferences' in changed:
            baseline = marker.get('preferences', {})
            removals = set(row.payload['preference_removals']) & (IDENTITY_KEYS if identity_only else PREFERENCE_KEYS)
            for key, value in baseline.items():
                if (key in prefs or key in removals) and old.get(key) != value:
                    raise ProfileHeld('cloud_preferences_changed')
            # Without a publisher baseline, an existing cloud value cannot be
            # proven to belong to the local change being removed.
            if any(key in old and key not in baseline for key in removals):
                raise ProfileHeld('cloud_preferences_changed')
        for field in ('name', 'status', 'sms_opt_in'):
            if field in changed:
                setattr(volunteer, field, profile[field])
        if 'preferences' in changed:
            old.update(prefs)
            for key in row.payload['preference_removals']:
                if key in (IDENTITY_KEYS if identity_only else PREFERENCE_KEYS):
                    old.pop(key, None)
    else:
        if row.payload.get('google_voice_provenance') and row.payload['route'] == 'stop':
            raise ProfileHeld('stop_cloud_profile_missing')
        if cloud.get(m.Policy, 'sms_opt_out:' + row.phone) and profile['sms_opt_in']:
            raise ProfileHeld('cloud_opt_out_requires_review')
        volunteer = m.Volunteer(name=profile['name'], phone=row.phone, sms_opt_in=profile['sms_opt_in'],
                    status='inactive' if identity_only else profile['status'], is_coordinator=False, is_pastor=False,
                    preferences={}, created_at=row.created_at)
        cloud.add(volunteer)
        cloud.flush()  # cloud allocates its own ID; local IDs are never copied.
        old.update(prefs)
    if 'availability' in changed:
        for saved in profile['availability']:
            if saved['month'] not in row.payload['availability_months']:
                continue
            matches = cloud.scalars(select(m.Availability).where(m.Availability.volunteer_id == volunteer.id,
                                      m.Availability.month == saved['month']).with_for_update()).all()
            if len(matches) > 1:
                raise ProfileHeld('ambiguous_cloud_availability')
            availability = matches[0] if matches else None
            if availability and availability.parsed_at and availability.parsed_at > row.created_at:
                raise ProfileHeld('newer_cloud_availability')
            if availability is None:
                availability = m.Availability(volunteer_id=volunteer.id, month=saved['month'])
                cloud.add(availability)
            availability.available_dates = saved['available_dates']
            availability.unavailable_dates = saved['unavailable_dates']
            availability.parsed_at = row.created_at
            # Do not transfer conversation text/raw replies into the mirror.
    old[MARKER] = {'source_id': row.source_id, 'key': row.key, 'at': row.created_at.isoformat(),
                  'scope': 'identity' if identity_only else 'full',
                  'sms_opt_in': volunteer.sms_opt_in,
                  'fields': {field: getattr(volunteer, field) for field in ('name', 'status', 'sms_opt_in')},
                  'preferences': {key: old[key] for key in PREFERENCE_KEYS if key in old}}
    volunteer.preferences = old
    cloud.flush()
    return volunteer.id, 'applied'


def publish_pending(local, cloud_factory, settings, *, limit=1, identity_only=False, retry_held=False,
                    identity_when_incomplete=False):
    """Explicit publisher hook. Never runs Gloo, Messages or unrelated jobs."""
    if not settings.profile_sync_enabled:
        raise ProfileHeld('profile_sync_disabled')
    if not 1 <= limit <= 20:
        raise ProfileHeld('invalid_limit')
    if settings.google_voice_profile_sync_enabled:
        local.flush()
        local.expire_all()  # A long-lived publisher must observe committed STOP/evidence changes.
    phones = approved_phones(settings)
    role_map = json.loads(settings.profile_sync_role_map or '{}')
    if not isinstance(role_map, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in role_map.items()):
        raise ProfileHeld('invalid_role_map')
    states = ['pending', 'failed'] + (['held'] if retry_held else [])
    query = select(ProfileOutbox).where(ProfileOutbox.phone.in_(phones),
                ProfileOutbox.state.in_(states))
    if identity_only or identity_when_incomplete:
        query = query.where(ProfileOutbox.detail != 'identity_synced_preferences_pending')
    priority = [case((ProfileOutbox.payload['route'].as_string() == 'stop', 0), else_=1)] if settings.google_voice_profile_sync_enabled else []
    rows = local.scalars(query.order_by(*priority, ProfileOutbox.created_at, ProfileOutbox.key).limit(limit)).all()
    results = []
    for row in rows:
        row.attempts += 1
        try:
            profile = row.payload.get('profile') or {}
            publish_identity = identity_only or (identity_when_incomplete and row.payload['route'] != 'stop' and
                (profile.get('availability_draft') is not None or
                 profile.get('preferences', {}).get('onboarding_stage') != 'complete'))
            current = local.scalar(select(m.Volunteer).where(m.Volunteer.phone == row.phone))
            if current is None:
                raise ProfileHeld('source_profile_missing')
            if not current.sms_opt_in and (row.payload.get('profile') or {}).get('sms_opt_in'):
                raise ProfileHeld('newer_local_opt_out')
            if row.payload.get('profile') is not None and safe_snapshot(local, row.phone) != row.payload['profile']:
                raise ProfileHeld('newer_local_profile')
            with cloud_factory() as cloud:
                with cloud.begin():
                    if row.payload.get('profile') is None:
                        raise ProfileHeld('profile_validation_requires_review')
                    if row.payload.get('google_voice_provenance') or settings.google_voice_profile_sync_enabled:
                        from app.integrations.google_voice_profile_sync import validate_publication
                        validate_publication(local, settings, row)
                    row.cloud_id, row.detail = _apply(cloud, row, role_map, identity_only=publish_identity)
            if publish_identity:
                row.state, row.detail = 'pending', 'identity_synced_preferences_pending'
            else:
                row.state = 'synced'
                row.synced_at = datetime.now(timezone.utc)
        except ProfileHeld as error:
            row.state, row.detail = 'held', str(error)
        except Exception:
            # Driver exceptions may contain DSNs or user data; never persist them.
            row.state, row.detail = 'failed', 'cloud_unavailable_or_write_failed'
        local.commit()
        results.append({'key': row.key, 'state': row.state, 'detail': row.detail, 'attempts': row.attempts})
    return results

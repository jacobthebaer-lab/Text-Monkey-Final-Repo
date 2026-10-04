"""One audited historical correction, signed for held previews only.

The private operator tool is the only provisioner. A provider signature is not
claimed: this authority records an independently audited local execution.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re

from sqlalchemy import select

from app.db import models as m
from app.integrations.planning_center import PlanningCenterError
from app.integrations.planning_center_availability import FrozenSnapshot, _hash, _json
from app.integrations.profile_models import ProfileOutbox

ACTION = 'accept_audited_local_profile_correction'
METHOD = 'coordinator_independent_audit_of_trusted_prior_local_execution'
REASON = 'Exact historical correction accepted for held preview; no provider signature or execution authority.'
DOMAIN = b'text-monkey:audited-local-profile-correction:v1\0'
AUDIT_HASHES = {'saved_context_hash', 'producer_script_sha256', 'audit_sha256',
                'gloo_output_hash', 'publication_proof_sha256'}
NATIVE_FIELDS = {'database_identity_hash', 'row_id', 'guid', 'service', 'body_hash',
                 'sender_hash', 'receiving_line_hash'}


def fail(reason):
    raise PlanningCenterError('correction_lineage_' + reason)


def database_identity(path):
    try:
        saved = Path(path).expanduser().resolve(strict=True)
        info = saved.stat()
        if not saved.is_file():
            raise ValueError()
        return _hash({'path': str(saved), 'device': info.st_dev, 'inode': info.st_ino})
    except (OSError, ValueError, TypeError):
        fail('database_identity_unavailable')


def target_binding(session, captured, volunteer, row, receipt, message, profile):
    engine = session.get_bind()
    if engine.dialect.name != 'sqlite' or engine.url.database in (None, '', ':memory:'):
        fail('file_sqlite_required')
    return {'database_identity_hash': database_identity(engine.url.database),
        'source_id': row.source_id, 'volunteer_id': volunteer.id,
        'organization_id': captured.value['organization_id'], 'person_id': captured.value['person_id'],
        'message_id': message.id, 'guid': row.source_guid, 'fingerprint': receipt.fingerprint,
        'receipt_result_hash': _hash(receipt.result), 'original_intent': receipt.result['intent'],
        'corrected_profile_key': row.key, 'profile_hash': _hash(profile),
        'source_snapshot_hash': captured.digest, 'correction_route': row.payload['route'],
        'session_id': receipt.result['session_id']}


def key_for(binding):
    return 'profile_fix:' + _hash(binding)


def signature(document, key):
    if not isinstance(key, bytes) or len(key) < 32:
        fail('private_key_required')
    return hmac.new(key, DOMAIN + document.encode(), hashlib.sha256).hexdigest()


def private_key(settings):
    # Import lazily: the held-preview module itself imports the source reader.
    from app.core.planning_center_held_preview import private_file
    key = private_file(settings.pco_correction_lineage_key_path, 4096)
    signature('', key)
    return key


def _time(value):
    if not isinstance(value, str):
        fail('time_invalid')
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        fail('time_invalid')
    return value.astimezone(timezone.utc)


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def validate_manifest(session, manifest, target, now):
    """Fixed fields only; never trusts a caller-supplied actor or proof flag."""
    if not isinstance(manifest, dict) or set(manifest) != {'schema', 'binding', 'audit'} or type(manifest['schema']) is not int or manifest['schema'] != 1:
        fail('manifest_schema_invalid')
    binding, audit = manifest['binding'], manifest['audit']
    if (not isinstance(binding, dict) or set(binding) != set(target) | {'previous_profile_key', 'previous_profile_hash', 'previous_profile_state', 'native'} or
            any(binding.get(k) != v or type(binding.get(k)) is not type(v) for k, v in target.items())):
        fail('current_binding_changed')
    if (not isinstance(audit, dict) or set(audit) != AUDIT_HASHES | {'producer_commit', 'completed_at'} or
            not all(_sha(audit[k]) for k in AUDIT_HASHES) or
            not isinstance(audit['producer_commit'], str) or not re.fullmatch('[a-f0-9]{40}', audit['producer_commit']) or
            _time(audit['completed_at']) > now):
        fail('audit_manifest_invalid')
    native = binding['native']
    if (not isinstance(native, dict) or set(native) != NATIVE_FIELDS or
            type(native['row_id']) is not int or native['row_id'] < 1 or
            native['guid'] != target['guid'] or native['service'] not in {'iMessage', 'SMS'} or
            not all(_sha(native[k]) for k in NATIVE_FIELDS - {'row_id', 'guid', 'service'})):
        fail('native_manifest_invalid')
    message = session.get(m.Message, target['message_id'])
    volunteer = session.get(m.Volunteer, target['volunteer_id'])
    if native['body_hash'] != _hash(message.body) or native['sender_hash'] != _hash(volunteer.phone):
        fail('native_source_hash_changed')
    old = session.scalar(select(ProfileOutbox).where(ProfileOutbox.key == binding['previous_profile_key'])
        .with_for_update().execution_options(populate_existing=True))
    current = session.get(ProfileOutbox, target['corrected_profile_key'])
    if _time(audit['completed_at']) < current.created_at:
        fail('audit_precedes_correction')
    if (old is None or old.key == current.key or old.source_id != current.source_id or old.phone != current.phone or
            old.source_guid != current.source_guid or old.created_at >= current.created_at or
            old.state != binding['previous_profile_state'] or old.state not in {'pending', 'failed', 'synced', 'held'} or
            old.payload.get('route') != target['original_intent'] or
            not _sha(binding['previous_profile_hash']) or _hash(old.payload.get('profile')) != binding['previous_profile_hash'] or
            hashlib.sha256((old.source_id + '\0' + old.source_guid + '\0' + _json(old.payload['profile'])).encode()).hexdigest() != old.key):
        fail('previous_revision_not_verified')
    return binding


def provision(session, settings, config, *, manifest, accepted_manifest_sha256, user, clock,
              native_observer, apply=False):
    """Trusted private CLI only. Caller commits the Policy-only write.

    accepted_manifest_sha256 is the explicitly accepted exact file digest;
    authentication comes from a fresh Supabase GET, not fields in that file.
    native_observer independently reads exactly the selected Messages row.
    """
    from app.core.planning_center_committed_source import CommittedAvailabilityReader
    from app.core.planning_center_frequency_reviews import _actor
    from app.integrations.planning_center_frequency_executor import _aware
    actor = _actor(user, settings)
    if not _sha(accepted_manifest_sha256):
        fail('accepted_manifest_digest_required')
    # Files must use the canonical encoding, making the exact file digest
    # independently verifiable here as well as at the CLI boundary.
    if hashlib.sha256(_json(manifest).encode()).hexdigest() != accepted_manifest_sha256:
        fail('accepted_manifest_digest_changed')
    if not isinstance(manifest, dict) or set(manifest) != {'schema', 'binding', 'audit'} or not isinstance(manifest['binding'], dict):
        fail('manifest_schema_invalid')
    now = _aware(clock()).astimezone(timezone.utc)
    if _time(user['email_confirmed_at']) > now:
        fail('provisioner_confirmation_invalid')
    b = manifest['binding']
    if not {'volunteer_id', 'corrected_profile_key', 'source_id'} <= set(b):
        fail('manifest_schema_invalid')
    reader = CommittedAvailabilityReader(settings, config, volunteer_id=b['volunteer_id'],
        profile_key=b['corrected_profile_key'], source_id=b['source_id'], clock=clock)
    captured, target = reader._checked_read(session, inspecting=True)
    binding = validate_manifest(session, manifest, target, now)
    if native_observer(session, target) != binding['native']:
        fail('native_source_changed')
    key = key_for(binding)
    if session.get(m.Policy, key) is not None:
        fail('record_already_exists')
    document = _json({'schema': 1, 'action': ACTION, 'evidence_class': 'audited_local_producer',
        'provider_signature_verified': False, 'execution_allowed': False, 'binding': binding,
        'audit': manifest['audit'], 'accepted_manifest_sha256': accepted_manifest_sha256,
        'provisioner': actor, 'provisioner_confirmed_at': user['email_confirmed_at'],
        'provisioned_at': now.isoformat(), 'acceptance_method': METHOD,
        'acceptance_basis': 'operator_exact_manifest_digest_acceptance', 'reason': REASON})
    result = {'policy_key': key, 'document_hash': _hash(json.loads(document)),
              'accepted_manifest_sha256': accepted_manifest_sha256, 'applied': False}
    if apply:
        session.add(m.Policy(key=key, value={'state': 'active', 'corrected_profile_key': target['corrected_profile_key'],
            'document': document, 'signature': signature(document, private_key(settings))}))
        session.flush()
        result['applied'] = True
    return result


def verified_source(session, settings, captured, target, now):
    from app.core.planning_center_frequency_reviews import _actor
    from app.integrations.planning_center_frequency_executor import _aware
    if not settings.pco_correction_lineage_enabled:
        fail('disabled')
    rows = session.scalars(select(m.Policy).where(m.Policy.key.like('profile_fix:%'))
        .with_for_update().execution_options(populate_existing=True)).all()
    rows = [r for r in rows if r.value.get('corrected_profile_key') == target['corrected_profile_key']]
    if len(rows) != 1:
        fail('signed_record_required')
    row = rows[0]
    try:
        value = row.value
        if set(value) != {'state', 'corrected_profile_key', 'document', 'signature'} or value['state'] != 'active':
            fail('inactive_record')
        document = value['document']
        if not _sha(value['signature']) or not hmac.compare_digest(value['signature'], signature(document, private_key(settings))):
            fail('signature_invalid')
        data = json.loads(document)
        if (_json(data) != document or set(data) != {'schema', 'action', 'evidence_class', 'provider_signature_verified',
                'execution_allowed', 'binding', 'audit', 'accepted_manifest_sha256', 'provisioner',
                'provisioner_confirmed_at', 'provisioned_at', 'acceptance_method', 'acceptance_basis', 'reason'} or
                type(data['schema']) is not int or data['schema'] != 1 or data['action'] != ACTION or
                data['evidence_class'] != 'audited_local_producer' or data['provider_signature_verified'] is not False or
                data['execution_allowed'] is not False or data['acceptance_method'] != METHOD or
                data['acceptance_basis'] != 'operator_exact_manifest_digest_acceptance' or data['reason'] != REASON):
            fail('document_invalid')
        provisioner = data['provisioner']
        if set(provisioner) != {'id', 'email'} or _actor({**provisioner, 'email_confirmed_at': data['provisioner_confirmed_at']}, settings) != provisioner:
            fail('provisioner_not_allowed')
        now = _aware(now).astimezone(timezone.utc)
        provisioned_at = _time(data['provisioned_at'])
        if not _time(data['audit']['completed_at']) <= provisioned_at <= now or _time(data['provisioner_confirmed_at']) > provisioned_at:
            fail('provision_time_invalid')
        manifest = {'schema': 1, 'binding': data['binding'], 'audit': data['audit']}
        binding = validate_manifest(session, manifest, target, now)
        if (row.key != key_for(binding) or value['corrected_profile_key'] != target['corrected_profile_key'] or
                data['accepted_manifest_sha256'] != hashlib.sha256(_json(manifest).encode()).hexdigest()):
            fail('binding_or_manifest_changed')
        return FrozenSnapshot.capture({**captured.value, 'correction_lineage': {
            'policy_key': row.key, 'document_hash': _hash(data), 'evidence_class': data['evidence_class'],
            'provider_signature_verified': False, 'execution_allowed': False}})
    except (ValueError, TypeError, KeyError, AttributeError):
        fail('signed_record_invalid')

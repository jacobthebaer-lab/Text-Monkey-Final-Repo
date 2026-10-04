-- PROPOSED, UNAPPLIED. SQLite only, selected private Mac store only.
-- Requires independent review, fresh read-only preflight, backup and explicit
-- target/application authorization. Run with stop-on-error; rollback on failure.
-- No IF NOT EXISTS: existing/partial feature schema must never be adopted silently.
BEGIN IMMEDIATE;

-- Schema-only source checks. WHERE 0 returns no private records.
SELECT id, name, phone, sms_opt_in, status, is_coordinator, is_pastor, preferences, created_at FROM volunteers WHERE 0;
SELECT id, volunteer_id, month, available_dates, unavailable_dates, raw_reply, parsed_at FROM availability WHERE 0;
SELECT id, name, ministry, required_qualifications, criticality, fill_policy FROM roles WHERE 0;
SELECT id, name, title_patterns FROM event_types WHERE 0;
SELECT id, direction, volunteer_id, phone, body, kind, purpose, provider_sid, status, created_at FROM messages WHERE 0;
SELECT key, value FROM policies WHERE 0;
SELECT guid, fingerprint, result FROM mac_inbound_receipts WHERE 0;
SELECT key, source_id, source_guid, phone, payload, state, detail, attempts, created_at, synced_at, cloud_id FROM profile_sync_outbox WHERE 0;
SELECT id, organization_id, volunteer_id, person_id, created_at FROM pco_volunteer_people WHERE 0;

CREATE TABLE pco_availability_previews (
  key VARCHAR(64) NOT NULL PRIMARY KEY, organization_id VARCHAR(40) NOT NULL,
  person_id VARCHAR(40) NOT NULL, source_hash VARCHAR(64) NOT NULL,
  remote_hash VARCHAR(64) NOT NULL, document TEXT NOT NULL, created_at DATETIME NOT NULL
);
CREATE TABLE pco_availability_intents (
  key VARCHAR(64) NOT NULL PRIMARY KEY, preview_key VARCHAR(64) NOT NULL,
  organization_id VARCHAR(40) NOT NULL, person_id VARCHAR(40) NOT NULL,
  resource_key VARCHAR(180) NOT NULL, source_hash VARCHAR(64) NOT NULL,
  state VARCHAR(30) NOT NULL, document TEXT NOT NULL, created_at DATETIME NOT NULL
);
CREATE TABLE pco_frequency_claims (
  key VARCHAR(100) NOT NULL PRIMARY KEY, intent_key VARCHAR(64) NOT NULL
);
CREATE TABLE pco_frequency_attempts (
  intent_key VARCHAR(64) NOT NULL PRIMARY KEY, document TEXT NOT NULL,
  document_hash VARCHAR(64) NOT NULL, claimed_at DATETIME NOT NULL,
  verified_at DATETIME, readback TEXT, reason VARCHAR(100) NOT NULL
);
CREATE TABLE pco_frequency_ownership (
  key VARCHAR(180) NOT NULL PRIMARY KEY, intent_key VARCHAR(64) NOT NULL,
  document TEXT NOT NULL, verified_at DATETIME NOT NULL
);
CREATE TABLE pco_frequency_review_receipts (
  id VARCHAR(36) NOT NULL PRIMARY KEY, intent_key VARCHAR(64) NOT NULL,
  organization_id VARCHAR(40) NOT NULL, person_id VARCHAR(40) NOT NULL,
  actor_id VARCHAR(36) NOT NULL, actor_email VARCHAR(120) NOT NULL,
  state VARCHAR(30) NOT NULL, document TEXT NOT NULL, signature VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL
);

CREATE INDEX ix_pco_availability_previews_organization_id ON pco_availability_previews (organization_id);
CREATE INDEX ix_pco_availability_previews_person_id ON pco_availability_previews (person_id);
CREATE INDEX ix_pco_availability_intents_organization_id ON pco_availability_intents (organization_id);
CREATE INDEX ix_pco_availability_intents_person_id ON pco_availability_intents (person_id);
CREATE INDEX ix_pco_availability_intents_preview_key ON pco_availability_intents (preview_key);
CREATE INDEX ix_pco_availability_intents_resource_key ON pco_availability_intents (resource_key);
CREATE INDEX ix_pco_frequency_review_receipts_intent_key ON pco_frequency_review_receipts (intent_key);
CREATE INDEX ix_pco_frequency_review_receipts_organization_id ON pco_frequency_review_receipts (organization_id);
COMMIT;

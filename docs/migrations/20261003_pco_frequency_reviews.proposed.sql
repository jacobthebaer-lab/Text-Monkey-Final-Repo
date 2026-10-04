-- PROPOSED, UNAPPLIED. Independent owner review and explicit target approval required.
-- Not in Supabase's automatic migration directory. Never run at application startup.
-- Apply atomically only to the selected same-store Mac/profile provenance backend.
begin;

do $$
declare table_name text;
begin
  if not exists (select 1 from pg_roles where rolname = 'texty_backend') then
    raise exception 'Required private backend role is missing';
  end if;
  foreach table_name in array array[
    'volunteers', 'availability', 'roles', 'event_types', 'policies', 'messages',
    'mac_inbound_receipts', 'profile_sync_outbox', 'pco_volunteer_people'
  ] loop
    if to_regclass(format('texty.%I', table_name)) is null then
      raise exception 'Required committed source table is missing: %', table_name;
    end if;
  end loop;
  foreach table_name in array array[
    'pco_availability_previews', 'pco_availability_intents', 'pco_frequency_claims',
    'pco_frequency_attempts', 'pco_frequency_ownership', 'pco_frequency_review_receipts'
  ] loop
    if to_regclass(format('texty.%I', table_name)) is not null then
      raise exception 'Existing table needs schema comparison before migration: %', table_name;
    end if;
  end loop;
end $$;

create table texty.pco_availability_previews (
  key varchar(64) primary key, organization_id varchar(40) not null,
  person_id varchar(40) not null, source_hash varchar(64) not null,
  remote_hash varchar(64) not null, document text not null, created_at timestamp without time zone not null
);
create table texty.pco_availability_intents (
  key varchar(64) primary key, preview_key varchar(64) not null,
  organization_id varchar(40) not null, person_id varchar(40) not null,
  resource_key varchar(180) not null, source_hash varchar(64) not null,
  state varchar(30) not null, document text not null, created_at timestamp without time zone not null
);
create table texty.pco_frequency_claims (
  key varchar(100) primary key, intent_key varchar(64) not null
);
create table texty.pco_frequency_attempts (
  intent_key varchar(64) primary key, document text not null, document_hash varchar(64) not null,
  claimed_at timestamp without time zone not null, verified_at timestamp without time zone,
  readback text, reason varchar(100) not null
);
create table texty.pco_frequency_ownership (
  key varchar(180) primary key, intent_key varchar(64) not null,
  document text not null, verified_at timestamp without time zone not null
);
create table texty.pco_frequency_review_receipts (
  id varchar(36) primary key, intent_key varchar(64) not null,
  organization_id varchar(40) not null, person_id varchar(40) not null,
  actor_id varchar(36) not null, actor_email varchar(120) not null,
  state varchar(30) not null, document text not null, signature varchar(64) not null,
  created_at timestamp without time zone not null, expires_at timestamp without time zone not null
);

create index ix_pco_availability_previews_organization_id on texty.pco_availability_previews (organization_id);
create index ix_pco_availability_previews_person_id on texty.pco_availability_previews (person_id);
create index ix_pco_availability_intents_preview_key on texty.pco_availability_intents (preview_key);
create index ix_pco_availability_intents_organization_id on texty.pco_availability_intents (organization_id);
create index ix_pco_availability_intents_person_id on texty.pco_availability_intents (person_id);
create index ix_pco_availability_intents_resource_key on texty.pco_availability_intents (resource_key);
create index ix_pco_frequency_review_receipts_intent_key on texty.pco_frequency_review_receipts (intent_key);
create index ix_pco_frequency_review_receipts_organization_id on texty.pco_frequency_review_receipts (organization_id);

-- Match existing private-store access; browser roles never access signed receipts,
-- identity mappings or native snapshots directly. All reviews use bearer-auth API.
do $$
declare table_name text;
begin
  foreach table_name in array array[
    'pco_availability_previews', 'pco_availability_intents', 'pco_frequency_claims',
    'pco_frequency_attempts', 'pco_frequency_ownership', 'pco_frequency_review_receipts'
  ] loop
    execute format('alter table texty.%I enable row level security', table_name);
    execute format('revoke all on texty.%I from public, anon, authenticated', table_name);
    execute format('grant select, insert, update, delete on texty.%I to texty_backend', table_name);
    execute format('create policy backend_access on texty.%I to texty_backend using (true) with check (true)', table_name);
  end loop;
  grant usage on schema texty to texty_backend;
end $$;

commit;

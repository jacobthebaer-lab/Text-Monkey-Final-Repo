-- Reviewed private bridge schema; apply only to the selected texty store.
-- Never run this migration automatically or against an unapproved database.
create table if not exists texty.pco_event_links (
  key varchar(160) primary key, organization_id varchar(40) not null,
  service_type_id varchar(40) not null, plan_id varchar(40) not null,
  event_id bigint not null unique
);
create table if not exists texty.pco_shift_links (
  key varchar(200) primary key, event_key varchar(160) not null, shift_id bigint not null unique
);
create table if not exists texty.pco_deliveries (
  key varchar(160) primary key, event_name varchar(160) not null,
  received_at timestamp not null, result jsonb not null
);
create table if not exists texty.pco_volunteer_people (
  id bigserial primary key, organization_id varchar(40) not null,
  volunteer_id bigint not null, person_id varchar(40) not null, created_at timestamp not null,
  unique (organization_id, volunteer_id), unique (organization_id, person_id)
);
create table if not exists texty.pco_staffing_links (
  assignment_id bigint primary key, organization_id varchar(40) not null,
  service_type_id varchar(40) not null, plan_id varchar(40) not null,
  team_id varchar(40) not null, person_id varchar(40) not null,
  plan_person_id varchar(40) not null, remote_status varchar(30) not null,
  verified_at timestamp not null, remote_snapshot jsonb not null default '{}'::jsonb,
  unique (organization_id, plan_person_id)
);
create table if not exists texty.pco_staffing_intents (
  id bigserial primary key, idempotency_key varchar(180) not null unique,
  organization_id varchar(40) not null, service_type_id varchar(40) not null,
  plan_id varchar(40) not null, team_id varchar(40) not null,
  assignment_id bigint, person_id varchar(40) not null, action varchar(20) not null,
  state varchar(20) not null default 'pending', plan_person_id varchar(40), reason text,
  attempts integer not null default 0, created_at timestamp not null, updated_at timestamp not null,
  expected jsonb not null default '{}'::jsonb, retry_at timestamp, depends_on bigint
);
create table if not exists texty.pco_position_scopes (
  key varchar(180) primary key, organization_id varchar(40) not null,
  service_type_id varchar(40) not null, plan_id varchar(40) not null,
  event_id bigint not null, role_id bigint not null, team_id varchar(40) not null,
  position_id varchar(40) not null, position_name varchar(100) not null,
  plan_time_id varchar(40) not null, required_count integer, verified_at timestamp not null,
  unique (organization_id, event_id, role_id)
);
create table if not exists texty.pco_staffing_leases (
  key varchar(180) primary key, owner varchar(40) not null default '', expires_at timestamp not null
);
create table if not exists texty.pco_staffing_polls (
  key varchar(180) primary key, next_at timestamp not null, reason text
);
create index if not exists pco_people_org_idx on texty.pco_volunteer_people (organization_id);
create index if not exists pco_staffing_link_plan_idx on texty.pco_staffing_links (organization_id, plan_id);
create index if not exists pco_staffing_intent_plan_idx on texty.pco_staffing_intents (organization_id, plan_id);
create index if not exists pco_event_org_idx on texty.pco_event_links (organization_id);
create index if not exists pco_shift_event_idx on texty.pco_shift_links (event_key);

-- Match the private store's backend-only access model. Neither browser role
-- may read or mutate PCO identities, receipts or scheduler coordination state.
do $$
declare table_name text; sequence_name text;
begin
  foreach table_name in array array[
    'pco_event_links', 'pco_shift_links', 'pco_deliveries',
    'pco_volunteer_people', 'pco_staffing_links', 'pco_staffing_intents',
    'pco_position_scopes', 'pco_staffing_leases', 'pco_staffing_polls'
  ] loop
    execute format('alter table texty.%I enable row level security', table_name);
    execute format('revoke all on texty.%I from public, anon, authenticated', table_name);
    if exists (select 1 from pg_roles where rolname = 'texty_backend') then
      execute format('grant select, insert, update, delete on texty.%I to texty_backend', table_name);
      if not exists (select 1 from pg_policies p where p.schemaname = 'texty'
          and p.tablename = table_name and p.policyname = 'backend_access') then
        execute format('create policy backend_access on texty.%I to texty_backend using (true) with check (true)', table_name);
      end if;
    end if;
  end loop;
  foreach sequence_name in array array['pco_volunteer_people_id_seq', 'pco_staffing_intents_id_seq'] loop
    execute format('revoke all on sequence texty.%I from public, anon, authenticated', sequence_name);
    if exists (select 1 from pg_roles where rolname = 'texty_backend') then
      execute format('grant usage, select on sequence texty.%I to texty_backend', sequence_name);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'texty_backend') then
    grant usage on schema texty to texty_backend;
  end if;
end $$;

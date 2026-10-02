-- REVIEW ONLY. This change is not applied by the onboarding task or app startup.
-- New staging tables in the existing PRIVATE texty schema; not a multi-church
-- scheduling migration. Browser roles have no access. Backend queries must
-- scope every request by the owner verified by Supabase /auth/v1/user.
create table texty.admin_workspaces (
  id varchar(36) primary key,
  owner_id varchar(36) not null unique,
  details json not null default '{}',
  completed boolean not null default false,
  revision integer not null default 0,
  updated_at timestamp not null,
  constraint admin_workspace_owner_uuid check (owner_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
);
create table texty.admin_staged_contacts (
  id varchar(36) primary key,
  workspace_id varchar(36) not null references texty.admin_workspaces(id),
  name varchar(160) not null,
  phone varchar(20) not null,
  email varchar(254) not null default '',
  ministry varchar(160) not null default '',
  source varchar(240) not null,
  created_at timestamp not null,
  unique (workspace_id, phone)
);
create index ix_admin_staged_contacts_workspace_id on texty.admin_staged_contacts(workspace_id);
create table texty.admin_import_batches (
  id varchar(36) primary key,
  workspace_id varchar(36) not null references texty.admin_workspaces(id),
  submission_id varchar(36) not null,
  fingerprint varchar(64) not null,
  result json not null,
  created_at timestamp not null,
  unique (workspace_id, submission_id)
);
create index ix_admin_import_batches_workspace_id on texty.admin_import_batches(workspace_id);
alter table texty.admin_workspaces enable row level security;
alter table texty.admin_staged_contacts enable row level security;
alter table texty.admin_import_batches enable row level security;
revoke all on texty.admin_workspaces, texty.admin_staged_contacts, texty.admin_import_batches from public, anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'texty_backend') then
    grant select, insert, update, delete on texty.admin_workspaces, texty.admin_staged_contacts, texty.admin_import_batches to texty_backend;
    create policy backend_access on texty.admin_workspaces to texty_backend using (true) with check (true);
    create policy backend_access on texty.admin_staged_contacts to texty_backend using (true) with check (true);
    create policy backend_access on texty.admin_import_batches to texty_backend using (true) with check (true);
  end if;
end $$;

-- Additive private transport metadata. Apply to the isolated cloud database.
create table if not exists texty.google_voice_inbound_receipts (
    id varchar(256) primary key,
    fingerprint varchar(64) not null,
    result json not null
);
create table if not exists texty.google_voice_delivery_claims (
    message_id integer primary key references texty.messages(id),
    idempotency_key varchar(128) not null unique,
    created_at timestamptz not null
);
alter table texty.google_voice_inbound_receipts enable row level security;
alter table texty.google_voice_delivery_claims enable row level security;
revoke all on texty.google_voice_inbound_receipts from public, anon, authenticated;
revoke all on texty.google_voice_delivery_claims from public, anon, authenticated;

-- Existing installations already ran the general backend-access migration.
-- Grant only this private transport's new tables; do not broaden client access.
do $$
declare table_name text;
begin
    if exists (select 1 from pg_roles where rolname = 'texty_backend') then
        grant usage on schema texty to texty_backend;
        foreach table_name in array array['google_voice_inbound_receipts', 'google_voice_delivery_claims'] loop
            execute format('grant select, insert, update, delete on texty.%I to texty_backend', table_name);
            if not exists (select 1 from pg_policies where schemaname = 'texty'
                           and tablename = table_name and policyname = 'backend_access') then
                execute format('create policy backend_access on texty.%I to texty_backend using (true) with check (true)', table_name);
            end if;
        end loop;
    end if;
end $$;

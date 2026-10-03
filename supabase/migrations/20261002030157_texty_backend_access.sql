-- The server login credential is generated separately and stored locally.
-- Never put a database password in a migration.
revoke execute on function public.rls_auto_enable() from public, anon, authenticated;

do $$
declare t record;
begin
  if exists (select 1 from pg_roles where rolname = 'texty_backend') then
    grant usage, create on schema texty to texty_backend;
    -- Only administer tables owned by the migration role. Additive transport
    -- tables created by the backend itself are secured by their owner.
    for t in select tablename from pg_tables where schemaname='texty' and tableowner=current_user loop
      execute format('grant select, insert, update, delete, references on texty.%I to texty_backend',t.tablename);
      execute format('alter table texty.%I enable row level security', t.tablename);
      if not exists (select 1 from pg_policies where schemaname='texty' and tablename=t.tablename and policyname='backend_access') then
        execute format('create policy backend_access on texty.%I to texty_backend using (true) with check (true)', t.tablename);
      end if;
    end loop;
  end if;
end $$;

-- Explicit synthetic seed IDs must not collide with later generated IDs.
-- Preserve every record and never move an existing sequence backwards.
do $$
declare
    item record;
    seq_name text;
    max_id bigint;
    sequence_floor bigint;
begin
    for item in select table_name from information_schema.columns
                where table_schema = 'texty' and column_name = 'id'
                  and data_type in ('integer', 'bigint')
    loop
        seq_name := pg_get_serial_sequence(format('texty.%I', item.table_name), 'id');
        if seq_name is not null then
            execute format('lock table texty.%I in share row exclusive mode', item.table_name);
            execute format('select coalesce(max(id),0) from texty.%I', item.table_name) into max_id;
            sequence_floor := nextval(seq_name::regclass);
            perform setval(seq_name::regclass, greatest(max_id, sequence_floor), true);
        end if;
    end loop;
end $$;

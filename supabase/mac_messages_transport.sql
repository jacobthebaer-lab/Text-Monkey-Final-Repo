-- Additive private transport metadata; apply in the isolated Texty project.
create table if not exists texty.mac_inbound_receipts (
    guid varchar(128) primary key,
    fingerprint varchar(64) not null,
    result json not null
);
create table if not exists texty.mac_delivery_claims (
    message_id integer primary key references texty.messages(id),
    token varchar(64) not null
);
alter table texty.mac_inbound_receipts enable row level security;
alter table texty.mac_delivery_claims enable row level security;
revoke all on texty.mac_inbound_receipts from public, anon, authenticated;
revoke all on texty.mac_delivery_claims from public, anon, authenticated;

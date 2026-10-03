-- Additive MVP migration. Private store only; no roster/reset/sequence changes.
CREATE TABLE IF NOT EXISTS texty.notifications (
 key varchar(120) PRIMARY KEY,
 volunteer_id integer REFERENCES texty.volunteers(id),
 event_id integer REFERENCES texty.events(id),
 purpose varchar(40) NOT NULL,
 body text NOT NULL DEFAULT '',
 state varchar(20) NOT NULL DEFAULT 'pending',
 due_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL,
 expires_at timestamptz,
 message_id integer REFERENCES texty.messages(id),
 detail json NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_notifications_due_at ON texty.notifications(due_at);
ALTER TABLE texty.notifications ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON texty.notifications FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON texty.notifications TO texty_backend;

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='texty' AND tablename='notifications' AND policyname='backend_access') THEN
  CREATE POLICY backend_access ON texty.notifications FOR ALL TO texty_backend USING (true) WITH CHECK (true);
 END IF;
END $$;

-- Each Shift is one slot. Fail rather than discard data if old duplicate slots exist.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_assignment_per_shift
 ON texty.assignments(shift_id) WHERE status IN ('proposed','approved','confirmed');

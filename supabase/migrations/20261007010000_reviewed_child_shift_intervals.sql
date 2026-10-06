-- Proposed source migration only. Deployment requires separate operator authorization.
-- Child slots share the original Event and retain one-active-person-per-Shift.
ALTER TABLE texty.shifts ADD COLUMN parent_shift_id integer REFERENCES texty.shifts(id);
ALTER TABLE texty.shifts ADD COLUMN interval_starts_at timestamp without time zone;
ALTER TABLE texty.shifts ADD COLUMN interval_ends_at timestamp without time zone;
ALTER TABLE texty.shifts ADD COLUMN coverage_review_id integer REFERENCES texty.approvals(id);
CREATE INDEX ix_shifts_parent_shift_id ON texty.shifts(parent_shift_id);
CREATE UNIQUE INDEX one_interval_per_split ON texty.shifts(parent_shift_id, interval_starts_at, interval_ends_at);
ALTER TABLE texty.shifts ADD CONSTRAINT valid_child_shift_interval CHECK (
  (parent_shift_id IS NULL AND interval_starts_at IS NULL AND interval_ends_at IS NULL AND coverage_review_id IS NULL)
  OR (parent_shift_id IS NOT NULL AND parent_shift_id <> id AND interval_starts_at IS NOT NULL
      AND interval_ends_at IS NOT NULL AND interval_ends_at > interval_starts_at AND coverage_review_id IS NOT NULL)
);

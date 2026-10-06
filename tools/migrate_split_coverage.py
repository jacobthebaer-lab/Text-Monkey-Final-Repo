"""Explicit, offline SQLite migration for reviewed child Shift intervals.

This is never called by app startup. Stop workers and back up the selected store
before an operator applies it. Inspect is read-only; --apply is explicit.
"""
import argparse
from pathlib import Path
import sqlite3

COLUMNS = {'parent_shift_id', 'interval_starts_at', 'interval_ends_at', 'coverage_review_id'}
CHECK = '''(parent_shift_id IS NULL AND interval_starts_at IS NULL AND interval_ends_at IS NULL AND coverage_review_id IS NULL)
 OR (parent_shift_id IS NOT NULL AND parent_shift_id <> id AND interval_starts_at IS NOT NULL
 AND interval_ends_at IS NOT NULL AND interval_ends_at > interval_starts_at AND coverage_review_id IS NOT NULL)'''


def inspect_store(connection):
    existing = {row[1] for row in connection.execute('PRAGMA table_info(shifts)')}
    if not {'id', 'event_id', 'role_id', 'slot_index'} <= existing:
        raise ValueError('The selected store does not have the existing Shift schema')
    present = existing & COLUMNS
    if present and present != COLUMNS:
        raise ValueError('A partially applied migration requires operator recovery before activation')
    indexes = {row[1] for row in connection.execute('PRAGMA index_list(assignments)')}
    if 'one_active_assignment_per_shift' not in indexes:
        raise ValueError('The existing one-active-person constraint must be present')
    if present:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='shifts'").fetchone()[0]
        indexes = {row[1] for row in connection.execute('PRAGMA index_list(shifts)')}
        if 'valid_child_shift_interval' not in sql or 'one_interval_per_split' not in indexes:
            raise ValueError('The child interval constraints are incomplete')
    return 'ready' if present else 'migration_required'


def apply(connection):
    if inspect_store(connection) == 'ready':
        return 'already_applied'
    if connection.in_transaction:
        raise ValueError('Apply requires its own offline transaction')
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('ALTER TABLE shifts ADD COLUMN parent_shift_id INTEGER REFERENCES shifts(id)')
        connection.execute('ALTER TABLE shifts ADD COLUMN interval_starts_at DATETIME')
        connection.execute('ALTER TABLE shifts ADD COLUMN interval_ends_at DATETIME')
        connection.execute('ALTER TABLE shifts ADD COLUMN coverage_review_id INTEGER REFERENCES approvals(id) '
                           'CONSTRAINT valid_child_shift_interval CHECK ('+CHECK+')')
        connection.execute('CREATE INDEX ix_shifts_parent_shift_id ON shifts(parent_shift_id)')
        connection.execute('CREATE UNIQUE INDEX one_interval_per_split ON shifts(parent_shift_id, interval_starts_at, interval_ends_at)')
        inspect_store(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return 'applied'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    with sqlite3.connect(path.as_uri()+('?mode=rw' if args.apply else '?mode=ro'), uri=True) as connection:
        print(apply(connection) if args.apply else inspect_store(connection))

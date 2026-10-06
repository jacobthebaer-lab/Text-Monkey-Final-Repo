"""Migrate only a disposable pre-feature SQLite store; preserve existing records."""
import sqlite3
import pytest
from tools.migrate_split_coverage import apply, inspect_store


def old_store():
    connection = sqlite3.connect(':memory:')
    connection.executescript('''
        CREATE TABLE shifts(id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL,role_id INTEGER NOT NULL,slot_index INTEGER NOT NULL);
        CREATE TABLE approvals(id INTEGER PRIMARY KEY);
        CREATE TABLE assignments(id INTEGER PRIMARY KEY,shift_id INTEGER NOT NULL,status TEXT);
        CREATE UNIQUE INDEX one_active_assignment_per_shift ON assignments(shift_id)
            WHERE status IN ('proposed','approved','confirmed');
        INSERT INTO shifts VALUES(1,9,3,0);
        INSERT INTO assignments VALUES(1,1,'confirmed');
    ''')
    return connection


def test_offline_migration_preserves_whole_slots_and_existing_one_person_guard():
    with old_store() as db:
        assert inspect_store(db) == 'migration_required'
        assert apply(db) == 'applied'
        assert apply(db) == 'already_applied'
        assert db.execute('SELECT * FROM shifts').fetchone() == (1,9,3,0,None,None,None,None)
        assert db.execute('SELECT * FROM assignments').fetchone() == (1,1,'confirmed')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO assignments VALUES(2,1,'approved')")
        db.execute("INSERT INTO shifts VALUES(2,9,3,-1,1,'2026-11-01 08:00','2026-11-01 09:00',1)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO shifts VALUES(3,9,3,-2,1,'2026-11-01 08:00','2026-11-01 09:00',1)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO shifts VALUES(4,9,3,-3,1,'2026-11-01 08:00',NULL,1)")


def test_incomplete_schema_is_held_without_filling_missing_columns():
    with old_store() as db:
        db.execute('ALTER TABLE shifts ADD COLUMN parent_shift_id INTEGER')
        with pytest.raises(ValueError,match='partially applied'):
            apply(db)
        assert len(db.execute('PRAGMA table_info(shifts)').fetchall()) == 5

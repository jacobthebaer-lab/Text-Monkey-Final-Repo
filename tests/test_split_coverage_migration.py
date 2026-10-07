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


@pytest.mark.parametrize('index_sql', [
    'CREATE INDEX one_active_assignment_per_shift ON assignments(shift_id)',
    "CREATE UNIQUE INDEX one_active_assignment_per_shift ON assignments(shift_id) WHERE status='confirmed'",
    "CREATE UNIQUE INDEX one_active_assignment_per_shift ON assignments(id) WHERE status IN ('proposed','approved','confirmed')",
])
def test_migration_holds_same_named_index_without_complete_active_slot_guard(index_sql):
    with old_store() as db:
        db.execute('DROP INDEX one_active_assignment_per_shift')
        db.execute(index_sql)
        before = db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        with pytest.raises(ValueError, match='one-active-person constraint'):
            apply(db)
        assert db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before


def test_ready_store_holds_same_named_nonunique_child_interval_index():
    with old_store() as db:
        apply(db)
        db.execute('DROP INDEX one_interval_per_split')
        db.execute('CREATE INDEX one_interval_per_split ON shifts(parent_shift_id, interval_starts_at, interval_ends_at)')
        with pytest.raises(ValueError, match='child interval constraints'):
            inspect_store(db)


def test_current_orm_schema_has_the_same_offline_constraints():
    from sqlalchemy import create_engine
    from app.db.models import Base
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        assert inspect_store(connection.connection.driver_connection) == 'ready'
    engine.dispose()


def test_active_assignment_predicate_accepts_equivalent_status_order():
    with old_store() as db:
        db.execute('DROP INDEX one_active_assignment_per_shift')
        db.execute("CREATE UNIQUE INDEX one_active_assignment_per_shift ON assignments(shift_id) WHERE status IN ('confirmed', 'proposed', 'approved')")
        assert inspect_store(db) == 'migration_required'


def test_postgres_declares_the_same_active_slot_and_child_interval_guards():
    from pathlib import Path
    from sqlalchemy.schema import CreateIndex, CreateTable
    from sqlalchemy.dialects import postgresql
    from app.db.models import Assignment, Shift
    index = next(i for i in Assignment.__table__.indexes if i.name == 'one_active_assignment_per_shift')
    compiled = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert 'UNIQUE INDEX one_active_assignment_per_shift' in compiled
    assert "(shift_id) WHERE status IN ('proposed','approved','confirmed')" in compiled
    assert 'CONSTRAINT valid_child_shift_interval CHECK' in str(CreateTable(Shift.__table__).compile(dialect=postgresql.dialect()))
    migrations = Path(__file__).resolve().parents[1] / 'supabase/migrations'
    deployed_guard = (migrations / '20261002055227_notification_outbox.sql').read_text()
    assert "where status in ('proposed','approved','confirmed')" in deployed_guard.lower()
    child_migration = (migrations / '20261007010000_reviewed_child_shift_intervals.sql').read_text()
    assert 'CREATE UNIQUE INDEX one_interval_per_split' in child_migration
    assert 'ADD CONSTRAINT valid_child_shift_interval CHECK' in child_migration

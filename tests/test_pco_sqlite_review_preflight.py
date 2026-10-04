"""Execute only the proposed SQLite DDL in isolated synthetic databases."""
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import URL, create_engine

from scripts.pco_sqlite_review_preflight import SOURCE_ROWS, TARGETS, inspect_database, main

ARTIFACT = Path(__file__).resolve().parents[1] / 'docs/migrations/20261003_pco_frequency_reviews.sqlite.proposed.sql'


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'synthetic source ?mode=rw.db'
    engine = create_engine(URL.create('sqlite', database=str(path)))
    # Only the nine explicit source-model tables, never broad metadata.create_all.
    for model in SOURCE_ROWS:
        model.__table__.create(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO policies (key,value) VALUES ('synthetic-sentinel','{}')")
    return path


def apply_synthetic(path, sql=None):
    with sqlite3.connect(path) as db:
        try:
            db.executescript(sql or ARTIFACT.read_text())
        except sqlite3.Error:
            db.rollback()
            raise


def test_fresh_source_preflight_is_read_only_redacted_and_held(store):
    before = store.read_bytes()
    report = inspect_database(store)
    assert report['source_issues'] == {} and report['feature_state'] == 'absent'
    assert report['migration_review_ready'] and report['migration_needed']
    assert report['migration_authorized'] is False and report['execution_enabled'] is False
    assert str(store) not in str(report) and 'synthetic-sentinel' not in str(report)
    assert store.read_bytes() == before


def test_sqlite_artifact_matches_all_six_models_eight_indexes_and_preserves_source(store):
    apply_synthetic(store)
    report = inspect_database(store)
    assert report['source_issues'] == {} and report['feature_issues'] == {}
    assert report['feature_state'] == 'complete' and report['schema_compatible']
    assert not report['migration_needed'] and not report['migration_review_ready']
    with sqlite3.connect(store) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert tables == {m.__tablename__ for m in (*SOURCE_ROWS, *TARGETS)}
        assert db.execute("SELECT value FROM policies WHERE key='synthetic-sentinel'").fetchone() == ('{}',)
        index_count = sum(len([r for r in db.execute('PRAGMA index_list("'+m.__tablename__+'")') if r[3]=='c']) for m in TARGETS)
        assert index_count == 8
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO pco_frequency_claims (key,intent_key) VALUES (NULL,'synthetic')")
        db.execute("INSERT INTO pco_frequency_claims (key,intent_key) VALUES ('10:70','synthetic')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO pco_frequency_claims (key,intent_key) VALUES ('10:70','duplicate')")


def test_missing_source_table_holds_preflight_and_sql_aborts_before_feature_creation(store):
    with sqlite3.connect(store) as db: db.execute('DROP TABLE profile_sync_outbox')
    report = inspect_database(store)
    assert report['source_issues']['profile_sync_outbox'] == ['missing_table']
    assert not report['migration_review_ready']
    with pytest.raises(sqlite3.OperationalError): apply_synthetic(store)
    assert inspect_database(store)['feature_state'] == 'absent'


def test_partial_feature_schema_is_held_and_apply_rollback_preserves_it(store):
    with sqlite3.connect(store) as db:
        db.execute('CREATE TABLE pco_frequency_claims (key VARCHAR(100) NOT NULL PRIMARY KEY, intent_key VARCHAR(64) NOT NULL)')
        db.execute("INSERT INTO pco_frequency_claims VALUES ('10:70','unknown-outcome-sentinel')")
    report = inspect_database(store)
    assert report['feature_state'] == 'partial' and not report['schema_compatible']
    with pytest.raises(sqlite3.OperationalError): apply_synthetic(store)
    report = inspect_database(store)
    assert report['feature_tables_present'] == ['pco_frequency_claims']
    with sqlite3.connect(store) as db:
        assert db.execute('SELECT intent_key FROM pco_frequency_claims').fetchone() == ('unknown-outcome-sentinel',)


@pytest.mark.parametrize('change', ['missing_index', 'unexpected_unique', 'trigger', 'wrong_column_type'])
def test_incompatible_complete_target_schema_cannot_be_adopted(store, change):
    if change == 'wrong_column_type':
        apply_synthetic(store, ARTIFACT.read_text().replace('actor_email VARCHAR(120)', 'actor_email INTEGER'))
    else:
        apply_synthetic(store)
        with sqlite3.connect(store) as db:
            if change == 'missing_index': db.execute('DROP INDEX ix_pco_availability_intents_resource_key')
            elif change == 'unexpected_unique': db.execute('CREATE UNIQUE INDEX unexpected ON pco_availability_intents(person_id)')
            else: db.execute('CREATE TRIGGER unexpected AFTER INSERT ON pco_frequency_claims BEGIN DELETE FROM pco_frequency_claims; END')
    report = inspect_database(store)
    assert report['feature_state'] == 'complete' and report['feature_issues']
    assert not report['schema_compatible'] and not report['migration_review_ready']
    assert report['migration_needed']


def test_target_index_name_collision_holds_a_fresh_migration(store):
    with sqlite3.connect(store) as db:
        db.execute('CREATE INDEX ix_pco_availability_previews_person_id ON messages(phone)')
    report = inspect_database(store)
    assert report['feature_issues'] and report['feature_state'] == 'absent'
    assert not report['migration_review_ready']
    with pytest.raises(sqlite3.OperationalError): apply_synthetic(store)
    assert inspect_database(store)['feature_state'] == 'absent'


def test_source_required_identity_unique_constraints_are_checked(store):
    with sqlite3.connect(store) as db:
        db.execute('DROP TABLE pco_volunteer_people')
        db.execute('CREATE TABLE pco_volunteer_people (id INTEGER NOT NULL PRIMARY KEY, organization_id VARCHAR(40) NOT NULL, volunteer_id INTEGER NOT NULL, person_id VARCHAR(40) NOT NULL, created_at DATETIME NOT NULL)')
    report = inspect_database(store)
    assert 'required_unique_constraints' in report['source_issues']['pco_volunteer_people']
    assert not report['migration_review_ready']


def test_cli_missing_target_is_sanitized_and_cannot_create_a_database(tmp_path, capsys):
    path = tmp_path / 'PRIVATE_PATH_SENTINEL.db'
    assert main(['--database', str(path)]) == 2
    output = capsys.readouterr().out
    assert 'PRIVATE_PATH_SENTINEL' not in output and not path.exists()


def test_cli_has_no_apply_mode(store, capsys):
    assert main(['--database', str(store)]) == 0
    assert '"execution_enabled": false' in capsys.readouterr().out
    with pytest.raises(SystemExit): main(['--database', str(store), '--apply'])
    assert inspect_database(store)['feature_state'] == 'absent'

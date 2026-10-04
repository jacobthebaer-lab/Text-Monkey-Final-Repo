"""Read-only SQLite schema report. No apply mode, row reads or runtime imports."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote

from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects import sqlite

from app.core.planning_center_committed_source import SOURCE_ROWS
from app.integrations.planning_center_availability import PCOAvailabilityPreview, PCOAvailabilityIntent
from app.integrations.planning_center_frequency_executor import PCOFrequencyClaim, PCOFrequencyAttempt, PCOFrequencyOwnership
from app.integrations.planning_center_review_models import PCOFrequencyReviewReceipt

TARGETS = (PCOAvailabilityPreview, PCOAvailabilityIntent, PCOFrequencyClaim,
           PCOFrequencyAttempt, PCOFrequencyOwnership, PCOFrequencyReviewReceipt)
RELEASE_HOLDS = ['explicit_target_migration_review_and_authorization_required',
    'actual_committed_source_and_mapping_not_verified_by_schema', 'private_signing_key_not_verified',
    'protected_review_route_and_guarded_executor_wiring_not_verified',
    'native_notification_silence_unverified', 'native_edit_coordination_unverified',
    'fresh_native_preflight_required']


def _quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _schema(db, table):
    columns = {r[1]: {'type': ' '.join(r[2].upper().split()), 'notnull': bool(r[3]),
                     'default': r[4], 'pk': r[5], 'hidden': r[6]}
               for r in db.execute('PRAGMA table_xinfo(' + _quoted(table) + ')')}
    indexes = {}
    for row in db.execute('PRAGMA index_list(' + _quoted(table) + ')'):
        details = [r for r in db.execute('PRAGMA index_xinfo(' + _quoted(row[1]) + ')') if r[5]]
        indexes[row[1]] = {'unique': bool(row[2]), 'partial': bool(row[4]), 'origin': row[3],
                          'columns': [r[2] for r in details], 'descending': [bool(r[3]) for r in details],
                          'collation': [r[4] for r in details]}
    sql = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()[0]
    return {'columns': columns, 'indexes': indexes,
            'foreign_keys': bool(db.execute('PRAGMA foreign_key_list(' + _quoted(table) + ')').fetchall()),
            'triggers': bool(db.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (table,)).fetchone()),
            'extra_table_constraints': bool(re.search(r'\bCHECK\s*\(|\bWITHOUT\s+ROWID\b|\bSTRICT\s*$', sql, re.I))}


def _issues(table, actual, *, exact):
    issues = []
    columns, indexes = actual['columns'], actual['indexes']
    if exact and set(columns) - set(table.columns.keys()):
        issues.append('unexpected_columns')
    for column in table.columns:
        saved = columns.get(column.name)
        if saved is None:
            issues.append('missing_column:' + column.name)
            continue
        expected_type = ' '.join(str(column.type.compile(dialect=sqlite.dialect())).upper().split())
        if saved['type'] != expected_type:
            issues.append('column_type:' + column.name)
        if saved['notnull'] != (not column.nullable) or saved['hidden']:
            issues.append('column_nullability_or_generation:' + column.name)
        if exact and saved['default'] is not None:
            issues.append('unexpected_column_default:' + column.name)
    primary = [name for name, value in sorted(columns.items(), key=lambda item: item[1]['pk']) if value['pk']]
    if primary != [c.name for c in table.primary_key.columns]:
        issues.append('primary_key')
    unique = {tuple(index['columns']) for index in indexes.values() if index['unique'] and
              not index['partial'] and not any(index['descending']) and all(c == 'BINARY' for c in index['collation'])}
    required = {tuple(c.name for c in constraint.columns) for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)}
    required |= {tuple(c.name for c in index.columns) for index in table.indexes if index.unique}
    if not required <= unique:
        issues.append('required_unique_constraints')
    if exact:
        if actual['foreign_keys'] or actual['triggers'] or actual['extra_table_constraints']:
            issues.append('unexpected_constraints_or_triggers')
        if any(name not in {i.name for i in table.indexes} and saved['origin'] != 'pk'
               for name, saved in indexes.items()):
            issues.append('unexpected_indexes')
        for index in table.indexes:
            expected = {'unique': bool(index.unique), 'partial': False, 'origin': 'c',
                        'columns': [c.name for c in index.columns], 'descending': [False] * len(index.columns),
                        'collation': ['BINARY'] * len(index.columns)}
            if indexes.get(index.name) != expected:
                issues.append('required_index:' + index.name)
    return sorted(issues)


def inspect_database(database):
    """Snapshot schema via mode=ro/query_only; expose no path or row contents.

    Strict declared types intentionally hold equivalent but unreviewed schemas.
    Compatibility is evidence for review, never permission to apply or execute.
    """
    path = Path(database).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('sqlite_target_requires_existing_file')
    stat = path.stat()
    with sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True, timeout=5) as db:
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            index_names = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
            source, targets, snapshot = {}, {}, {}
            for model in SOURCE_ROWS:
                table = model.__table__
                if table.name not in names:
                    source[table.name] = ['missing_table']
                else:
                    saved = _schema(db, table.name); snapshot[table.name] = saved
                    if issues := _issues(table, saved, exact=False): source[table.name] = issues
            present = []
            for model in TARGETS:
                table = model.__table__
                if table.name in names:
                    present.append(table.name)
                    saved = _schema(db, table.name); snapshot[table.name] = saved
                    if issues := _issues(table, saved, exact=True): targets[table.name] = issues
                elif collisions := sorted(index.name for index in table.indexes if index.name in index_names):
                    targets[table.name] = ['index_name_collision:' + name for name in collisions]
            target_state = ('absent' if not present else 'complete' if len(present) == len(TARGETS) else 'partial')
            compatible = not source and not targets and target_state != 'partial'
            return {'schema': 1, 'inspection': 'read_only_schema_only',
                'target_identity_hash': _hash([str(path), stat.st_dev, stat.st_ino]),
                'schema_hash': _hash({'tables': snapshot, 'source_issues': source,
                                     'feature_issues': targets, 'feature_state': target_state}),
                'sqlite_schema_version': db.execute('PRAGMA schema_version').fetchone()[0],
                'source_issues': source, 'feature_issues': targets, 'feature_state': target_state,
                'feature_tables_present': sorted(present), 'schema_compatible': compatible,
                'migration_review_ready': compatible and target_state == 'absent',
                'migration_needed': target_state != 'complete' or bool(targets),
                'migration_authorized': False, 'execution_enabled': False, 'release_holds': RELEASE_HOLDS.copy()}
        finally:
            db.rollback()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, help='Explicit existing SQLite target; always opened read-only.')
    args = parser.parse_args(argv)
    try:
        report = inspect_database(args.database)
    except (OSError, ValueError, sqlite3.Error):
        report = {'inspection': 'held', 'reason': 'sqlite_schema_preflight_unavailable',
                  'migration_authorized': False, 'execution_enabled': False}
        print(json.dumps(report, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report['schema_compatible'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

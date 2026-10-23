"""Owner-authorized, transactional schema migrations with bound preview hashes."""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from app.data import DATA_TABLE, DataService, FUNCTIONS
from app.observability import audit_actor, request_id, trace_operation, event

SQLITE_SCHEMA = {'sqlite_master', 'sqlite_schema', 'sqlite_sequence'}
SCHEMA_FUNCTIONS = {'like', 'glob', 'printf', 'sqlite_rename_table', 'sqlite_rename_column', 'sqlite_rename_test', 'sqlite_drop_column', 'sqlite_rename_quotefix'}


def schema_objects(connection):
    return [dict(row) for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name")]


def schema_digest(schema):
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


class Migrations:
    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def authorize(action, first, second, database, source):
        if source is not None or database not in {None, 'main'}:
            return sqlite3.SQLITE_DENY
        data_first = bool(DATA_TABLE.fullmatch(first or ''))
        data_second = bool(DATA_TABLE.fullmatch(second or ''))
        if action == sqlite3.SQLITE_CREATE_TABLE:
            return sqlite3.SQLITE_OK if data_first or first == 'sqlite_sequence' else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_DROP_TABLE:
            return sqlite3.SQLITE_OK if data_first and first != 'data_records' else sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX}:
            safe_index = bool(DATA_TABLE.fullmatch(first or '')) or (first or '').startswith('sqlite_autoindex_')
            return sqlite3.SQLITE_OK if safe_index and data_second else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_REINDEX:
            return sqlite3.SQLITE_OK if data_first else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_ALTER_TABLE:
            return sqlite3.SQLITE_OK if first == 'main' and data_second and second != 'data_records' else sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            return sqlite3.SQLITE_OK if data_first or first in SQLITE_SCHEMA else sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if second and second.lower() in FUNCTIONS | SCHEMA_FUNCTIONS else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    @trace_operation
    def migrate(self, migration_id, statements, apply=False, preview_hash=None):
        if not isinstance(migration_id, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', migration_id):
            raise ValueError('migration_id must be 1–128 letters, digits, dots, underscores, colons, or hyphens')
        if not 1 <= len(statements) <= 50:
            raise ValueError('A migration must contain 1–50 single SQL statements')
        for statement in statements:
            DataService.statement(statement['sql'], statement.get('params', []))
        payload = json.dumps(statements, sort_keys=True, separators=(',', ':'))
        if len(payload.encode()) > 262144:
            raise ValueError('Migration exceeds 256 KiB')
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.registry.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT digest,response FROM system_migrations WHERE migration_id=?', (migration_id,)).fetchone()
            if previous:
                if previous['digest'] != digest:
                    raise ValueError('migration_id already used for different statements')
                return {**json.loads(previous['response']), 'replayed': True}
            before = schema_objects(connection)
            before_hash = schema_digest(before)
            expected_hash = hashlib.sha256((str(Path(self.registry.path).resolve()) + ':' + migration_id + ':' + before_hash + ':' + digest).encode()).hexdigest()
            if apply and preview_hash != expected_hash:
                raise ValueError('Preview missing, statements changed, or schema changed; preview this migration again before applying')
            deadline = time.monotonic() + 5
            steps = 0
            def progress():
                nonlocal steps
                steps += 1000
                return int(steps > 2000000 or time.monotonic() > deadline)
            changed_rows = 0
            statement_changes = []
            touched_tables = set()
            try:
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 262144)
                connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 16384)
                connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
                connection.set_progress_handler(progress, 1000)
                before_counts = {table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in DataService.tables(connection)}
                for statement in statements:
                    # SQLite implements ALTER using internal schema scans and
                    # quick-check table functions. Enable only those helpers for
                    # a single ALTER statement; public SQL cannot issue PRAGMAs.
                    leading = re.sub(r'\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*', '', statement['sql'], flags=re.S)
                    altering = bool(re.match(r'ALTER\s+TABLE\b', leading, re.I))
                    def authorize(action, first, second, database, source):
                        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and DATA_TABLE.fullmatch(first or ""):
                            touched_tables.add(first)
                        if altering and source is None:
                            if database == 'temp' and first in {'sqlite_temp_master','sqlite_temp_schema'} and action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_UPDATE}:
                                return sqlite3.SQLITE_OK
                            if action == sqlite3.SQLITE_READ and first == 'pragma_quick_check':
                                return sqlite3.SQLITE_OK
                            if action == sqlite3.SQLITE_PRAGMA and first == 'quick_check':
                                return sqlite3.SQLITE_OK
                        return self.authorize(action,first,second,database,source)
                    connection.set_authorizer(authorize)
                    previous_changes = connection.total_changes
                    cursor = connection.execute(statement['sql'], statement.get('params', []))
                    if cursor.description is not None:
                        if not altering or cursor.fetchone() is not None:
                            raise ValueError('Migrations accept DDL and data backfills without RETURNING; use db_query for reads')
                    delta = connection.total_changes - previous_changes
                    changed_rows += delta
                    statement_changes.append(delta)
                    connection.set_authorizer(None)
                after = schema_objects(connection)
                before_system = [row for row in before if not DATA_TABLE.fullmatch(row['tbl_name']) and row['tbl_name'] != 'sqlite_sequence']
                after_system = [row for row in after if not DATA_TABLE.fullmatch(row['tbl_name']) and row['tbl_name'] != 'sqlite_sequence']
                if before_system != after_system:
                    raise ValueError('Migration cannot create or change protected schema')
                legacy_before = [row for row in before if row['type']=='table' and row['name']=='data_records']
                legacy_after = [row for row in after if row['type']=='table' and row['name']=='data_records']
                if legacy_before != legacy_after:
                    raise ValueError('Keep the compatibility data_records schema unchanged; migrate records into new relational tables')
                if any(row['type'] not in {'table','index'} for row in after):
                    raise ValueError('Views, triggers, and virtual tables are not supported')
                tables = DataService.tables(connection)
                for table in tables:
                    for foreign_key in connection.execute('SELECT * FROM pragma_foreign_key_list(?)', (table,)):
                        if foreign_key['table'] not in tables:
                            raise ValueError('Foreign keys must reference existing data_* tables')
                if connection.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Migration violates foreign keys')
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Migration failed integrity checks')
                before_map = {(row['type'],row['name']): row for row in before}
                after_map = {(row['type'],row['name']): row for row in after}
                changes = []
                for key in sorted(before_map.keys() | after_map.keys()):
                    old, new = before_map.get(key), after_map.get(key)
                    if old != new:
                        for item in (old, new):
                            if item and DATA_TABLE.fullmatch(item["tbl_name"]):
                                touched_tables.add(item["tbl_name"])
                        changes.append({'operation': 'create' if old is None else 'drop' if new is None else 'alter', 'object_type': key[0], 'name': key[1], 'before_sql': old['sql'] if old else None, 'after_sql': new['sql'] if new else None})
                after_counts = {table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}
                impacts = [{'table': table, 'rows_before': before_counts.get(table), 'rows_after': after_counts.get(table)} for table in sorted(touched_tables | {table for table in before_counts.keys() | after_counts.keys() if before_counts.get(table) != after_counts.get(table)})]
                result = {'row_impacts': impacts, 'status': 'applied' if apply else 'preview', 'migration_id': migration_id, 'preview_hash': expected_hash, 'schema_hash_before': before_hash, 'schema_hash_after': schema_digest(after), 'changes': changes, 'changed_rows': changed_rows, 'replayed': False, 'message': 'Migration applied atomically and audited.' if apply else 'Preview rolled back; no changes saved. Review SQL/schema changes and data impact with the user, then apply identical statements with this preview_hash. The hash binds schema and statements, not current data; apply rechecks all constraints.'}
                if not apply:
                    connection.rollback()
                    return result
                connection.set_progress_handler(None, 0)
                batch_id = uuid.uuid4().hex
                for index, statement in enumerate(statements):
                    connection.execute('INSERT INTO audit_mutations(batch_id,statement_index,actor,request_id,sql,params,changed_rows) VALUES (?,?,?,?,?,?,?)', (batch_id,index,audit_actor.get(),request_id.get(),statement['sql'],json.dumps(statement.get('params',[])), statement_changes[index]))
                connection.execute('INSERT INTO system_migrations(migration_id,digest,response) VALUES (?,?,?)', (migration_id,digest,json.dumps(result)))
                event('database.migration.applied', schema_changes=len(changes), changed_rows=changed_rows)
                return result
            except sqlite3.Error:
                raise ValueError('Migration rejected: forbidden SQL, invalid schema, failed constraint, or execution limit; no changes saved') from None
            finally:
                connection.set_authorizer(None)
                connection.set_progress_handler(None,0)

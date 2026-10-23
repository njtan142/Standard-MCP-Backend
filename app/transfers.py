"""Owner-created, expiring file transfers and insert-only portable SQL snapshots."""
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.data import DataService, FUNCTIONS
from app.database import SkillRegistry
from app.observability import audit_actor, request_id, trace_operation, event

MAX_SQL_BYTES = 2 * 1024 * 1024
TTL = 15 * 60


class TransferStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / '.transfers.sqlite3'

    def connect(self, create=False):
        if not create and not self.path.exists():
            raise ValueError('Transfer not found or expired')
        if self.path.is_symlink():
            raise ValueError('Invalid transfer storage path')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        if create:
            connection.execute('''CREATE TABLE IF NOT EXISTS transfers (
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL, project TEXT NOT NULL, actor TEXT NOT NULL,
                expires INTEGER NOT NULL, payload BLOB
            )''')
            connection.commit()
        return connection

    def create(self, kind, project, payload=None):
        token = secrets.token_urlsafe(32)
        identifier = uuid.uuid4().hex
        expires = int(time.time()) + TTL
        connection = self.connect(create=True)
        try:
            with connection:
                connection.execute('DELETE FROM transfers WHERE expires <= ?', (int(time.time()),))
                if connection.execute('SELECT count(*) FROM transfers').fetchone()[0] >= 32:
                    raise ValueError('Transfer limit reached; wait 15 minutes for old links to expire')
                connection.execute('INSERT INTO transfers VALUES (?, ?, ?, ?, ?, ?, ?)', (identifier, hashlib.sha256(token.encode()).hexdigest(), kind, project, audit_actor.get(), expires, payload))
        finally:
            connection.close()
        return identifier, token, datetime.fromtimestamp(expires, timezone.utc).isoformat()

    def lookup(self, *, token=None, identifier=None, kind=None, project=None):
        connection = self.connect()
        try:
            with connection:
                connection.execute('DELETE FROM transfers WHERE expires <= ?', (int(time.time()),))
                if token is not None:
                    row = connection.execute('SELECT * FROM transfers WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
                else:
                    row = connection.execute('SELECT * FROM transfers WHERE id=?', (identifier,)).fetchone()
                if row is None or (kind and row['kind'] != kind) or (project and (row['project'] != project or row['actor'] != audit_actor.get())):
                    raise ValueError('Transfer not found or expired')
                return dict(row)
        finally:
            connection.close()

    def upload(self, token, payload):
        self.lookup(token=token, kind='import')
        if not payload or len(payload) > MAX_SQL_BYTES:
            raise ValueError('SQL upload must be nonempty and at most 2 MiB')
        try:
            payload.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Upload must be a UTF-8 SQL text file') from None
        connection = self.connect()
        try:
            with connection:
                cursor = connection.execute('UPDATE transfers SET payload=? WHERE token_hash=? AND kind=? AND payload IS NULL AND expires>?', (payload, hashlib.sha256(token.encode()).hexdigest(), 'import', int(time.time())))
                if cursor.rowcount != 1:
                    raise ValueError('Upload already received or link expired; request a new import link')
        finally:
            connection.close()


def sql_literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if isinstance(value, str):
        # Hex preserves Unicode and embedded NULs unlike SQLite quote(text).
        return "CAST(X'" + value.encode().hex() + "' AS TEXT)"
    if isinstance(value, float) and not math.isfinite(value):
        return '-9e999' if value < 0 else '9e999'
    return repr(value)


def split_sql(text):
    if not text.strip() or len(text.encode()) > MAX_SQL_BYTES:
        raise ValueError('SQL must be nonblank UTF-8 text and at most 2 MiB')
    if '\x00' in text:
        raise ValueError('SQL source cannot contain NUL characters')
    statements = []
    start = 0
    deadline = time.monotonic() + 2
    for candidate in re.finditer(';', text):
        if time.monotonic() > deadline:
            raise ValueError('SQL parsing budget exceeded')
        part = text[start:candidate.end()]
        if sqlite3.complete_statement(part):
            statements.append(part)
            start = candidate.end()
            if len(statements) > 5000:
                raise ValueError('Import is limited to 5000 statements')
    remainder = text[start:]
    if re.sub(r'--[^\n]*(?:\n|$)|/\*.*?\*/', '', remainder, flags=re.S).strip():
        raise ValueError('Every SQL statement must end with a semicolon')
    if not statements:
        raise ValueError('SQL contains no statements')
    return statements


SYSTEM_TABLES = {'system_skills', 'audit_mutations', 'system_write_requests', 'system_sql_imports', 'system_migrations'}


def allowed_table(name):
    return name in SYSTEM_TABLES or name == 'sqlite_sequence' or bool(re.fullmatch(r'data_[a-z][a-z0-9_]*', name or ''))


def normalized_schema(sql):
    return re.sub(r'\s+', ' ', sql.strip()).lower()


class SQLTransfers:
    def __init__(self, settings, project, registry):
        self.settings = settings
        self.project = project
        self.registry = registry
        self.store = TransferStore(settings.data_dir)

    @trace_operation
    def export(self):
        chunks = ['-- Standard MCP full project SQL v1\nBEGIN TRANSACTION;\n']
        size = len(chunks[0].encode())
        count = 0
        def add(text):
            nonlocal size
            size += len(text.encode())
            if size > MAX_SQL_BYTES:
                raise ValueError('Export exceeds 2 MiB; use an offline SQLite backup for larger projects')
            chunks.append(text)
        with self.registry.connect() as connection:
            connection.execute('BEGIN')
            schema = list(connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type DESC, name"))
            tables = []
            for row in schema:
                if row['type'] == 'table' and row['name'] == 'sqlite_sequence':
                    continue
                if row['type'] == 'table' and allowed_table(row['name']):
                    if not re.match(r'CREATE TABLE\b', row['sql'], re.I):
                        raise ValueError('Virtual tables are not supported in SQL backups')
                    tables.append(row['name'])
                    add(row['sql'] + ';\n')
                elif row['type'] != 'index' or not allowed_table(row['tbl_name']):
                    raise ValueError('Project contains unsupported tables, views, or triggers; use an offline backup')
            for table in tables:
                cursor = connection.execute(f'SELECT * FROM "{table}"')
                columns = [column[0] for column in cursor.description]
                quoted = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
                for row in cursor:
                    count += 1
                    if count > 5000:
                        raise ValueError('Export exceeds 5000 rows; use an offline SQLite backup')
                    add(f'INSERT INTO "{table}" ({quoted}) VALUES (' + ','.join(sql_literal(value) for value in row) + ');\n')
            if connection.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
                add('DELETE FROM sqlite_sequence;\n')
                for row in connection.execute('SELECT name,seq FROM sqlite_sequence'):
                    add('INSERT INTO sqlite_sequence(name,seq) VALUES (' + sql_literal(row[0]) + ',' + sql_literal(row[1]) + ');\n')
            for row in schema:
                if row['type'] == 'index':
                    add(row['sql'] + ';\n')
            add('COMMIT;\n')
        payload = ''.join(chunks).encode()
        split_sql(payload.decode())
        _, token, expires = self.store.create('export', self.project, payload)
        return {'status': 'download_ready', 'filename': f'{self.project}.sql', 'download_url': f'{self.settings.public_url}/files/download#{token}', 'expires_at': expires, 'byte_count': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(), 'row_count': count, 'tables': tables, 'message': 'Full project SQL: schema, skills, data, audit history, and receipts. Download link expires in 15 minutes. OAuth settings and transfer credentials are excluded.'}

    def prepare_import(self):
        identifier, token, expires = self.store.create('import', self.project)
        return {'status': 'awaiting_upload', 'import_id': identifier, 'upload_url': f'{self.settings.public_url}/files/upload#{token}', 'expires_at': expires, 'message': 'Open this link to upload a full project .sql file. Upload only stages it. Call import_sql with this import_id to preview, then apply=true only after user authorization to replace this project.'}

    @trace_operation
    def import_file(self, import_id=None, sql_content=None, apply=False):
        if import_id and sql_content is not None:
            raise ValueError('Supply import_id or sql_content, not both')
        if not import_id and sql_content is None:
            if apply:
                raise ValueError('Provide an import_id to apply')
            return self.prepare_import()
        if sql_content is not None:
            if apply:
                raise ValueError('Stage and preview SQL first, then apply using its import_id')
            payload = sql_content.encode()
            split_sql(sql_content)
            import_id, _, _ = self.store.create('import', self.project, payload)
        record = self.store.lookup(identifier=import_id, kind='import', project=self.project)
        if record['payload'] is None:
            return {'status': 'awaiting_upload', 'import_id': import_id, 'message': 'File not uploaded yet; use the upload link returned when this import was created.'}
        payload = bytes(record['payload'])
        text = payload.decode('utf-8-sig')
        statements = split_sql(text)
        digest = hashlib.sha256(payload).hexdigest()
        # Staging never has a connection or ATTACH route to the live database.
        stage = sqlite3.connect(':memory:', cached_statements=0, isolation_level=None)
        stage.row_factory = sqlite3.Row
        try:
            stage.execute('PRAGMA max_page_count=8192')
            stage.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 128)
            stage.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQL_BYTES)
            stage.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
            stage.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            deadline = time.monotonic() + 5
            steps = 0
            def authorize(action, first, second, database, source):
                if source is not None or database not in {None, 'main'}:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_TABLE and allowed_table(first):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_REINDEX and (re.fullmatch(r"data_[a-z][a-z0-9_]*", first or "") or (first or "").startswith("sqlite_autoindex_")):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_CREATE_INDEX and allowed_table(second):
                    return sqlite3.SQLITE_OK
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_READ} and (allowed_table(first) or first in {'sqlite_master', 'sqlite_schema'}):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_DELETE and first == 'sqlite_sequence':
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and second and second.lower() in FUNCTIONS:
                    return sqlite3.SQLITE_OK
                event("sql.restore.action.denied", action=action)
                return sqlite3.SQLITE_DENY
            def progress(*unused):
                nonlocal steps
                steps += 1000
                if time.monotonic() > deadline or steps > 2000000:
                    raise ValueError('SQL restore execution limit exceeded')
                return 0
            stage.set_authorizer(authorize)
            stage.set_progress_handler(progress, 1000)
            for statement in statements:
                leading = re.sub(r'\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*', '', statement, flags=re.S)
                if re.fullmatch(r'(?:BEGIN(?: TRANSACTION)?|COMMIT|END)\s*;', leading, re.I):
                    continue
                stage.execute(statement)
            stage.set_authorizer(None)
            stage.set_progress_handler(None, 0)
            stage.commit()
            stage.execute('PRAGMA foreign_keys=ON')
            if stage.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or stage.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('SQL restore failed database integrity checks')
            schema = list(stage.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL"))
            if any(row['type'] not in {'table','index'} or not allowed_table(row['tbl_name']) for row in schema):
                raise ValueError('Unsupported schema in project SQL')
            with self.registry.connect() as destination:
                existing = destination.execute('SELECT response FROM system_sql_imports WHERE import_id=?', (import_id,)).fetchone()
                if existing:
                    return {**json.loads(existing[0]), 'replayed': True}
                expected = {row['name']: normalized_schema(row['sql']) for row in destination.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'") if row['name'] in SYSTEM_TABLES}
                # Older snapshots predate the migration ledger; add that empty
                # metadata table without changing restored records or skills.
                if not stage.execute("SELECT 1 FROM sqlite_schema WHERE name='system_migrations'").fetchone():
                    definition = destination.execute("SELECT sql FROM sqlite_schema WHERE name='system_migrations'").fetchone()
                    stage.execute(definition[0])
                    schema = list(stage.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL"))
                actual = {row['name']: normalized_schema(row['sql']) for row in schema if row['type']=='table' and row['name'] in SYSTEM_TABLES}
                if actual != expected:
                    raise ValueError('Full restore requires the current project system-table schemas; use export_sql from the same backend version')
                for row in stage.execute('SELECT name,description,content,enabled,version FROM system_skills'):
                    SkillRegistry.validate_name(row['name'])
                    SkillRegistry.validate_text(row['description'], 'Description', 1000)
                    SkillRegistry.validate_text(row['content'], 'Content', 65536)
                    if row['enabled'] not in {0,1} or type(row['version']) is not int or row['version'] < 1:
                        raise ValueError('Invalid skill fields in SQL snapshot')
                tables = sorted(row['name'] for row in schema if row['type']=='table' and row['name'] != 'sqlite_sequence')
                if 'data_records' not in tables:
                    raise ValueError('Full project SQL is missing data_records')
                # Require the generic table schema to remain compatible with normal tools.
                expected_records = destination.execute("SELECT sql FROM sqlite_schema WHERE name='data_records'").fetchone()[0]
                restored_records = stage.execute("SELECT sql FROM sqlite_schema WHERE name='data_records'").fetchone()[0]
                if normalized_schema(expected_records) != normalized_schema(restored_records):
                    raise ValueError('Incompatible data_records schema')
                count = sum(stage.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables)
                result = {'status': 'restored' if apply else 'preview', 'import_id': import_id, 'sha256': digest, 'row_count': count, 'tables': tables, 'replayed': False, 'message': 'Entire project restored; snapshot audit history retained and restore event appended.' if apply else 'Validation passed. No changes saved. apply=true will REPLACE this project, including current skills, data, audit history, and receipts, with this snapshot.'}
                if not apply:
                    return result
                stage.execute('INSERT INTO audit_mutations(batch_id,statement_index,actor,request_id,sql,params,changed_rows) VALUES (?,0,?,?,?,?,?)', (uuid.uuid4().hex, audit_actor.get(), request_id.get(), '-- Full SQL restore ' + digest, json.dumps({'import_id': import_id, 'sha256': digest, 'tables': tables}), count))
                stage.execute('INSERT INTO system_sql_imports(import_id,response) VALUES (?,?)', (import_id, json.dumps(result)))
                stage.commit()
                deadline = time.monotonic() + 5
                def backup_progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise ValueError('Restore lock timeout; project was not restored')
                stage.backup(destination, pages=128, progress=backup_progress, sleep=0.01)
                return result
        except sqlite3.Error:
            raise ValueError('SQL restore rejected: unsupported SQL, invalid snapshot, duplicate keys, or execution limit; project was not changed') from None
        finally:
            stage.close()

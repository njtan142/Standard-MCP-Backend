"""Bounded SQL access enforced by SQLite's authorizer, independent of skill text."""
import base64
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from app.observability import audit_actor, request_id, trace_operation

DATA_TABLE = re.compile(r"data_[a-z][a-z0-9_]*\Z")
FUNCTIONS = set("count sum avg min max total coalesce ifnull nullif abs round length lower upper trim ltrim rtrim substr substring replace instr typeof quote hex date time datetime julianday strftime unixepoch json json_array json_object json_extract json_type json_valid json_array_length json_set json_insert json_replace json_remove json_patch last_insert_rowid changes printf format like glob unicode char group_concat string_agg json_group_array json_group_object json_quote json_error_position iif likely unlikely likelihood row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value".split())
MAX_RESULT_BYTES = 262144


class DataService:
    def __init__(self, registry):
        self.registry = registry

    @contextmanager
    def connection(self):
        with self.registry.connect() as connection:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_RESULT_BYTES)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 16384)
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            yield connection

    @staticmethod
    def tables(connection):
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'") if DATA_TABLE.fullmatch(row[0])}

    @staticmethod
    def authorizer(data_tables, write=False):
        def authorize(action, first, second, database, source):
            if source is not None or database not in {None, "main"}:
                return sqlite3.SQLITE_DENY
            if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK if first in data_tables or first == "audit_mutations" else sqlite3.SQLITE_DENY
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                return sqlite3.SQLITE_OK if write and first in data_tables else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_OK if second and second.lower() in FUNCTIONS else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY
        return authorize

    @contextmanager
    def restricted(self, connection, tables, write=False):
        deadline = time.monotonic() + 2
        steps = 0
        def progress():
            nonlocal steps
            steps += 1000
            return int(steps > 1000000 or time.monotonic() > deadline)
        connection.set_authorizer(self.authorizer(tables, write))
        connection.set_progress_handler(progress, 1000)
        try:
            yield
        except sqlite3.Error as exc:
            # Never echo SQL, bound values, or DB error text into logs/errors.
            raise ValueError("SQL rejected or failed: use one permitted statement, check schema/constraints and parameters, and stay within execution limits") from exc
        finally:
            connection.set_authorizer(None)
            connection.set_progress_handler(None, 0)

    @staticmethod
    def statement(sql, params):
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 16384:
            raise ValueError("SQL must be nonblank and at most 16384 characters")
        if not isinstance(params, (list, dict)):
            raise ValueError("params must be a list or named-parameter object")
        values = params.values() if isinstance(params, dict) else params
        if any(value is not None and type(value) not in {str, int, float, bool} for value in values):
            raise ValueError("Bind only scalar SQL values; encode JSON objects/arrays as strings")
        values = params.values() if isinstance(params, dict) else params
        if any((type(value) is int and not -(2**63) <= value < 2**63) or (type(value) is float and not math.isfinite(value)) for value in values):
            raise ValueError("Bind finite numbers and signed 64-bit integers")
        if len(json.dumps(params).encode()) > 65536:
            raise ValueError("Bound parameters exceed 64 KiB")

    @trace_operation
    def schema(self):
        with self.connection() as connection:
            tables = []
            for name in sorted(self.tables(connection) | {"audit_mutations"}):
                columns = [{"name": row["name"], "type": row["type"], "nullable": not bool(row["notnull"] or row["pk"]), "primary_key": bool(row["pk"]), "default_value": row["dflt_value"], "generated": row["hidden"] in {2,3}} for row in connection.execute("SELECT * FROM pragma_table_xinfo(?)", (name,))]
                foreign_keys = [{"id": row["id"], "sequence": row["seq"], "table": row["table"], "from_column": row["from"], "to_column": row["to"], "on_update": row["on_update"], "on_delete": row["on_delete"]} for row in connection.execute("SELECT * FROM pragma_foreign_key_list(?)", (name,))]
                indexes = []
                for row in connection.execute("SELECT * FROM pragma_index_list(?)", (name,)):
                    definition = connection.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (row["name"],)).fetchone()
                    indexes.append({"name": row["name"], "unique": bool(row["unique"]), "columns": [column["name"] for column in connection.execute("SELECT * FROM pragma_index_info(?)", (row["name"],))], "partial": bool(row["partial"]), "sql": definition[0] if definition else None})
                definition = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (name,)).fetchone()[0]
                strict = bool(connection.execute("SELECT strict FROM pragma_table_list WHERE schema='main' AND name=?", (name,)).fetchone()[0])
                tables.append({"name": name, "writable": name != "audit_mutations", "columns": columns, "foreign_keys": foreign_keys, "indexes": indexes, "sql": definition, "strict": strict})
        return {"tables": tables, "guidance": "Prefer relational data_* tables with STRICT types, CHECK/UNIQUE constraints, foreign keys, and indexes. Let SQLite enforce integrity and perform joins/aggregation. Use db_migrate to preview schema changes/backfills, then apply with its preview_hash after user authorization. data_records is optional legacy/document storage; preserve existing JSON records until an explicit migration has been verified. Bind values through params. audit_mutations is read-only. Generic SQL cannot change system tables, audit, or schema. Views, triggers, PRAGMAs, ATTACH, and transaction SQL remain denied."}

    @trace_operation
    def query(self, sql, params=None, max_rows=100):
        params = [] if params is None else params
        self.statement(sql, params)
        if type(max_rows) is not int or not 1 <= max_rows <= 500:
            raise ValueError("max_rows must be between 1 and 500")
        with self.connection() as connection:
            tables = self.tables(connection)
            with self.restricted(connection, tables):
                cursor = connection.execute(sql, params)
                if cursor.description is None:
                    raise ValueError("db_query requires a read-only SELECT statement")
                columns = [column[0] for column in cursor.description]
                if len(set(columns)) != len(columns):
                    raise ValueError("Use distinct aliases for duplicate column names")
                rows = []
                size = len(json.dumps(columns).encode())
                truncated = False
                for row in cursor:
                    if len(rows) == max_rows:
                        truncated = True
                        break
                    record = {column: {"base64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value for column, value in zip(columns, row)}
                    row_bytes = len(json.dumps(record, ensure_ascii=True).encode())
                    if size + row_bytes > MAX_RESULT_BYTES:
                        if not rows:
                            raise ValueError("A result row exceeds 256 KiB; select fewer/smaller columns")
                        truncated = True
                        break
                    rows.append(record)
                    size += row_bytes
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}

    @trace_operation
    def execute(self, statements, idempotency_key):
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", idempotency_key):
            raise ValueError("Use an idempotency_key of 1–128 letters, digits, dots, underscores, colons, or hyphens")
        if not 1 <= len(statements) <= 50:
            raise ValueError("A batch must contain 1–50 statements")
        for statement in statements:
            self.statement(statement["sql"], statement.get("params", []))
        payload = json.dumps(statements, sort_keys=True, separators=(",", ":"))
        if len(payload.encode()) > MAX_RESULT_BYTES:
            raise ValueError("Batch exceeds 256 KiB")
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection() as connection:
            # Serialize deduplication, mutations, and audit entries in one transaction.
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT digest, response FROM system_write_requests WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise ValueError("Idempotency key already used with a different batch")
                result = json.loads(previous["response"])
                result["replayed"] = True
                return result
            tables = self.tables(connection)
            batch_id = uuid.uuid4().hex
            results = []
            for index, statement in enumerate(statements):
                sql, params = statement["sql"], statement.get("params", [])
                with self.restricted(connection, tables, write=True):
                    before = connection.total_changes
                    cursor = connection.execute(sql, params)
                    if cursor.description is not None:
                        raise ValueError("db_execute accepts INSERT/UPDATE/DELETE without RETURNING; use db_query to read")
                    changed = connection.total_changes - before
                    last_id = cursor.lastrowid if re.match(r"\s*INSERT\b", sql, re.IGNORECASE) else None
                audit = connection.execute("INSERT INTO audit_mutations(batch_id, statement_index, actor, request_id, sql, params, changed_rows) VALUES (?, ?, ?, ?, ?, ?, ?)", (batch_id, index, audit_actor.get(), request_id.get(), sql, json.dumps(params), changed))
                results.append({"index": index, "changed_rows": changed, "last_insert_rowid": last_id, "audit_id": audit.lastrowid})
            result = {"batch_id": batch_id, "results": results, "replayed": False}
            connection.execute("INSERT INTO system_write_requests(idempotency_key, digest, response) VALUES (?, ?, ?)", (idempotency_key, digest, json.dumps(result)))
        return result

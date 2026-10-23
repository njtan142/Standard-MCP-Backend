"""Insert-only snapshot merging; live history and existing values are never replaced."""
import base64
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from app.data import DATA_TABLE, DataService
from app.observability import audit_actor, request_id, event


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def json_value(value):
    return {'base64': base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def merge_snapshot(source, destination, registry_path, import_id, source_digest, apply, preview_hash):
    receipt_id = 'merge:' + import_id
    destination.execute('BEGIN IMMEDIATE')
    previous = destination.execute('SELECT response FROM system_sql_imports WHERE import_id=?', (receipt_id,)).fetchone()
    if previous:
        return {**json.loads(previous[0]), 'replayed': True}
    deadline = time.monotonic() + 5
    steps = 0
    def progress():
        nonlocal steps
        steps += 1000
        return int(steps > 2000000 or time.monotonic() > deadline)
    source_tables = {row['name']: row['sql'] for row in source.execute("SELECT name,sql FROM sqlite_schema WHERE type='table'") if DATA_TABLE.fullmatch(row['name']) or row['name'] in {'system_skills','system_memories'}}
    before_schema = [tuple(row) for row in destination.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')]
    footprint = hashlib.sha256()
    counts = []
    conflicts = []
    conflict_count = 0
    created_tables = []
    created_indexes = []
    affected_tables = DataService.tables(destination) | {name for name in source_tables if DATA_TABLE.fullmatch(name)}
    def conflict(table, key, reason):
        nonlocal conflict_count
        conflict_count += 1
        if len(conflicts) < 100:
            conflicts.append({'table': table, 'key': {name: json_value(value) for name,value in key.items()}, 'reason': reason})
    def authorize(action, first, second, database, origin):
        if origin is None and database in {None,'main'} and first in {'system_skills','system_memories'} and action in {sqlite3.SQLITE_READ,sqlite3.SQLITE_INSERT}:
            return sqlite3.SQLITE_OK
        return DataService.authorizer(affected_tables, write=True)(action,first,second,database,origin)
    try:
        destination.set_progress_handler(progress,1000)
        destination.execute('PRAGMA defer_foreign_keys=ON')
        # Schema is already validated in isolation by the SQL snapshot loader.
        # Existing definitions must match exactly, including CHECK/default literals.
        for table, definition in sorted(source_tables.items()):
            existing = destination.execute('SELECT type,sql FROM sqlite_schema WHERE name=?',(table,)).fetchone()
            if existing:
                if existing['type']!='table' or existing['sql'].strip()!=definition.strip():
                    raise ValueError(f'Incompatible existing table schema: {table}; use a reviewed db_migrate plan first')
            else:
                if not DATA_TABLE.fullmatch(table):
                    raise ValueError('Merge cannot create internal tables')
                destination.execute(definition)
                created_tables.append(table)
            if DATA_TABLE.fullmatch(table):
                for foreign_key in destination.execute('SELECT * FROM pragma_foreign_key_list(?)',(table,)):
                    if foreign_key['table'] not in affected_tables:
                        raise ValueError('Merged foreign keys must reference data_* tables, never internal tables')
        for index in source.execute("SELECT name,tbl_name,sql FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"):
            if not DATA_TABLE.fullmatch(index['tbl_name']):
                continue
            existing = destination.execute('SELECT type,sql FROM sqlite_schema WHERE name=?',(index['name'],)).fetchone()
            if existing:
                if existing['type']!='index' or existing['sql'].strip()!=index['sql'].strip():
                    raise ValueError(f'Incompatible existing index schema: {index["name"]}')
            else:
                destination.execute(index['sql'])
                created_indexes.append(index['name'])
        for table in sorted(source_tables):
            columns = list(source.execute('SELECT * FROM pragma_table_xinfo(?)',(table,)))
            writable = [column['name'] for column in columns if column['hidden']==0]
            primary = [column['name'] for column in sorted(columns,key=lambda column:column['pk']) if column['pk']]
            if not primary:
                raise ValueError(f'Merge requires an explicit primary key for table {table}; implicit rowids are not portable identities')
            selected = ','.join(quoted(column) for column in writable)
            predicate = ' AND '.join(quoted(column)+' IS ?' for column in primary)
            insert_sql = f'INSERT INTO {quoted(table)} ({selected}) VALUES ({",".join("?" for _ in writable)}) ON CONFLICT DO NOTHING'
            totals = {'table':table,'inserted':0,'identical':0,'conflicts':0}
            for row in source.execute(f'SELECT {selected} FROM {quoted(table)} ORDER BY {",".join(quoted(column) for column in primary)}'):
                if time.monotonic()>deadline:
                    raise ValueError('Merge execution budget exceeded')
                key = {column:row[column] for column in primary}
                if any(value is None for value in key.values()):
                    raise ValueError(f'Merge requires non-NULL primary keys in table {table}')
                existing = destination.execute(f'SELECT {selected} FROM {quoted(table)} WHERE {predicate}',tuple(key.values())).fetchone()
                footprint.update(json.dumps({'table':table,'key':{k:json_value(v) for k,v in key.items()},'existing':None if existing is None else [json_value(value) for value in existing]},sort_keys=True).encode())
                if existing is not None:
                    if tuple(existing)==tuple(row):
                        totals['identical']+=1
                    else:
                        conflict(table,key,'Existing primary key has different values; existing row preserved')
                        totals['conflicts']+=1
                    continue
                destination.set_authorizer(authorize)
                cursor=destination.execute(insert_sql,tuple(row))
                destination.set_authorizer(None)
                if cursor.rowcount==1:
                    totals['inserted']+=1
                else:
                    conflict(table,key,'A different existing primary key conflicts with a UNIQUE constraint; existing row preserved')
                    totals['conflicts']+=1
            counts.append(totals)
        fk_violations = sum(1 for _ in destination.execute('PRAGMA foreign_key_check'))
        if fk_violations:
            conflict_count+=fk_violations
            if len(conflicts)<100:
                conflicts.append({'table':'[relationships]','key':{},'reason':f'{fk_violations} foreign-key violations; no rows can be committed'})
        summary = {'tables':counts,'created_tables':created_tables,'created_indexes':created_indexes,'conflict_count':conflict_count}
        expected = hashlib.sha256(json.dumps({'project':str(Path(registry_path).resolve()),'import_id':import_id,'source':source_digest,'schema':before_schema,'footprint':footprint.hexdigest(),'summary':summary},sort_keys=True).encode()).hexdigest()
        inserted = sum(table['inserted'] for table in counts)
        result = {'status':'merged' if apply else 'merge_preview','mode':'merge','import_id':import_id,'sha256':source_digest,'preview_hash':expected,'can_apply':conflict_count==0,'row_count':inserted,'inserted_rows':inserted,'identical_rows':sum(table['identical'] for table in counts),'conflict_count':conflict_count,'conflicts':conflicts,'conflicts_truncated':conflict_count>len(conflicts),'merge_tables':counts,'created_tables':created_tables,'created_indexes':created_indexes,'tables':sorted(source_tables),'replayed':False,'message':'Merge committed; existing rows, unrelated tables, audit history, and internal receipts preserved.' if apply else 'Simulation rolled back. Identical rows are skipped; different values or UNIQUE/relationship conflicts block apply. Review this preview before authorizing apply with mode=merge and the returned preview_hash.'}
        if not apply:
            destination.rollback()
            return result
        if conflict_count:
            raise ValueError('Merge has conflicting records or relationships; no changes saved. Preview, resolve conflicts explicitly, and preview again')
        if preview_hash!=expected:
            raise ValueError('Merge preview missing or target changed; preview again and supply its preview_hash. No changes saved')
        destination.set_progress_handler(None,0)
        destination.execute('INSERT INTO audit_mutations(batch_id,statement_index,actor,request_id,sql,params,changed_rows) VALUES (?,0,?,?,?,?,?)',(uuid.uuid4().hex,audit_actor.get(),request_id.get(),'-- SQL merge '+source_digest,json.dumps({'import_id':import_id,**summary}),inserted))
        destination.execute('INSERT INTO system_sql_imports(import_id,response) VALUES (?,?)',(receipt_id,json.dumps(result)))
        event('sql.merge.committed',inserted_rows=inserted,identical_rows=result['identical_rows'],created_tables=len(created_tables),created_indexes=len(created_indexes))
        return result
    except sqlite3.Error:
        raise ValueError('Merge rejected: incompatible schema, constraint, trigger, or execution limit; no changes saved') from None
    finally:
        destination.set_authorizer(None)
        destination.set_progress_handler(None,0)

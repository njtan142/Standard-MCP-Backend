import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.data import DataService
from app.database import SkillRegistry


@pytest.fixture
def service(tmp_path):
    registry = SkillRegistry(str(tmp_path / 'project.sqlite3'))
    registry.initialize()
    registry.create_skill('protected', 'Internal', '# Secret')
    return DataService(registry)


def insert(identifier='one', body='{"amount_minor":120000,"currency":"PHP"}'):
    return {"sql": "INSERT INTO data_records(collection, id, body) VALUES (?, ?, ?)", "params": ['expenses', identifier, body]}


def test_write_read_schema_audit_and_idempotency(service):
    schema = service.schema()
    assert {table['name'] for table in schema['tables']} == {'data_records', 'audit_mutations'}
    result = service.execute([insert()], 'expense-one')
    assert result['results'][0]['changed_rows'] == 1
    assert not result['replayed']
    rows = service.query('SELECT id, json_extract(body, ?) AS amount FROM data_records', ['$.amount_minor'])
    assert rows['rows'] == [{'id': 'one', 'amount': 120000}]
    audit = service.query('SELECT batch_id, changed_rows, actor FROM audit_mutations')
    assert audit['rows'][0]['batch_id'] == result['batch_id']
    assert audit['rows'][0]['changed_rows'] == 1
    assert audit['rows'][0]['actor'] == 'local-development'
    replay = service.execute([insert()], 'expense-one')
    assert replay['replayed'] and replay['batch_id'] == result['batch_id']
    assert service.query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n': 1}]
    with pytest.raises(ValueError, match='different batch'):
        service.execute([insert('two')], 'expense-one')
    assert service.query('SELECT count(*) AS n FROM data_records')['rows'] == [{'n': 1}]


def test_atomic_rollback_including_audit_and_dedup(service):
    with pytest.raises(ValueError):
        service.execute([insert(), insert()], 'rollback')
    assert service.query('SELECT count(*) AS n FROM data_records')['rows'] == [{'n': 0}]
    assert service.query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n': 0}]
    assert not service.execute([insert('fixed')], 'rollback')['replayed']


@pytest.mark.parametrize('sql', [
    'SELECT * FROM system_skills', 'SELECT count(*) FROM system_skills',
    'SELECT * FROM sqlite_schema', 'SELECT * FROM system_write_requests',
    'PRAGMA table_info(system_skills)', 'PRAGMA writable_schema=ON',
    "ATTACH DATABASE ':memory:' AS external", 'BEGIN', 'COMMIT',
    'CREATE TABLE data_escape (id INTEGER)', 'DROP TABLE data_records',
    'SELECT load_extension(?)', 'SELECT randomblob(1000000000)',
    "SELECT * FROM pragma_table_info('system_skills')",
    'SELECT * FROM data_records; SELECT * FROM data_records',
    'DELETE FROM data_records',
])
def test_query_denies_protected_and_unsafe_sql(service, sql):
    with pytest.raises(ValueError):
        service.query(sql)
    assert service.registry.get_skill('protected')['content'] == '# Secret'


@pytest.mark.parametrize('sql', [
    "DELETE FROM system_skills", 'DELETE FROM audit_mutations',
    'DELETE FROM system_write_requests', 'DROP TABLE data_records',
    'CREATE TABLE data_escape (id INTEGER)', 'PRAGMA foreign_keys=OFF',
    'UPDATE sqlite_master SET sql=sql',
    "INSERT INTO data_records SELECT 'x', name, content FROM system_skills",
    'DELETE FROM data_records; DELETE FROM data_records',
    'COMMIT', 'SELECT * FROM data_records',
    "INSERT INTO data_records(collection,id,body) VALUES ('a','b','{}') RETURNING *",
])
def test_write_denies_and_rolls_back_prior_statement(service, sql):
    with pytest.raises(ValueError):
        service.execute([insert(), {'sql': sql, 'params': []}], 'blocked')
    assert service.query('SELECT count(*) AS n FROM data_records')['rows'] == [{'n': 0}]
    assert service.query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n': 0}]
    assert service.registry.get_skill('protected')['enabled']


def test_query_limits_and_parameterized_injection(service):
    injection = "x'); DROP TABLE system_skills; --"
    service.execute([insert(injection), insert('two')], 'parameterized')
    assert service.query('SELECT id FROM data_records WHERE id = ?', [injection])['rows'] == [{'id': injection}]
    result = service.query('SELECT id FROM data_records ORDER BY id', max_rows=1)
    assert result['row_count'] == 1 and result['truncated']
    assert not service.query('SELECT id FROM data_records ORDER BY id', max_rows=2)['truncated']
    with pytest.raises(ValueError):
        service.query('SELECT 1', max_rows=501)
    with pytest.raises(ValueError, match='aliases'):
        service.query('SELECT 1 AS same, 2 AS same')
    with pytest.raises(ValueError):
        service.query('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x) SELECT sum(n) FROM x')


def test_reject_nested_params_and_invalid_json(service):
    with pytest.raises(ValueError, match='scalar'):
        service.execute([insert(body={'amount': 10})], 'nested')
    with pytest.raises(ValueError):
        service.execute([insert(body='not json')], 'invalid-json')
    assert service.query('SELECT * FROM audit_mutations')['rows'] == []


def test_concurrent_identical_batches_and_reopen(service):
    def execute(_):
        return service.execute([insert()], 'same-key')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert sorted(result['replayed'] for result in results) == [False, True]
    assert results[0]['batch_id'] == results[1]['batch_id']
    reopened = DataService(SkillRegistry(service.registry.path))
    reopened.registry.initialize()
    assert reopened.query('SELECT count(*) AS n FROM data_records')['rows'] == [{'n': 1}]
    assert reopened.query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n': 1}]


def test_views_and_triggers_cannot_access_internal_tables(service):
    with service.registry.connect() as connection:
        connection.execute('CREATE VIEW data_view AS SELECT content FROM system_skills')
        connection.execute("CREATE TRIGGER data_trigger AFTER INSERT ON data_records BEGIN DELETE FROM system_skills; END")
    with pytest.raises(ValueError):
        service.query('SELECT * FROM data_view')
    with pytest.raises(ValueError):
        service.execute([insert()], 'trigger')
    assert service.registry.get_skill('protected')['content'] == '# Secret'
    assert service.query('SELECT count(*) AS n FROM data_records')['rows'] == [{'n': 0}]


def test_foreign_keys_and_named_parameters(service):
    with service.registry.connect() as connection:
        connection.execute('CREATE TABLE data_parents(id TEXT PRIMARY KEY)')
        connection.execute('CREATE TABLE data_children(id TEXT PRIMARY KEY, parent TEXT REFERENCES data_parents(id))')
    service.execute([{'sql': 'INSERT INTO data_parents(id) VALUES (:id)', 'params': {'id': 'parent'}}, {'sql': 'INSERT INTO data_children(id,parent) VALUES (?,?)', 'params': ['child', 'parent']}], 'family')
    assert service.query('SELECT parent FROM data_children')['rows'] == [{'parent': 'parent'}]
    with pytest.raises(ValueError):
        service.execute([{'sql': 'DELETE FROM data_parents WHERE id=?', 'params': ['parent']}], 'invalid-delete')
    assert service.query('SELECT id FROM data_parents')['row_count'] == 1
    assert service.query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n': 2}]


def test_large_result_bound_and_binary_encoding(service):
    with service.registry.connect() as connection:
        for i in range(4):
            connection.execute('INSERT INTO data_records(collection,id,body) VALUES (?,?,?)', ('large', str(i), json.dumps({'text': 'x' * 100000})))
    result = service.query('SELECT id, body FROM data_records ORDER BY id', max_rows=100)
    assert result['truncated'] and result['row_count'] == 2
    assert service.query('SELECT CAST(? AS BLOB) AS value', ['abc'])['rows'] == [{'value': {'base64': 'YWJj'}}]


def test_initialization_preserves_existing_skills(tmp_path):
    import sqlite3
    path = str(tmp_path / 'existing.sqlite3')
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE system_skills(name TEXT PRIMARY KEY, description TEXT, content TEXT, enabled INTEGER DEFAULT 1, version INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        connection.execute("INSERT INTO system_skills(name,description,content) VALUES ('existing','Preserved','# Keep')")
    registry = SkillRegistry(path)
    registry.initialize()
    registry.initialize()
    assert registry.get_skill('existing')['content'] == '# Keep'
    assert DataService(registry).query('SELECT * FROM data_records')['rows'] == []

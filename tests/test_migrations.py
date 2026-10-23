import pytest

from app.database import SkillRegistry
from app.data import DataService
from app.migrations import Migrations


@pytest.fixture
def migration(tmp_path):
    registry = SkillRegistry(str(tmp_path / 'project.sqlite3'))
    registry.initialize()
    registry.create_skill('protected', 'Keep', '# Keep')
    return Migrations(registry)


def relational_plan():
    return [
        {'sql': 'CREATE TABLE data_categories(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE) STRICT', 'params': []},
        {'sql': "CREATE TABLE data_expenses(id TEXT PRIMARY KEY, amount_minor INTEGER NOT NULL CHECK(amount_minor>0), currency TEXT NOT NULL CHECK(length(currency)=3), category_id TEXT NOT NULL REFERENCES data_categories(id), expense_date TEXT NOT NULL) STRICT", 'params': []},
        {'sql': 'CREATE INDEX data_expenses_category ON data_expenses(category_id)', 'params': []},
    ]


def apply(migration, identifier, statements):
    preview = migration.migrate(identifier, statements)
    return migration.migrate(identifier, statements, apply=True, preview_hash=preview['preview_hash'])


def test_preview_apply_schema_introspection_and_retry(migration):
    plan = relational_plan()
    preview = migration.migrate('finance-v1', plan)
    assert preview['status']=='preview' and len(preview['changes'])==3
    data = DataService(migration.registry)
    assert {table['name'] for table in data.schema()['tables']}=={'data_records','audit_mutations'}
    with pytest.raises(ValueError, match='Preview'):
        migration.migrate('finance-v1', plan, apply=True)
    result = migration.migrate('finance-v1', plan, apply=True, preview_hash=preview['preview_hash'])
    assert result['status']=='applied'
    assert migration.migrate('finance-v1', plan, apply=True, preview_hash=preview['preview_hash'])['replayed']
    schema = {table['name']:table for table in data.schema()['tables']}
    assert schema['data_expenses']['strict']
    assert schema['data_expenses']['foreign_keys'][0]['table']=='data_categories'
    assert any(index['name']=='data_expenses_category' for index in schema['data_expenses']['indexes'])
    assert 'CHECK(amount_minor>0)' in schema['data_expenses']['sql']
    assert data.query('SELECT count(*) AS n FROM audit_mutations')['rows']==[{'n':3}]
    assert migration.registry.get_skill('protected')['content']=='# Keep'


def test_relational_integrity_and_sql_aggregation(migration):
    apply(migration,'v1',relational_plan())
    data=DataService(migration.registry)
    data.execute([{'sql':'INSERT INTO data_categories VALUES (?,?)','params':['food','Food']},{'sql':'INSERT INTO data_expenses VALUES (?,?,?,?,?)','params':['e1',10000,'PHP','food','2026-10-23']},{'sql':'INSERT INTO data_expenses VALUES (?,?,?,?,?)','params':['e2',5000,'PHP','food','2026-10-23']}],'expenses')
    assert data.query('SELECT c.name,SUM(e.amount_minor) AS total FROM data_expenses e JOIN data_categories c ON e.category_id=c.id GROUP BY c.name')['rows']==[{'name':'Food','total':15000}]
    for params in [['negative',-1,'PHP','food','2026-10-23'],['wrong-type','text','PHP','food','2026-10-23'],['bad-fk',1,'PHP','missing','2026-10-23']]:
        with pytest.raises(ValueError):
            data.execute([{'sql':'INSERT INTO data_expenses VALUES (?,?,?,?,?)','params':params}],params[0])


def test_stale_preview_and_changed_statements(migration):
    plan=relational_plan()
    preview=migration.migrate('original',plan)
    apply(migration,'other',[{'sql':'CREATE TABLE data_other(id INTEGER PRIMARY KEY)','params':[]}])
    with pytest.raises(ValueError,match='Preview'):
        migration.migrate('original',plan,apply=True,preview_hash=preview['preview_hash'])
    preview=migration.migrate('original',plan)
    changed=[{'sql':'CREATE TABLE data_different(id TEXT)','params':[]}]
    with pytest.raises(ValueError,match='Preview'):
        migration.migrate('original',changed,apply=True,preview_hash=preview['preview_hash'])


@pytest.mark.parametrize('sql',[
    'DROP TABLE system_skills', 'ALTER TABLE system_skills ADD COLUMN hacked TEXT',
    'DELETE FROM audit_mutations', 'DELETE FROM system_migrations',
    'CREATE TABLE system_hacked(id)', 'CREATE TABLE not_namespaced(id)',
    "CREATE VIRTUAL TABLE data_fts USING fts5(body)",
    'CREATE VIEW data_view AS SELECT * FROM data_records',
    'CREATE TRIGGER data_trigger AFTER INSERT ON data_records BEGIN DELETE FROM system_skills; END',
    'PRAGMA writable_schema=ON', "ATTACH DATABASE ':memory:' AS other",'COMMIT',
    'SELECT * FROM system_skills','SELECT load_extension(?)',
    'CREATE TABLE data_wrong(id TEXT REFERENCES system_skills(name))',
    'ALTER TABLE data_records RENAME TO data_old', 'DROP TABLE data_records',
])
def test_forbidden_migrations_roll_back(migration,sql):
    with pytest.raises(ValueError):
        migration.migrate('blocked',[{'sql':'CREATE TABLE data_first(id INTEGER)','params':[]},{'sql':sql,'params':[]}])
    assert {table['name'] for table in DataService(migration.registry).schema()['tables']}=={'data_records','audit_mutations'}
    assert migration.registry.get_skill('protected')['content']=='# Keep'


def test_ddl_failure_rolls_back_whole_migration(migration):
    with pytest.raises(ValueError):
        migration.migrate('invalid',[{'sql':'CREATE TABLE data_test(id INTEGER)','params':[]},{'sql':'CREATE TABLE data_test(id INTEGER)','params':[]}])
    assert not any(table['name']=='data_test' for table in DataService(migration.registry).schema()['tables'])


def test_alter_rename_index_drop_and_foreign_key_checks(migration):
    apply(migration,'base',relational_plan())
    apply(migration,'add-column',[{'sql':'ALTER TABLE data_expenses ADD COLUMN notes TEXT','params':[]}])
    apply(migration,'rename-column',[{'sql':'ALTER TABLE data_expenses RENAME COLUMN notes TO description','params':[]}])
    apply(migration,'rename-table',[{'sql':'ALTER TABLE data_expenses RENAME TO data_purchases','params':[]}])
    table = next(table for table in DataService(migration.registry).schema()['tables'] if table['name']=='data_purchases')
    assert 'description' in {column['name'] for column in table['columns']}
    assert table['foreign_keys'][0]['table']=='data_categories'
    apply(migration,'drop-index',[{'sql':'DROP INDEX data_expenses_category','params':[]}])
    apply(migration,'drop-table',[{'sql':'DROP TABLE data_purchases','params':[]}])
    assert 'data_purchases' not in {table['name'] for table in DataService(migration.registry).schema()['tables']}


def test_backfill_preserves_legacy_and_audits_each_statement(migration):
    data=DataService(migration.registry)
    data.execute([{'sql':'INSERT INTO data_records(collection,id,body) VALUES (?,?,?)','params':['expenses','legacy','{"amount_minor":1234,"currency":"PHP","category":"food","date":"2026-10-23"}']}],'legacy')
    plan=relational_plan()+[
        {'sql':'INSERT INTO data_categories(id,name) VALUES (?,?)','params':['food','Food']},
        {'sql':"INSERT INTO data_expenses(id,amount_minor,currency,category_id,expense_date) SELECT id,json_extract(body,'$.amount_minor'),json_extract(body,'$.currency'),json_extract(body,'$.category'),json_extract(body,'$.date') FROM data_records WHERE collection=?",'params':['expenses']},
    ]
    preview=migration.migrate('backfill',plan)
    assert preview['changed_rows']==2
    assert data.query('SELECT id FROM data_records')['rows']==[{'id':'legacy'}]
    assert 'data_expenses' not in {table['name'] for table in data.schema()['tables']}
    migration.migrate('backfill',plan,apply=True,preview_hash=preview['preview_hash'])
    assert data.query('SELECT id,amount_minor FROM data_expenses')['rows']==[{'id':'legacy','amount_minor':1234}]
    assert data.query('SELECT id FROM data_records')['rows']==[{'id':'legacy'}]
    assert [row['changed_rows'] for row in data.query('SELECT changed_rows FROM audit_mutations ORDER BY id')['rows']]==[1,0,0,0,1,1]


def test_rename_to_protected_namespace_rolls_back(migration):
    apply(migration,'create',[{'sql':'CREATE TABLE data_allowed(id TEXT)','params':[]}])
    with pytest.raises(ValueError):
        migration.migrate('escape',[{'sql':'ALTER TABLE data_allowed RENAME TO system_evil','params':[]}])
    assert 'data_allowed' in {table['name'] for table in DataService(migration.registry).schema()['tables']}


def test_data_change_after_preview_is_revalidated_and_failure_atomic(migration):
    data=DataService(migration.registry)
    data.execute([{'sql':'INSERT INTO data_records VALUES (?,?,?,?,?)','params':['expenses','old','{"amount_minor":100}', '2026-10-23','2026-10-23']}],'seed')
    plan=[{'sql':'CREATE TABLE data_money(id TEXT PRIMARY KEY, amount_minor INTEGER CHECK(amount_minor>0)) STRICT','params':[]},{'sql':"INSERT INTO data_money SELECT id,json_extract(body,'$.amount_minor') FROM data_records",'params':[]}]
    preview=migration.migrate('backfill-check',plan)
    data.execute([{'sql':'UPDATE data_records SET body=? WHERE id=?','params':['{"amount_minor":-1}','old']}],'changed-data')
    with pytest.raises(ValueError):
        migration.migrate('backfill-check',plan,True,preview['preview_hash'])
    assert 'data_money' not in {table['name'] for table in data.schema()['tables']}
    assert data.query('SELECT count(*) AS n FROM audit_mutations')['rows']==[{'n':2}]


def test_concurrent_apply_is_retry_safe(migration):
    from concurrent.futures import ThreadPoolExecutor
    plan=relational_plan()
    preview=migration.migrate('concurrent',plan)
    def run(_):
        return migration.migrate('concurrent',plan,True,preview['preview_hash'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,range(2)))
    assert sorted(result['replayed'] for result in results)==[False,True]
    assert DataService(migration.registry).query('SELECT count(*) AS n FROM audit_mutations')['rows']==[{'n':3}]


def test_drop_table_preview_reports_existing_row_loss_without_applying(migration):
    apply(migration,'create',[{'sql':'CREATE TABLE data_disposable(id INTEGER PRIMARY KEY)','params':[]}])
    data=DataService(migration.registry)
    data.execute([{'sql':'INSERT INTO data_disposable VALUES (?)','params':[1]}],'seed-drop')
    preview=migration.migrate('drop-preview',[{'sql':'DROP TABLE data_disposable','params':[]}])
    assert preview['row_impacts']==[{'table':'data_disposable','rows_before':1,'rows_after':None}]
    assert data.query('SELECT id FROM data_disposable')['rows']==[{'id':1}]


def test_user_reported_schema_and_indexes_preview_supported(migration):
    statements=[
        {'sql':"CREATE TABLE data_accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, currency TEXT NOT NULL DEFAULT 'PHP' CHECK (currency = 'PHP'), UNIQUE (id, currency)) STRICT",'params':[]},
        {'sql':'CREATE TABLE data_categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(name)) > 0)) STRICT','params':[]},
        {'sql':"CREATE TABLE data_expenses (id TEXT PRIMARY KEY NOT NULL, amount_centavos INTEGER NOT NULL CHECK (amount_centavos > 0), currency TEXT NOT NULL CHECK (currency = 'PHP'), expense_date TEXT NOT NULL, category_id INTEGER NOT NULL REFERENCES data_categories(id), account_id INTEGER, is_test INTEGER NOT NULL CHECK (is_test IN (0, 1)), source_collection TEXT NOT NULL, source_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY (account_id, currency) REFERENCES data_accounts(id, currency)) STRICT",'params':[]},
        {'sql':'CREATE UNIQUE INDEX data_expenses_source_idx ON data_expenses(source_collection, source_id)','params':[]},
        {'sql':'CREATE INDEX data_expenses_category_date_idx ON data_expenses(category_id, expense_date)','params':[]},
        {'sql':'CREATE INDEX data_expenses_account_date_idx ON data_expenses(account_id, expense_date)','params':[]},
        {'sql':'CREATE INDEX data_expenses_date_idx ON data_expenses(expense_date)','params':[]},
    ]
    preview=migration.migrate('reported-schema',statements)
    assert preview['status']=='preview' and len(preview['changes'])==7
    assert 'data_expenses' not in {table['name'] for table in DataService(migration.registry).schema()['tables']}


@pytest.mark.parametrize('amount,raw',[('100','100'),('100.5','100.5'),('100.05','100.05'),('100.50','100.50'),('1e2','1e2'),('-5','-5')])
def test_json_operators_preserve_numeric_tokens_in_reads_and_migrations(migration,amount,raw):
    document='{"amount":'+amount+',"currency":"PHP"}'
    data=DataService(migration.registry)
    result=data.query("SELECT ? -> '$.amount' AS raw, ? ->> '$.currency' AS currency",[document,document])
    assert result['rows']==[{'raw':raw,'currency':'PHP'}]
    plan=[{'sql':'CREATE TABLE data_raw_token(value TEXT) STRICT','params':[]},{'sql':"INSERT INTO data_raw_token(value) VALUES (? -> '$.amount')",'params':[document]}]
    preview=migration.migrate('raw-token',plan)
    assert preview['changed_rows']==1
    assert 'data_raw_token' not in {table['name'] for table in data.schema()['tables']}


def test_diagnostic_identifies_statement_and_constraint_without_values(migration,caplog):
    plan=[{'sql':'CREATE TABLE data_guard(amount INTEGER CHECK(amount>0), secret TEXT) STRICT','params':[]},{'sql':'INSERT INTO data_guard VALUES (?,?)','params':[-1,'PRIVATE_MIGRATION_VALUE']}]
    with pytest.raises(ValueError) as failure:
        migration.migrate('constraint-details',plan)
    assert 'statement 2/2' in str(failure.value)
    assert 'SQLITE_CONSTRAINT_CHECK' in str(failure.value)
    assert 'PRIVATE_MIGRATION_VALUE' not in str(failure.value)
    assert 'PRIVATE_MIGRATION_VALUE' not in caplog.text
    assert 'data_guard' not in {table['name'] for table in DataService(migration.registry).schema()['tables']}


def test_diagnostic_identifies_blocked_function_without_parameters(migration,caplog):
    with pytest.raises(ValueError) as failure:
        migration.migrate('blocked-function',[{'sql':'SELECT load_extension(?)','params':['PRIVATE_PATH_VALUE']}])
    message=str(failure.value)
    assert 'statement 1/1' in message
    assert 'FUNCTION' in message and 'load_extension' in message
    assert 'PRIVATE_PATH_VALUE' not in message and 'PRIVATE_PATH_VALUE' not in caplog.text

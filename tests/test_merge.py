from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.config import Settings
from app.data import DataService
from app.database import SkillRegistry
from app.transfers import SQLTransfers


@pytest.fixture
def projects(tmp_path):
    settings=Settings('http://127.0.0.1:8000',data_dir=str(tmp_path),local_no_auth=True)
    source=SkillRegistry(str(settings.project_path('source')));source.initialize()
    target=SkillRegistry(str(settings.project_path('target')));target.initialize()
    return settings,source,target


def snapshot(settings,source):
    service=SQLTransfers(settings,'source',source)
    result=service.export()
    return bytes(service.store.lookup(token=urlsplit(result['download_url']).fragment,kind='export')['payload']).decode()


def insert(identifier,body='{"amount":100}'):
    return {'sql':'INSERT INTO data_records(collection,id,body) VALUES (?,?,?)','params':['expenses',identifier,body]}


def test_merge_inserts_missing_skips_identical_preserves_history_receipts_and_unrelated_rows(projects):
    settings,source,target=projects
    source.create_skill('source.skill','New skill','# Instructions')
    target.create_skill('target.skill','Keep skill','# Keep')
    DataService(source).execute([insert('same'),insert('missing')],'source-write')
    DataService(target).execute([insert('same'),insert('target-only')],'target-write')
    service=SQLTransfers(settings,'target',target)
    preview=service.import_file(sql_content=snapshot(settings,source),mode='merge')
    assert preview['status']=='merge_preview' and preview['can_apply']
    assert preview['inserted_rows']==2 and preview['identical_rows']==1
    assert DataService(target).query('SELECT id FROM data_records ORDER BY id')['rows']==[{'id':'same'},{'id':'target-only'}]
    with pytest.raises(ValueError): target.get_skill('source.skill')
    result=service.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    assert result['status']=='merged'
    assert target.get_skill('target.skill')['content']=='# Keep'
    assert target.get_skill('source.skill')['content']=='# Instructions'
    assert DataService(target).query('SELECT id FROM data_records ORDER BY id')['rows']==[{'id':'missing'},{'id':'same'},{'id':'target-only'}]
    assert DataService(target).query('SELECT count(*) AS n FROM audit_mutations')['rows']==[{'n':3}]
    with target.connect() as connection:
        assert connection.execute('SELECT idempotency_key FROM system_write_requests').fetchall()[0][0]=='target-write'
        assert connection.execute('SELECT count(*) FROM system_sql_imports').fetchone()[0]==1
    assert service.import_file(import_id=preview['import_id'],mode='merge',apply=True)['replayed']
    assert DataService(target).query('SELECT count(*) AS n FROM audit_mutations')['rows']==[{'n':3}]


def test_different_values_block_apply_and_preserve_all_existing_data(projects):
    settings,source,target=projects
    DataService(source).execute([insert('same','{"amount":100}'),insert('new')],'seed-source')
    DataService(target).execute([insert('same','{"amount":200}')],'seed-target')
    service=SQLTransfers(settings,'target',target)
    preview=service.import_file(sql_content=snapshot(settings,source),mode='merge')
    assert preview['conflict_count']==1 and not preview['can_apply']
    assert preview['conflicts'][0]['key']=={'collection':'expenses','id':'same'}
    with pytest.raises(ValueError,match='conflicting'):
        service.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    assert DataService(target).query('SELECT id,body FROM data_records')['rows']==[{'id':'same','body':'{"amount":200}'}]
    assert DataService(target).query('SELECT count(*) AS n FROM audit_mutations')['rows']==[{'n':1}]


def test_missing_tables_and_foreign_keys_are_created_and_merged_atomically(projects):
    settings,source,target=projects
    with source.connect() as connection:
        connection.execute('CREATE TABLE data_parents(id INTEGER PRIMARY KEY, name TEXT UNIQUE) STRICT')
        connection.execute('CREATE TABLE data_children(id INTEGER PRIMARY KEY, parent INTEGER NOT NULL REFERENCES data_parents(id)) STRICT')
        connection.execute('CREATE INDEX data_children_parent ON data_children(parent)')
        connection.execute("INSERT INTO data_parents VALUES(1,'Food')")
        connection.execute('INSERT INTO data_children VALUES(2,1)')
    service=SQLTransfers(settings,'target',target)
    preview=service.import_file(sql_content=snapshot(settings,source),mode='merge')
    assert preview['created_tables']==['data_children','data_parents']
    assert preview['created_indexes']==['data_children_parent']
    assert preview['inserted_rows']==2 and preview['can_apply']
    assert 'data_children' not in {table['name'] for table in DataService(target).schema()['tables']}
    service.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    assert DataService(target).query('SELECT p.name FROM data_children c JOIN data_parents p ON c.parent=p.id')['rows']==[{'name':'Food'}]


def test_unique_collision_does_not_misassociate_foreign_keys(projects):
    settings,source,target=projects
    for registry in [source,target]:
        with registry.connect() as connection:
            connection.execute('CREATE TABLE data_parents(id INTEGER PRIMARY KEY, name TEXT UNIQUE) STRICT')
            connection.execute('CREATE TABLE data_children(id INTEGER PRIMARY KEY, parent INTEGER REFERENCES data_parents(id)) STRICT')
    with source.connect() as connection:
        connection.execute("INSERT INTO data_parents VALUES(1,'Food')")
        connection.execute('INSERT INTO data_children VALUES(1,1)')
    with target.connect() as connection:
        connection.execute("INSERT INTO data_parents VALUES(2,'Food')")
    service=SQLTransfers(settings,'target',target)
    preview=service.import_file(sql_content=snapshot(settings,source),mode='merge')
    assert not preview['can_apply'] and preview['conflict_count']>=1
    assert any('UNIQUE' in conflict['reason'] for conflict in preview['conflicts'])
    with pytest.raises(ValueError): service.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    assert DataService(target).query('SELECT id FROM data_parents')['rows']==[{'id':2}]
    assert DataService(target).query('SELECT * FROM data_children')['rows']==[]


def test_stale_or_missing_preview_cannot_commit(projects):
    settings,source,target=projects
    DataService(source).execute([insert('same')],'source')
    service=SQLTransfers(settings,'target',target)
    preview=service.import_file(sql_content=snapshot(settings,source),mode='merge')
    with pytest.raises(ValueError,match='preview'):
        service.import_file(import_id=preview['import_id'],mode='merge',apply=True)
    assert DataService(target).query('SELECT * FROM data_records')['rows']==[]
    DataService(target).execute([insert('same')],'other-change')
    with pytest.raises(ValueError,match='preview'):
        service.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    new_preview=service.import_file(import_id=preview['import_id'],mode='merge')
    assert new_preview['inserted_rows']==0 and new_preview['identical_rows']==1


@pytest.mark.parametrize('schema', ['CREATE TABLE data_unsafe(id INTEGER)','CREATE TABLE data_unsafe(id INTEGER PRIMARY KEY, body TEXT)'])
def test_no_primary_key_or_incompatible_schema_rejected(projects,schema):
    settings,source,target=projects
    with source.connect() as connection: connection.execute(schema)
    if 'PRIMARY KEY' in schema:
        with target.connect() as connection: connection.execute('CREATE TABLE data_unsafe(id INTEGER PRIMARY KEY, body INTEGER)')
    service=SQLTransfers(settings,'target',target)
    with pytest.raises(ValueError): service.import_file(sql_content=snapshot(settings,source),mode='merge')
    assert DataService(target).query('SELECT * FROM audit_mutations')['rows']==[]


def test_generated_column_backup_and_merge(projects):
    settings,source,target=projects
    with source.connect() as connection:
        connection.execute('CREATE TABLE data_computed(id INTEGER PRIMARY KEY, amount INTEGER, doubled INTEGER GENERATED ALWAYS AS (amount*2) STORED) STRICT')
        connection.execute('INSERT INTO data_computed(id,amount) VALUES(1,10)')
    service=SQLTransfers(settings,'target',target)
    preview=service.import_file(sql_content=snapshot(settings,source),mode='merge')
    service.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    assert DataService(target).query('SELECT doubled FROM data_computed')['rows']==[{'doubled':20}]

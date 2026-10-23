import json
import sqlite3
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import SkillRegistry
from app.data import DataService
from app.main import create_app
from app.transfers import SQLTransfers, TransferStore, MAX_SQL_BYTES


@pytest.fixture
def transfer(tmp_path):
    settings = Settings('http://127.0.0.1:8000', data_dir=str(tmp_path), local_no_auth=True)
    registry = SkillRegistry(str(settings.project_path('source')))
    registry.initialize()
    registry.create_skill('finance.transactions', 'Expenses', '# Full content\nUnicode ₱ and NUL \x00 retained')
    DataService(registry).execute([{'sql': 'INSERT INTO data_records(collection,id,body) VALUES (?,?,?)', 'params': ['expenses','test','{"amount":100}']}], 'expense-create')
    return SQLTransfers(settings, 'source', registry)


def download(transfer):
    result = transfer.export()
    token = urlsplit(result['download_url']).fragment
    record = transfer.store.lookup(token=token, kind='export')
    return bytes(record['payload']).decode()


def test_full_roundtrip_preview_atomic_replace_and_retry(transfer):
    sql = download(transfer)
    assert 'CREATE TABLE system_skills' in sql
    assert 'audit_mutations' in sql and 'system_write_requests' in sql
    assert '.transfers' not in sql
    target_registry = SkillRegistry(str(transfer.settings.project_path('target')))
    target_registry.initialize()
    target_registry.create_skill('old', 'Replace me', '# Old')
    target = SQLTransfers(transfer.settings, 'target', target_registry)
    preview = target.import_file(sql_content=sql)
    assert preview['status'] == 'preview'
    assert target_registry.get_skill('old')['content'] == '# Old'
    assert DataService(target_registry).query('SELECT * FROM data_records')['rows'] == []
    restored = target.import_file(import_id=preview['import_id'], apply=True)
    assert restored['status'] == 'restored'
    with pytest.raises(ValueError):
        target_registry.get_skill('old')
    assert target_registry.get_skill('finance.transactions')['content'].endswith('NUL \x00 retained')
    data = DataService(target_registry)
    assert data.query('SELECT id FROM data_records')['rows'] == [{'id':'test'}]
    assert data.query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n':2}]
    original = [{'sql': 'INSERT INTO data_records(collection,id,body) VALUES (?,?,?)', 'params': ['expenses','test','{"amount":100}']}]
    assert data.execute(original, 'expense-create')['replayed']
    assert target.import_file(import_id=preview['import_id'], apply=True)['replayed']
    assert data.query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n':2}]


@pytest.mark.parametrize('sql', [
    "ATTACH DATABASE '/tmp/evil.sqlite3' AS evil;",
    'PRAGMA writable_schema=ON;', "SELECT load_extension('/tmp/evil');",
    'CREATE VIRTUAL TABLE data_bad USING fts5(body);',
    'CREATE TABLE data_ok(id); CREATE TRIGGER data_bad AFTER INSERT ON data_ok BEGIN SELECT 1; END;',
    'CREATE TABLE arbitrary(id);',
    'CREATE TABLE data_bad(id); INSERT INTO data_bad VALUES (1);',
    'BEGIN; SELECT 1; COMMIT;',
])
def test_invalid_restore_never_changes_live_project(transfer, sql):
    with pytest.raises(ValueError):
        transfer.import_file(sql_content=sql)
    assert transfer.registry.get_skill('finance.transactions')['version'] == 1
    assert DataService(transfer.registry).query('SELECT count(*) AS n FROM audit_mutations')['rows'] == [{'n':1}]


def test_upload_download_expiry_and_project_binding(transfer, monkeypatch):
    settings = transfer.settings
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        exported = transfer.export()
        token = urlsplit(exported['download_url']).fragment
        assert client.get('/files/content').status_code == 404
        response = client.get('/files/content', headers={'X-Transfer-Token':token})
        assert response.status_code == 200
        assert response.headers['content-disposition'] == 'attachment; filename="source.sql"'
        assert response.headers['cache-control'] == 'no-store'
        prepared = transfer.import_file()
        upload_token = urlsplit(prepared['upload_url']).fragment
        assert client.post('/files/upload-content', headers={'X-Transfer-Token':token}, content=response.content).status_code == 404
        assert client.post('/files/upload-content', headers={'X-Transfer-Token':upload_token, 'Origin':'https://evil.example'}, content=response.content).status_code == 403
        assert client.post('/files/upload-content', headers={'X-Transfer-Token':upload_token}, content=response.content).status_code == 200
        assert client.post('/files/upload-content', headers={'X-Transfer-Token':upload_token}, content=response.content).status_code == 400
        assert transfer.import_file(import_id=prepared['import_id'])['status'] == 'preview'
        other = SQLTransfers(settings, 'other', transfer.registry)
        with pytest.raises(ValueError):
            other.import_file(import_id=prepared['import_id'])
        import app.transfers
        original_time = app.transfers.time.time()
        monkeypatch.setattr(app.transfers.time, 'time', lambda: original_time+1000)
        assert client.get('/files/content', headers={'X-Transfer-Token':token}).status_code == 404
        with pytest.raises(ValueError):
            transfer.import_file(import_id=prepared['import_id'])


def test_upload_size_and_encoding_limits(transfer):
    result = transfer.import_file()
    token = urlsplit(result['upload_url']).fragment
    with pytest.raises(ValueError):
        transfer.store.upload(token, b'x'*(MAX_SQL_BYTES+1))
    with pytest.raises(ValueError):
        transfer.store.upload(token, b'\xff\xfe')
    assert transfer.store.lookup(token=token)['payload'] is None


def test_mcp_transfer_tools_and_secret_log_redaction(transfer, caplog):
    settings = transfer.settings
    caplog.set_level('DEBUG', logger='app')
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        def call(name, arguments, project='source'):
            response = client.post(f'/mcp?project={project}', headers={'Accept':'application/json, text/event-stream'}, json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments}})
            assert response.status_code == 200
            result = response.json()['result']
            assert not result.get('isError'), result
            return result['structuredContent']
        exported = call('export_sql', {})
        token = urlsplit(exported['download_url']).fragment
        sql = client.get('/files/content', headers={'X-Transfer-Token':token}).content
        prepared = call('import_sql', {}, 'restored')
        upload_token = urlsplit(prepared['upload_url']).fragment
        uploaded = client.post('/files/upload-content', headers={'X-Transfer-Token':upload_token}, content=sql)
        assert uploaded.status_code == 200
        preview = call('import_sql', {'import_id':prepared['import_id']}, 'restored')
        assert preview['status'] == 'preview'
        applied = call('import_sql', {'import_id':prepared['import_id'],'apply':True}, 'restored')
        assert applied['status'] == 'restored'
        skill = call('get_skill', {'name':'finance.transactions'}, 'restored')
        assert skill['content'].endswith('NUL \x00 retained')
        assert token not in caplog.text
        assert upload_token not in caplog.text
        assert 'NUL' not in caplog.text


def test_default_project_record_and_foreign_key_roundtrip(transfer):
    with transfer.registry.connect() as connection:
        connection.execute('CREATE TABLE data_parent(id INTEGER PRIMARY KEY AUTOINCREMENT)')
        connection.execute('CREATE TABLE data_child(id INTEGER PRIMARY KEY, parent INTEGER REFERENCES data_parent(id))')
        connection.execute('INSERT INTO data_parent(id) VALUES (100)')
        connection.execute('DELETE FROM data_parent')
        connection.execute('INSERT INTO data_parent DEFAULT VALUES')
        connection.execute('INSERT INTO data_child VALUES (1,101)')
    sql = download(transfer)
    preview = transfer.import_file(sql_content=sql)
    transfer.import_file(import_id=preview['import_id'], apply=True)
    with transfer.registry.connect() as connection:
        assert connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', ('data_parent',)).fetchone()[0] == 101
        assert connection.execute('PRAGMA foreign_key_check').fetchone() is None

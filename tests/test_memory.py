import pytest

from app.database import SkillRegistry
from app.memory import MemoryService
from app.data import DataService
from app.config import Settings
from app.transfers import SQLTransfers, split_sql
from urllib.parse import urlsplit


@pytest.fixture
def memory(tmp_path):
    registry=SkillRegistry(str(tmp_path/'finance.sqlite3'))
    registry.initialize()
    return MemoryService(registry)


def test_revisions_retirement_reactivation_and_audit(memory):
    first=memory.remember('loan.schedule','loan:123','Compute at mid-month','2026-11-01')
    assert first['version']==1 and not first['retired']
    with pytest.raises(ValueError,match='version conflict'):
        memory.remember('loan.schedule','loan:123','Overwrite')
    second=memory.remember('loan.schedule','loan:123','Compute on the 15th',expected_version=1)
    assert second['version']==2 and second['effective_from'] is None
    with pytest.raises(ValueError,match='version conflict'):
        memory.forget('loan.schedule',1)
    retired=memory.forget('loan.schedule',2)
    assert retired['version']==3 and retired['retired']
    assert memory.recall()['memories']==[]
    assert memory.recall(name='loan.schedule',include_retired=True)['memories']==[retired]
    assert len(memory.recall(name='loan.schedule',history=True,include_retired=True)['memories'])==3
    restored=memory.remember('loan.schedule','loan:123','Compute on the 15th',expected_version=3)
    assert restored['version']==4
    rows=DataService(memory.registry).query('SELECT sql,params FROM audit_mutations')['rows']
    assert len(rows)==4
    assert 'Compute' not in str(rows)


def test_literal_search_subject_and_pagination(memory):
    memory.remember('a','loan:a','Use 100% of preference_a')
    memory.remember('b','loan:b','Something else')
    assert [m['name'] for m in memory.recall(search='100%')['memories']]==['a']
    assert memory.recall(search='_a',subject='loan:b')['memories']==[]
    assert memory.recall(search='PREFERENCE_A',subject='loan:a')['memories'][0]['name']=='a'
    page=memory.recall(limit=1)
    assert page['truncated'] and page['next_offset']==1
    assert memory.recall(limit=1,offset=page['next_offset'])['memories'][0]['name']=='b'


@pytest.mark.parametrize('changes',[
    {'name':'bad name'}, {'subject':''}, {'content':' '}, {'content':'x'*8193},
    {'effective_from':'2026-02-30'}, {'effective_from':'20261101'}, {'expected_version':True},
])
def test_invalid_memory_does_not_write(memory,changes):
    with pytest.raises(ValueError):
        memory.remember(**({'name':'a','subject':'loan:a','content':'Preference'}|changes))
    assert memory.recall()['memories']==[]
    assert DataService(memory.registry).query('SELECT count(*) AS n FROM audit_mutations')['rows']==[{'n':0}]


def test_initialization_preserves_data_and_memory_and_sql_cannot_modify(memory):
    memory.remember('a','loan:a','Preference')
    memory.registry.initialize()
    assert memory.recall()['memories'][0]['version']==1
    for sql in ('SELECT * FROM system_memories','DELETE FROM system_memories'):
        with pytest.raises(ValueError):
            if sql.startswith('SELECT'):
                DataService(memory.registry).query(sql)
            else:
                DataService(memory.registry).execute([{'sql':sql,'params':[]}],'blocked')
    other=SkillRegistry(str(__import__('pathlib').Path(memory.registry.path).parent/'other.sqlite3'))
    other.initialize()
    assert MemoryService(other).recall()['memories']==[]


def test_memory_export_restore_merge_and_old_backup(memory,tmp_path):
    settings=Settings('http://127.0.0.1:8000',data_dir=str(tmp_path),local_no_auth=True)
    memory.remember('a','loan:a','Mid-month')
    memory.remember('a','loan:a','On the 15th',expected_version=1)
    service=SQLTransfers(settings,'finance',memory.registry)
    exported=service.export()
    sql=bytes(service.store.lookup(token=urlsplit(exported['download_url']).fragment,kind='export')['payload']).decode()
    target=SkillRegistry(str(tmp_path/'target.sqlite3'));target.initialize()
    transfers=SQLTransfers(settings,'target',target)
    preview=transfers.import_file(sql_content=sql,mode='merge')
    assert preview['can_apply']
    transfers.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    assert MemoryService(target).recall()['memories'][0]['content']=='On the 15th'
    assert len(MemoryService(target).recall(history=True)['memories'])==2
    restored=SkillRegistry(str(tmp_path/'restored.sqlite3'));restored.initialize()
    restore=SQLTransfers(settings,'restored',restored)
    preview=restore.import_file(sql_content=sql)
    restore.import_file(import_id=preview['import_id'],apply=True)
    assert MemoryService(restored).recall()['memories']==memory.recall()['memories']
    older='\n'.join(statement for statement in split_sql(sql) if 'system_memories' not in statement)
    preview=restore.import_file(sql_content=older)
    assert preview['status']=='preview'


def test_divergent_memory_merge_blocked(memory,tmp_path):
    settings=Settings('http://127.0.0.1:8000',data_dir=str(tmp_path),local_no_auth=True)
    memory.remember('a','loan:a','Source preference')
    target=SkillRegistry(str(tmp_path/'target.sqlite3'));target.initialize()
    MemoryService(target).remember('a','loan:a','Target preference')
    service=SQLTransfers(settings,'finance',memory.registry)
    exported=service.export()
    sql=bytes(service.store.lookup(token=urlsplit(exported['download_url']).fragment,kind='export')['payload']).decode()
    service=SQLTransfers(settings,'target',target)
    preview=service.import_file(sql_content=sql,mode='merge')
    assert not preview['can_apply']
    assert preview['conflicts'][0]['table']=='system_memories'
    with pytest.raises(ValueError,match='conflicting'):
        service.import_file(import_id=preview['import_id'],mode='merge',apply=True,preview_hash=preview['preview_hash'])
    assert MemoryService(target).recall()['memories'][0]['content']=='Target preference'


def test_simultaneous_clients_cannot_lose_an_update(memory):
    from concurrent.futures import ThreadPoolExecutor
    memory.remember('a','loan:a','Initial')
    def update(content):
        try:
            return memory.remember('a','loan:a',content,expected_version=1)['version']
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(update,['Mid-month','End-month']))
    assert sorted(map(str,results))==['2','conflict']
    assert len(memory.recall(history=True)['memories'])==2

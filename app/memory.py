"""Project preferences with optimistic concurrency and append-only revisions."""
import json
import uuid
from datetime import date

from app.database import SkillRegistry
from app.observability import audit_actor, request_id, trace_operation


class MemoryService:
    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def validate(name, subject, content, effective_from):
        SkillRegistry.validate_name(name)
        SkillRegistry.validate_text(subject, 'Subject', 256)
        SkillRegistry.validate_text(content, 'Content', 8192)
        if effective_from is not None:
            try:
                if date.fromisoformat(effective_from).isoformat() != effective_from:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('effective_from must be an ISO calendar date YYYY-MM-DD') from None

    @staticmethod
    def record(row):
        return {**dict(row), 'retired': bool(row['retired'])}

    @trace_operation
    def recall(self, subject=None, search=None, name=None, include_retired=False, history=False, limit=20, offset=0):
        if name is not None:
            SkillRegistry.validate_name(name)
        for value, label, size in ((subject, 'Subject', 256), (search, 'Search', 256)):
            if value is not None:
                SkillRegistry.validate_text(value, label, size)
        if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError('limit must be 1–20 and offset must be 0–100000')
        clauses, params = [], []
        if not history:
            clauses.append('m.version=(SELECT MAX(n.version) FROM system_memories n WHERE n.name=m.name)')
        if not include_retired:
            clauses.append('m.retired=0')
        for column, value in (('subject', subject), ('name', name)):
            if value is not None:
                clauses.append(f'm.{column}=?')
                params.append(value)
        if search is not None:
            clauses.append('(instr(lower(m.name),lower(?)) OR instr(lower(m.subject),lower(?)) OR instr(lower(m.content),lower(?)))')
            params.extend([search]*3)
        with self.registry.connect() as connection:
            rows = connection.execute('SELECT m.* FROM system_memories m WHERE '+(' AND '.join(clauses) or '1')+' ORDER BY m.name,m.version DESC LIMIT ? OFFSET ?', (*params,limit+1,offset)).fetchall()
        return {'memories': [self.record(row) for row in rows[:limit]], 'truncated': len(rows)>limit, 'next_offset': offset+limit if len(rows)>limit else None}

    def append(self, name, subject, content, effective_from, retired, expected_version):
        self.validate(name, subject, content, effective_from)
        if expected_version is not None:
            SkillRegistry.validate_version(expected_version)
        with self.registry.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT * FROM system_memories WHERE name=? ORDER BY version DESC LIMIT 1', (name,)).fetchone()
            if (current is None and expected_version is not None) or (current is not None and expected_version != current['version']):
                raise ValueError('Memory version conflict; recall(name=..., include_retired=true) and review its latest version before retrying')
            version = current['version']+1 if current else 1
            connection.execute('INSERT INTO system_memories(name,version,subject,content,effective_from,retired) VALUES (?,?,?,?,?,?)', (name,version,subject,content,effective_from,int(retired)))
            connection.execute('INSERT INTO audit_mutations(batch_id,statement_index,actor,request_id,sql,params,changed_rows) VALUES (?,0,?,?,?,?,1)', (uuid.uuid4().hex,audit_actor.get(),request_id.get(),'-- Memory '+('retire' if retired else 'remember'),json.dumps({'name':name,'version':version})))
            return self.record(connection.execute('SELECT * FROM system_memories WHERE name=? AND version=?',(name,version)).fetchone())

    @trace_operation
    def remember(self, name, subject, content, effective_from=None, expected_version=None):
        return self.append(name,subject,content,effective_from,False,expected_version)

    @trace_operation
    def forget(self, name, expected_version):
        SkillRegistry.validate_name(name)
        SkillRegistry.validate_version(expected_version)
        # Hold the project lock through read and append; version check also protects external writers.
        with self.registry.connect() as connection:
            current = connection.execute('SELECT * FROM system_memories WHERE name=? ORDER BY version DESC LIMIT 1',(name,)).fetchone()
            if current is None:
                raise ValueError('Memory not found')
            if current['retired']:
                raise ValueError('Memory already retired')
            return self.append(name,current['subject'],current['content'],current['effective_from'],True,expected_version)

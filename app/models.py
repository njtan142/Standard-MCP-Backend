"""Explicit MCP output schemas for skill tools."""
from pydantic import BaseModel, Field, JsonValue


class SkillSummary(BaseModel):
    name: str
    description: str
    enabled: bool
    version: int = Field(ge=1)


class Skill(SkillSummary):
    content: str = Field(description="Full Markdown instructions stored for this skill")
    created_at: str
    updated_at: str


class DeletedSkill(BaseModel):
    name: str
    deleted: bool


class SQLStatement(BaseModel):
    sql: str = Field(min_length=1, max_length=16384)
    params: list[JsonValue] | dict[str, JsonValue] = Field(default_factory=list)


class ColumnSchema(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool


class TableSchema(BaseModel):
    name: str
    writable: bool
    columns: list[ColumnSchema]


class DatabaseSchema(BaseModel):
    tables: list[TableSchema]
    guidance: str


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, JsonValue]]
    row_count: int
    truncated: bool


class StatementResult(BaseModel):
    index: int
    changed_rows: int
    last_insert_rowid: int | None
    audit_id: int


class ExecuteResult(BaseModel):
    batch_id: str
    results: list[StatementResult]
    replayed: bool = False

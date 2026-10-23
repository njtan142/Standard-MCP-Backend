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
    default_value: str | None = None
    generated: bool = False


class ForeignKeySchema(BaseModel):
    id: int
    sequence: int
    table: str
    from_column: str
    to_column: str | None
    on_update: str
    on_delete: str


class IndexSchema(BaseModel):
    name: str
    unique: bool
    columns: list[str | None]
    partial: bool
    sql: str | None


class TableSchema(BaseModel):
    name: str
    writable: bool
    columns: list[ColumnSchema]
    foreign_keys: list[ForeignKeySchema] = Field(default_factory=list)
    indexes: list[IndexSchema] = Field(default_factory=list)
    sql: str | None = None
    strict: bool = False


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


class MergeTableResult(BaseModel):
    table: str
    inserted: int
    identical: int
    conflicts: int


class MergeConflict(BaseModel):
    table: str
    key: dict[str, JsonValue]
    reason: str


class SQLTransferResult(BaseModel):
    status: str
    message: str
    mode: str = "restore"
    preview_hash: str | None = None
    can_apply: bool | None = None
    inserted_rows: int | None = None
    identical_rows: int | None = None
    conflict_count: int = 0
    conflicts: list[MergeConflict] = Field(default_factory=list)
    conflicts_truncated: bool = False
    merge_tables: list[MergeTableResult] = Field(default_factory=list)
    created_tables: list[str] = Field(default_factory=list)
    created_indexes: list[str] = Field(default_factory=list)
    filename: str | None = None
    download_url: str | None = None
    upload_url: str | None = None
    import_id: str | None = None
    expires_at: str | None = None
    byte_count: int | None = None
    sha256: str | None = None
    row_count: int | None = None
    tables: list[str] | None = None
    replayed: bool = False


class SchemaChange(BaseModel):
    operation: str
    object_type: str
    name: str
    before_sql: str | None
    after_sql: str | None


class TableImpact(BaseModel):
    table: str
    rows_before: int | None
    rows_after: int | None


class MigrationResult(BaseModel):
    status: str
    migration_id: str
    preview_hash: str
    schema_hash_before: str
    schema_hash_after: str
    changes: list[SchemaChange]
    row_impacts: list[TableImpact] = Field(default_factory=list)
    changed_rows: int
    replayed: bool
    message: str

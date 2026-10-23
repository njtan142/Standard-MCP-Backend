import asyncio
import logging
import re
from urllib.parse import parse_qs, urlsplit
from typing import Annotated, Literal
from pydantic import Field

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.datastructures import Headers
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.auth import JWTVerifier, OwnerVerifier
from app.config import Settings
from app.database import SkillRegistry
from app.models import DeletedSkill, Skill, SkillSummary, SQLStatement, DatabaseSchema, QueryResult, ExecuteResult, SQLTransferResult, MigrationResult
from app.data import DataService
from app.migrations import Migrations
from app.transfers import SQLTransfers
from app.file_routes import file_router
from app.observability import RequestLoggingMiddleware, configure_logging, event, project_name


def project_server(settings: Settings, project: str, registry: SkillRegistry, verifier=None, endpoint_path=None):
    endpoint_path = endpoint_path or f"/{project}/mcp"
    authority = urlsplit(settings.public_url).netloc
    mcp = FastMCP(
        f"Skill Workspace: {project}", stateless_http=True, json_response=True,
        streamable_http_path=endpoint_path,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[authority], allowed_origins=[settings.public_url],
        ),
        token_verifier=None if settings.local_no_auth else OwnerVerifier(verifier, settings.owner_subject, registry, debug_user_ids=settings.auth_debug_user_ids),
        auth=None if settings.local_no_auth else AuthSettings(
            issuer_url=settings.issuer,
            resource_server_url=f"{settings.public_url}{endpoint_path}",
            required_scopes=[],
            validate_token_resource=False,
        ),
    )

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    def about() -> str:
        """Read this project's instructions and current enabled skill directory."""
        return f"Project: {project}\n\n" + registry.about()

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    def list_skills(include_disabled: bool = False) -> list[SkillSummary]:
        """List skill names, descriptions, versions, and enabled status. Disabled skills are hidden unless include_disabled=true."""
        return [SkillSummary.model_validate(skill) for skill in registry.list_skills(include_disabled)]

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    def get_skill(name: str) -> Skill:
        """Retrieve a skill's Markdown and version, including disabled skills for administration. Do not run disabled skills."""
        return Skill.model_validate(registry.get_skill(name))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False})
    def create_skill(name: str, description: str, content: str) -> Skill:
        """Create an enabled skill in this project. Content is Markdown instructions, not executable code. Existing names are rejected."""
        return Skill.model_validate(registry.create_skill(name, description, content))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
    def update_skill(
        name: str,
        expected_version: Annotated[int, Field(strict=True, ge=1)],
        description: str | None = None,
        content: str | None = None,
        enabled: bool | None = None,
    ) -> Skill:
        """Edit or enable/disable a skill. First get_skill and supply its current version; stale updates fail. Omitted fields are preserved."""
        return Skill.model_validate(registry.update_skill(name, expected_version, description, content, enabled))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
    def delete_skill(name: str, expected_version: Annotated[int, Field(strict=True, ge=1)]) -> DeletedSkill:
        """Permanently delete a skill after user authorization. First get_skill and supply its current version; use update_skill(enabled=false) for reversible disabling."""
        return DeletedSkill.model_validate(registry.delete_skill(name, expected_version))

    data = DataService(registry)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    def db_schema() -> DatabaseSchema:
        """Inspect this project's permitted data tables and read-only audit table. Internal system tables are hidden."""
        return DatabaseSchema.model_validate(data.schema())

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    def db_query(sql: str, params: list | dict | None = None, max_rows: Annotated[int, Field(strict=True, ge=1, le=500)] = 100) -> QueryResult:
        """Execute one bounded read-only SELECT on data_* or audit_mutations. Bind values in params. Check truncated; use deterministic ORDER BY and pagination for more rows."""
        return QueryResult.model_validate(data.query(sql, params, max_rows))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    def db_execute(statements: Annotated[list[SQLStatement], Field(min_length=1, max_length=50)], idempotency_key: str) -> ExecuteResult:
        """Atomically execute 1–50 parameterized INSERT/UPDATE/DELETE statements on data_* tables, with audit history. No RETURNING, DDL, or transaction SQL. Use a fresh idempotency_key per intended batch; reuse it only for identical retries. Confirm destructive changes with the user."""
        return ExecuteResult.model_validate(data.execute([statement.model_dump() for statement in statements], idempotency_key))

    migrations = Migrations(registry)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    def db_migrate(migration_id: str, statements: Annotated[list[SQLStatement], Field(min_length=1, max_length=50)], apply: bool = False, preview_hash: str | None = None) -> MigrationResult:
        """Preview controlled relational schema migrations and backfills. Supports data_* tables and data_* indexes: CREATE TABLE/INDEX, ALTER TABLE, DROP TABLE/INDEX, plus data INSERT/UPDATE/DELETE. Prefer STRICT tables, constraints, relationships, indexes, and SQL aggregation. No system/audit changes, views, triggers, virtual tables, PRAGMAs, or transaction SQL. Preserve legacy data_records. First call apply=false; show the user the changes and get authorization, then apply identical SQL with the returned preview_hash. Applied migration_ids are retry-safe."""
        return MigrationResult.model_validate(migrations.migrate(migration_id, [statement.model_dump() for statement in statements], apply, preview_hash))

    transfers = SQLTransfers(settings, project, registry)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    def export_sql() -> SQLTransferResult:
        """Create a full project .sql snapshot (schema, skills, data, audit history, receipts) and return a private browser download link valid for 15 minutes. Files are limited to 2 MiB/5000 rows. OAuth settings and transfer credentials are excluded. Render download_url as a clickable link without dropping its fragment."""
        return SQLTransferResult.model_validate(transfers.export())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
    def import_sql(import_id: str | None = None, sql_content: str | None = None, apply: bool = False, mode: Literal["restore", "merge"] = "restore", preview_hash: str | None = None) -> SQLTransferResult:
        """Import a full SQL snapshot. No arguments prepares a private browser upload link; render it without dropping its fragment. Upload stages only. Call with import_id and mode to preview. mode=restore REPLACES the entire project after user authorization. mode=merge inserts missing data/skills and creates compatible missing tables/indexes, skips identical rows, and blocks different-value/UNIQUE conflicts; it never replaces existing records, audit history, or internal receipts. Merge apply requires mode=merge, apply=true, and the returned preview_hash after user authorization. Current restore mode and client safety checks are unchanged; do not use merge to retry a blocked full restore. sql_content can stage small SQL text. Reusing an applied import_id in the same mode returns its receipt."""
        return SQLTransferResult.model_validate(transfers.import_file(import_id, sql_content, apply, mode, preview_hash))

    @mcp.resource("workspace://about")
    def about_resource() -> str:
        """Project instructions and skill directory."""
        return about()

    return mcp


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    event("application.configured", auth_mode="local_no_auth" if settings.local_no_auth else "owner", log_level=settings.log_level, user_id_diagnostics=settings.auth_debug_user_ids)
    verifier = None if settings.local_no_auth else JWTVerifier(
        settings.issuer, settings.audience, settings.jwks_url
    )
    app = FastAPI(title="Skill Workspaces")
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(file_router(settings))

    @app.get("/health")
    def health():
        # Liveness does not provision projects or require an existing database.
        return {"status": "ok"}

    async def dispatch(scope, receive, send):
        path = scope["path"]
        shared_metadata = path == "/.well-known/oauth-protected-resource/mcp"
        legacy = re.fullmatch(r"/(?:\.well-known/oauth-protected-resource/)?([a-z][a-z0-9-]{0,62})/mcp", path)
        if shared_metadata or (legacy and path.startswith("/.well-known/")):
            if settings.local_no_auth:
                response = JSONResponse({"detail": "Not Found"}, status_code=404)
            else:
                endpoint = "/mcp" if shared_metadata else f"/{legacy[1]}/mcp"
                response = JSONResponse({
                    "resource": f"{settings.public_url}{endpoint}",
                    "authorization_servers": [settings.issuer],
                    "bearer_methods_supported": ["header"],
                })
            await response(scope, receive, send)
            return
        if path != "/mcp" and not legacy:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        query = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="replace"), keep_blank_values=True)
        if path == "/mcp":
            if set(query) != {"project"} or len(query["project"]) != 1:
                await JSONResponse({"detail": "Supply exactly one project query parameter"}, status_code=400)(scope, receive, send)
                return
            project = query["project"][0]
        else:
            if query:
                await JSONResponse({"detail": "Use /mcp?project=NAME for query selection"}, status_code=400)(scope, receive, send)
                return
            project = legacy[1]
        try:
            registry = SkillRegistry(str(settings.project_path(project)))
            project_name.set(project)
            event("project.selected", logging.DEBUG, endpoint="query" if path == "/mcp" else "path")
        except ValueError:
            await JSONResponse({"detail": "Invalid project"}, status_code=400)(scope, receive, send)
            return
        if not settings.local_no_auth:
            authorization = Headers(scope=scope).get("authorization", "")
            if not authorization.lower().startswith("bearer "):
                logging.getLogger(__name__).warning("OAuth challenge: request has no Bearer authorization header (expected before sign-in)")
        if settings.local_no_auth:
            await asyncio.to_thread(registry.initialize)
        server = project_server(settings, project, registry, verifier, endpoint_path=path)
        transport = server.streamable_http_app()
        # Stateless transport: each request has its own manager, with no growing
        # cache of arbitrary public project names. Authentication provisions DBs.
        async with server.session_manager.run():
            await transport(scope, receive, send)

    app.mount("/", dispatch)
    return app

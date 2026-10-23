import asyncio
import re
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from starlette.responses import JSONResponse
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.auth import JWTVerifier, OwnerVerifier
from app.config import Settings
from app.database import SkillRegistry


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
        token_verifier=None if settings.local_no_auth else OwnerVerifier(verifier, settings.owner_subject, registry),
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
    def list_skills() -> list[dict]:
        """List this project's enabled skill names, descriptions, and versions."""
        return registry.list_skills()

    @mcp.resource("workspace://about")
    def about_resource() -> str:
        """Project instructions and skill directory."""
        return about()

    return mcp


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    verifier = None if settings.local_no_auth else JWTVerifier(
        settings.issuer, settings.audience, settings.jwks_url
    )
    app = FastAPI(title="Skill Workspaces")

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
        except ValueError:
            await JSONResponse({"detail": "Invalid project"}, status_code=400)(scope, receive, send)
            return
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

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.auth import JWTVerifier
from app.config import Settings
from app.database import SkillRegistry


def project_server(settings: Settings, project: str, registry: SkillRegistry):
    authority = urlsplit(settings.public_url).netloc
    mcp = FastMCP(
        f"Skill Workspace: {project}", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[authority], allowed_origins=[settings.public_url],
        ),
        token_verifier=None if settings.local_no_auth else JWTVerifier(
            settings.issuer, settings.audience, settings.jwks_url
        ),
        auth=None if settings.local_no_auth else AuthSettings(
            issuer_url=settings.issuer,
            resource_server_url=f"{settings.public_url}/{project}/mcp",
            required_scopes=["skills:read", f"project:{project}"],
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
    registries = {
        project: SkillRegistry(str(Path(settings.data_dir) / filename))
        for project, filename in settings.projects.items()
    }
    servers = {project: project_server(settings, project, registry) for project, registry in registries.items()}
    transports = {project: server.streamable_http_app() for project, server in servers.items()}

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for project, server in servers.items():
                registries[project].initialize()
                await stack.enter_async_context(server.session_manager.run())
            yield

    app = FastAPI(title="Skill Workspaces", lifespan=lifespan)

    @app.get("/health")
    def health():
        for registry in registries.values():
            with registry.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    for project, transport in transports.items():
        # RFC 9728 metadata lives at the origin, outside the project's mount.
        metadata = [route for route in transport.routes if route.path.startswith("/.well-known/")]
        for route in metadata:
            transport.routes.remove(route)
            app.router.routes.append(route)
        app.mount(f"/{project}", transport)
    return app

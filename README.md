# Standard MCP Backend

A Python, SQLite-backed foundation for project-specific, skill-driven MCP services. The current milestone exposes read-only `about` and `list_skills` tools and a `workspace://about` resource over standard Streamable HTTP.

## Projects

Projects are derived automatically from the connection URL. No project registry or per-project environment setting is needed:

| MCP connection | Automatically created database under DATA_DIR |
| --- | --- |
| `/finance/mcp` | `finance.sqlite3` |
| `/projects/mcp` | `projects.sqlite3` |
| `/any-new-project/mcp` | `any-new-project.sqlite3` |

Add `https://tough-jay-refined.ngrok-free.app/finance/mcp` as a ChatGPT connection. The first authorized MCP request initializes the finance database and its skill table. A new valid project path works without a backend restart. Reconnecting reuses its existing database and preserves records.

Names must start with a lowercase letter and contain only lowercase letters, digits, and hyphens, up to 63 characters. Reserved names and unsafe database paths are rejected. Public metadata discovery, unauthenticated requests, and denied tokens never create databases. SQLite files remain separate per project; future data and audit tools must preserve this separation.

The old `/mcp` endpoint is removed. `PUBLIC_URL` means the HTTPS **origin**, without a trailing slash or path. `DATA_DIR` replaces `DATABASE_PATH`. `PROJECTS_JSON` is no longer used and can be removed from an existing `.env`. Existing `workspace.sqlite3` is left untouched; no data is moved automatically.

## Local development

Requires Python 3.12+ and uv:

```sh
uv sync --locked
uv run pytest -q
LOCAL_NO_AUTH=1 uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Connect a local MCP client to `http://127.0.0.1:8000/finance/mcp`. The first local MCP request initializes `data/finance.sqlite3`. Any valid project path works locally. `GET /health` is a liveness check and does not create or inspect project databases. Never expose LOCAL_NO_AUTH mode through a tunnel; it is exclusively for a loopback-bound development process. Production Compose does not enable it or publish backend ports.

## OAuth and project access

ChatGPT → ngrok HTTPS tunnel → Python MCP resource server → per-project SQLite.

An external OAuth authorization server handles sign-in and consent. Python validates RS256 JWT access tokens against configured JWKS, requiring expiry, issuer, audience, subject, and scopes. Configure a shared API identifier as `OAUTH_AUDIENCE`; it is validated independently of project resource URLs.

For your private deployment, grant your owner account `skills:read` and `projects:manage` once at the authorization server. This lets that account automatically create and access any project without registering individual project scopes. Configure these two API permissions in Auth0 and assign them only to trusted owners through RBAC. Do not grant `projects:manage` to untrusted users.

For restricted access later, tokens can instead carry `skills:read` and `project:finance` (or the appropriate project name). Such tokens can create/access that project only. The backend still enforces project grants; knowing a URL does not grant access. Users authorized for the same project share its complete dataset; row-level isolation is not implemented.

Protected-resource metadata is exposed at `/.well-known/oauth-protected-resource/finance/mcp` (and equivalent paths for any valid project name). Unauthenticated requests advertise the correct metadata URL. Configure your provider and ChatGPT client registration for these resource URLs and the `skills:read projects:manage` scopes advertised in discovery. Real provider compatibility, JWKS retrieval, and ChatGPT sign-in remain to be validated. This application is not an authorization server; GitHub access tokens or OIDC ID tokens are not substitutes for access tokens issued for this API.

## Docker Compose and ngrok

1. Copy `.env.example` to `.env`. Your hostname is prefilled as `tough-jay-refined.ngrok-free.app`. Set the OAuth issuer/JWKS/audience and ngrok authtoken locally. No project list is required. Never commit credentials or paste them into chat.
2. Use the ngrok account that owns that dev domain. The sidecar runs `ngrok http --url=tough-jay-refined.ngrok-free.app http://backend:8000`, forwarding all project and metadata paths while preserving the public Host header. Avoid running a second tunnel competing for the same domain.
3. Run `docker compose up --build -d`. Inspect `docker compose logs backend tunnel` for startup issues; do not share logs containing secrets.
4. Verify public `/health`, project metadata, and an unauthenticated 401 at `/finance/mcp`. Add `https://tough-jay-refined.ngrok-free.app/finance/mcp` as the ChatGPT connection and complete OAuth sign-in. Verify `about` and `list_skills` after sign-in. Add other projects as separate ChatGPT connections.
5. Check the actual ChatGPT flow against ngrok free-plan interstitials and limits before relying on the connection. A hostname alone does not prove end-to-end compatibility.

The tunnel only supplies HTTPS transport; no Wrangler or Cloudflare domain is required. Health and resource metadata are public; project skill content is protected by MCP authentication. No REST skill routes are implemented yet.

SQLite files persist in the `workspace-data` Docker volume. Never run `docker compose down -v` unless deleting data intentionally. Backup/restore automation is pending; use SQLite's backup API before deployment upgrades.

## Validation and remaining milestones

Automated tests exercise MCP initialization, discovery, calls, resources, OAuth metadata, invalid-token rejection, per-project scopes, isolated registries, automatic provisioning, denied-request non-provisioning, reconnect persistence, and unsafe project names. Local live HTTP client checks are run separately. Real tunnel and ChatGPT tests need external credentials and provider setup.

The cloud Docker builder has previously failed package-registry DNS resolution. A temporary equivalent image using hash-verified downloaded wheels validated the non-root container; the standard online Dockerfile must still run on a builder with working DNS. CI includes that build but has not been executed on GitHub here.

Next: skill retrieval/CRUD and versioning, authorized generic data operations, audit history, then a complete agent-driven workflow. No application-specific tables or skills are seeded yet.

## Dependency updates

`uv.lock` pins development dependencies; Docker uses a hash-verified production export:

```sh
uv lock
uv export --locked --no-dev --no-emit-project --output-file requirements.txt
```

Commit both files when intentionally updating dependencies and rerun tests/builds.

## Build locally, publish to Docker Hub, run on EC2

This workflow builds the image on your computer. EC2 only pulls and runs it.

### On your computer

Clone the repository, enter it, and authenticate to Docker Hub:

```sh
git clone https://github.com/njtan142/Standard-MCP-Backend.git
cd Standard-MCP-Backend
docker login
```

Create a Docker Hub repository named `standard-mcp-backend` under your account. Replace `YOUR_DOCKERHUB_USERNAME` below. Choose the platform matching your EC2 instance: `linux/amd64` for Intel/AMD instances, or `linux/arm64` for Graviton. This matters when building on Apple Silicon.

```sh
docker buildx build --platform linux/amd64 \
  -t YOUR_DOCKERHUB_USERNAME/standard-mcp-backend:0.1.0 \
  --push .
```

Use a new version tag for each release. The image contains application code and dependencies, never `.env`, tunnel credentials, or databases. You do not need OAuth credentials to build it.

### On EC2

Install Docker Engine and the Compose plugin appropriate to the EC2 OS. Copy `compose.ec2.yaml` and `.env.example` from this repository to a deployment directory; EC2 does not need the application source or a build toolchain.

```sh
cp .env.example .env
chmod 600 .env
```

Edit `.env` as described above and set:

```dotenv
APP_IMAGE=YOUR_DOCKERHUB_USERNAME/standard-mcp-backend:0.1.0
```

For a private Docker Hub image, first run `docker login` on EC2 with an account/token having pull access. Then:

```sh
docker compose -f compose.ec2.yaml pull
docker compose -f compose.ec2.yaml up -d --no-build
docker compose -f compose.ec2.yaml ps
docker compose -f compose.ec2.yaml logs --tail=50 backend tunnel
```

For upgrades, change APP_IMAGE to the new published tag and repeat pull/up. Keep the deployment directory and Compose project name stable so the same database volume is reused. The tunnel makes outbound connections; no inbound application port needs opening in the EC2 security group. Stop any local tunnel using the same ngrok hostname before starting it on EC2.

Docker Hub publication and EC2 deployment are user-operated steps; no registry image has been pushed or EC2 instance deployed by this repository setup.

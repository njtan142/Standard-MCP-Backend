# Standard MCP Backend

A Python, SQLite-backed foundation for project-specific, skill-driven MCP services. The current milestone exposes read-only `about` and `list_skills` tools and a `workspace://about` resource over standard Streamable HTTP.

## Projects

An explicit `PROJECTS_JSON` registry maps URL project names to database filenames:

```json
{"finance":"finance.sqlite3","projects":"projects.sqlite3"}
```

With `PUBLIC_URL=https://tough-jay-refined.ngrok-free.app`:

| MCP connection | Database under DATA_DIR | Required scopes |
| --- | --- | --- |
| `/finance/mcp` | `finance.sqlite3` | `skills:read project:finance` |
| `/projects/mcp` | `projects.sqlite3` | `skills:read project:projects` |

Each configured database has its own skill registry. Future data and audit tools must preserve this separation. `.sql` files are SQL text; these are SQLite `.sqlite3` database files. Unknown projects return 404 and never create databases. Registry validation rejects path traversal, duplicate files, and symlinks escaping DATA_DIR. Project additions require updating the registry and restarting the backend. Skill directory changes are visible immediately without restart.

The old `/mcp` endpoint is removed. `PUBLIC_URL` now means the HTTPS **origin**, without a trailing slash or path. `DATA_DIR` replaces `DATABASE_PATH`. Existing `data/workspace.sqlite3` files are left untouched; they are not automatically assigned to finance. Explicitly map an existing filename if you intend to reuse it.

## Local development

Requires Python 3.12+ and uv:

```sh
uv sync --locked
uv run pytest -q
LOCAL_NO_AUTH=1 uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

The default local project is finance. Connect a local MCP client to `http://127.0.0.1:8000/finance/mcp`. SQLite is initialized idempotently at `data/finance.sqlite3`. `GET /health` checks all configured databases. Never expose LOCAL_NO_AUTH mode through a tunnel; it is exclusively for a loopback-bound development process. Production Compose does not enable it or publish backend ports.

## OAuth and project access

ChatGPT → ngrok HTTPS tunnel → Python MCP resource server → per-project SQLite.

An external OAuth authorization server handles sign-in and consent. Python validates RS256 JWT access tokens against configured JWKS, requiring expiry, issuer, audience, subject, and scopes. Configure a shared API identifier as `OAUTH_AUDIENCE`; it is validated independently of project resource URLs.

Grant each user only their allowed project scopes at the authorization server. A finance connection requires both `skills:read` and `project:finance`; knowing a path does not grant access. The provider must enforce scope grants, not allow any user to self-select any project. Authorized users of the same project share that project's entire dataset; row-level isolation is not implemented.

Protected-resource metadata is exposed at `/.well-known/oauth-protected-resource/finance/mcp` (and equivalent paths per configured project). Unauthenticated requests advertise the correct metadata URL. Configure your provider and ChatGPT client registration for these resource URLs and requested scopes. Real provider compatibility, JWKS retrieval, and ChatGPT sign-in remain to be validated. This application is not an authorization server; GitHub access tokens or OIDC ID tokens are not substitutes for access tokens issued for this API.

## Docker Compose and ngrok

1. Copy `.env.example` to `.env`. Your hostname is prefilled as `tough-jay-refined.ngrok-free.app`. Set the registered projects, OAuth issuer/JWKS/audience, and ngrok authtoken locally. Never commit credentials or paste them into chat.
2. Use the ngrok account that owns that dev domain. The sidecar runs `ngrok http --url=tough-jay-refined.ngrok-free.app http://backend:8000`, forwarding all project and metadata paths while preserving the public Host header. Avoid running a second tunnel competing for the same domain.
3. Run `docker compose up --build -d`. Inspect `docker compose logs backend tunnel` for startup issues; do not share logs containing secrets.
4. Verify public `/health`, project metadata, and an unauthenticated 401 at `/finance/mcp`. Add `https://tough-jay-refined.ngrok-free.app/finance/mcp` as the ChatGPT connection and complete OAuth sign-in. Verify `about` and `list_skills` after sign-in. Add other projects as separate ChatGPT connections.
5. Check the actual ChatGPT flow against ngrok free-plan interstitials and limits before relying on the connection. A hostname alone does not prove end-to-end compatibility.

The tunnel only supplies HTTPS transport; no Wrangler or Cloudflare domain is required. Health and resource metadata are public; project skill content is protected by MCP authentication. No REST skill routes are implemented yet.

SQLite files persist in the `workspace-data` Docker volume. Never run `docker compose down -v` unless deleting data intentionally. Backup/restore automation is pending; use SQLite's backup API before deployment upgrades.

## Validation and remaining milestones

Automated tests exercise MCP initialization, discovery, calls, resources, OAuth metadata, invalid-token rejection, per-project scopes, isolated registries, unknown projects, and unsafe project configuration. Local live HTTP client checks are run separately. Real tunnel and ChatGPT tests need external credentials and provider setup.

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

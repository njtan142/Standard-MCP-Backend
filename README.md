# Standard MCP Backend

A Python, SQLite-backed foundation for project-specific, skill-driven MCP services. The current milestone exposes read-only `about` and `list_skills` tools and a `workspace://about` resource over standard Streamable HTTP.

## Projects

Projects are derived automatically from the connection URL. No project registry or per-project environment setting is needed:

| MCP connection | Automatically created database under DATA_DIR |
| --- | --- |
| `/mcp?project=finance` | `finance.sqlite3` |
| `/mcp?project=projects` | `projects.sqlite3` |
| `/mcp?project=any-new-project` | `any-new-project.sqlite3` |

Add `https://tough-jay-refined.ngrok-free.app/mcp?project=finance` as a ChatGPT connection. The first authorized MCP request initializes the finance database and its skill table. A new valid project path works without a backend restart. Reconnecting reuses its existing database and preserves records.

Names must start with a lowercase letter and contain only lowercase letters, digits, and hyphens, up to 63 characters. Reserved names and unsafe database paths are rejected. Public metadata discovery, unauthenticated requests, and denied tokens never create databases. SQLite files remain separate per project; future data and audit tools must preserve this separation.

The `/mcp` endpoint requires exactly one `project` query parameter; missing, blank, duplicate, or invalid selection returns 400. Legacy `/{project}/mcp` endpoints remain supported and use the same databases. `PUBLIC_URL` means the HTTPS **origin**, without a trailing slash or path. `DATA_DIR` replaces `DATABASE_PATH`. `PROJECTS_JSON` is no longer used and can be removed from an existing `.env`. Existing `workspace.sqlite3` is left untouched; no data is moved automatically.

## Local development

Requires Python 3.12+ and uv:

```sh
uv sync --locked
uv run pytest -q
LOCAL_NO_AUTH=1 uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Connect a local MCP client to `http://127.0.0.1:8000/mcp?project=finance`. The first local MCP request initializes `data/finance.sqlite3`. Any valid project path works locally. `GET /health` is a liveness check and does not create or inspect project databases. Never expose LOCAL_NO_AUTH mode through a tunnel; it is exclusively for a loopback-bound development process. Production Compose does not enable it or publish backend ports.

## OAuth and owner access (no Auth0 roles required)

ChatGPT → ngrok HTTPS tunnel → Python MCP resource server → per-project SQLite.

An external OAuth authorization server handles sign-in and consent. Python validates RS256 JWT access tokens against the configured JWKS and requires expiry, issuer, audience, and subject. `OAUTH_AUDIENCE` is the API identifier configured at your provider, independent of project resource URLs.

The backend then compares the verified token's `sub` exactly against `OWNER_SUBJECT`. Only that owner can automatically create and access any project. A different user's otherwise valid token is rejected before database creation, even if it claims administrative scopes. Remote startup fails if OWNER_SUBJECT is absent. No Auth0 roles, RBAC, or custom permissions are required; any permissions created during earlier setup can be left unused. Project-specific grants and multi-user access are not supported in this single-owner mode.

### Auth0 setup

1. Keep the `Standard MCP Backend` API with identifier `https://tough-jay-refined.ngrok-free.app/mcp` and RS256 signing. RBAC and roles are not needed for this setup.
2. Under **User Management → Users**, create an application login user if one does not exist. Your Auth0 dashboard account is separate. Open the user and copy its **User ID** (for example `auth0|...`) to `OWNER_SUBJECT` in your local `.env`. It is an identifier, not a password. Use the exact ID for the identity you will sign in with; switching identity providers can produce another ID.
3. Create a **Regular Web Application** named `ChatGPT MCP`. Keep its client secret private. Configure the exact callback URL provided by ChatGPT's custom app setup/current documentation in **Allowed Callback URLs**. Client ID/secret belong in ChatGPT's OAuth connection fields, not the backend image.
4. Set the tenant's **Default Audience** to the API identifier, so authorization flows that omit Auth0's audience parameter still receive an access token for this API. If that setting is not available in your dashboard, resolve how to request the API audience before testing the connection.
5. Set OAUTH_ISSUER to your exact Auth0 issuer URL including the trailing slash, OAUTH_JWKS_URL to its `/.well-known/jwks.json` endpoint, and OAUTH_AUDIENCE to the API identifier. Use the owner application account when ChatGPT prompts you to sign in.

The query endpoint advertises protected-resource metadata at `/.well-known/oauth-protected-resource/mcp`, with canonical resource `https://tough-jay-refined.ngrok-free.app/mcp` (no project query). Legacy path endpoints retain their own metadata. It advertises the issuer and resource, with no custom scope requirement. Unauthenticated requests include the correct metadata URL. Real Auth0/ChatGPT sign-in and provider discovery remain unverified; troubleshoot those before claiming the public connection works. GitHub access tokens and OIDC ID tokens are not substitutes for API access tokens.

### ChatGPT resource compatibility check

ChatGPT previously sent the full legacy project path as its OAuth `resource`, causing Auth0 to reject an unregistered API identifier. For query routing, register the canonical `/mcp` API identifier and set OAUTH_AUDIENCE to that same identifier. Then test ChatGPT with `/mcp?project=finance` and inspect Auth0 logs. The backend advertises `/mcp` without the query, but it cannot force ChatGPT to omit the query from its authorization request. If ChatGPT includes it, this approach does not yet solve automatic project connections; do not assume otherwise from local tests. Existing owner and issuer configuration remains unchanged.

## Docker Compose and ngrok

1. Copy `.env.example` to `.env`. Your hostname is prefilled as `tough-jay-refined.ngrok-free.app`. Set the OAuth issuer/JWKS/audience, OWNER_SUBJECT, and ngrok authtoken locally. No project list is required. Never commit credentials or paste them into chat.
2. Use the ngrok account that owns that dev domain. The sidecar runs `ngrok http --url=tough-jay-refined.ngrok-free.app http://backend:8000`, forwarding all project and metadata paths while preserving the public Host header. Avoid running a second tunnel competing for the same domain.
3. Run `docker compose up --build -d`. Inspect `docker compose logs backend tunnel` for startup issues; do not share logs containing secrets.
4. Verify public `/health`, project metadata, and an unauthenticated 401 at `/mcp?project=finance`. Add `https://tough-jay-refined.ngrok-free.app/mcp?project=finance` as the ChatGPT connection and complete OAuth sign-in. Verify `about` and `list_skills` after sign-in. Add other projects as separate ChatGPT connections.
5. Check the actual ChatGPT flow against ngrok free-plan interstitials and limits before relying on the connection. A hostname alone does not prove end-to-end compatibility.

The tunnel only supplies HTTPS transport; no Wrangler or Cloudflare domain is required. Health and resource metadata are public; project skill content is protected by MCP authentication. No REST skill routes are implemented yet.

SQLite files persist in the `workspace-data` Docker volume. Never run `docker compose down -v` unless deleting data intentionally. Backup/restore automation is pending; use SQLite's backup API before deployment upgrades.

## Validation and remaining milestones

Automated tests exercise MCP initialization, discovery, calls, resources, OAuth metadata, invalid-token rejection, owner authorization, isolated registries, automatic provisioning, denied-request non-provisioning, reconnect persistence, and unsafe project names. Local live HTTP client checks are run separately. Real tunnel and ChatGPT tests need external credentials and provider setup.

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

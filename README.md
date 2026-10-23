# Standard MCP Backend

A Python, SQLite-backed foundation for project-specific, skill-driven MCP services. The server exposes `about`, `list_skills`, `get_skill`, `create_skill`, `update_skill`, and `delete_skill`, `db_schema`, `db_query`, `db_execute`, and `db_migrate`, `remember`, `recall`, and `forget`, plus SQL export/import and a `workspace://about` resource over standard Streamable HTTP. Skill changes are stored in the selected project’s SQLite database. Skill tools advertise explicit output schemas and return structured fields alongside text content, including the complete Markdown from get_skill.

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

## Manage skills from ChatGPT

After pulling updates, rebuild locally with `docker compose up --build -d backend`. Refresh the custom app's tools (reconnect if needed) and start a new chat with the app selected. Discovery should show fifteen tools (six skill/discovery tools, three memory tools, four database tools, and SQL export/import):

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `about` | None | Project instructions and enabled skill directory |
| `list_skills` | `include_disabled=false` | Names, descriptions, versions, enabled status |
| `get_skill` | `name` | Full Markdown, enabled status, version, timestamps |
| `create_skill` | `name`, `description`, `content` | Creates an enabled skill at version 1 |
| `update_skill` | `name`, `expected_version`, optional `description`, `content`, `enabled` | Applies changes and increments version |
| `delete_skill` | `name`, `expected_version` | Permanently deletes the skill |

Try: “Create a skill named finance.transactions that instructs you to ask for missing amounts, dates, and categories and never invent them. Then retrieve the saved skill and show its version.”

Skill names are lowercase identifiers with dots, hyphens, or underscores, up to 128 characters. Description and Markdown content must be nonblank and are limited to 1,000 and 65,536 characters respectively. Duplicate creation fails without overwriting. Before editing or deleting, retrieve the latest version; stale changes fail and must be reviewed before retrying. Use `update_skill(enabled=false)` for reversible disabling; disabled content remains retrievable for administration but must not be followed as an active workflow. Names are immutable.

Mutation tools are marked as writes for MCP clients; deletion and updates are marked destructive. Authorization remains owner-only for all tools. No sample skills are installed automatically. Skills store instructions; they do not execute Markdown or add finance tables. Use the database tools described below to store actual records according to your skill instructions.

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

The backend checks the verified token's `sub` against `OWNER_SUBJECT`. A bare configured ID (for example `12345`) matches the user-ID portion of a subject such as `google-oauth2|12345`; the provider prefix is ignored in this mode. A full configured `provider|ID` requires an exact match, including the provider. Bare-ID matching is provider-independent within the configured issuer; use the full form when provider-specific matching is desired. Only that owner can automatically create and access any project. A different user's otherwise valid token is rejected before database creation, even if it claims administrative scopes. Remote startup fails if OWNER_SUBJECT is absent. No Auth0 roles, RBAC, or custom permissions are required; any permissions created during earlier setup can be left unused. Project-specific grants and multi-user access are not supported in this single-owner mode.

### Auth0 setup

1. Keep the `Standard MCP Backend` API with identifier `https://tough-jay-refined.ngrok-free.app/mcp` and RS256 signing. RBAC and roles are not needed for this setup.
2. Under **User Management → Users**, create an application login user if one does not exist. Your Auth0 dashboard account is separate. Open the user and copy its **User ID** (for example `auth0|...`) to `OWNER_SUBJECT` in your local `.env`. It is an identifier, not a password. Either keep the complete provider-prefixed ID for exact matching or configure just the ID after the first `|` for provider-independent matching.
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

SQLite files persist in the gitignored `./data` directory beside your Compose file, bind-mounted at `/srv/data`. See the migration steps below before switching from the earlier named volume. SQL export/import provides bounded project snapshots; keep an offline backup before a full restore.

## Validation and remaining milestones

Automated tests exercise MCP initialization, discovery, calls, resources, OAuth metadata, invalid-token rejection, owner authorization, isolated registries, automatic provisioning, denied-request non-provisioning, reconnect persistence, and unsafe project names. Local live HTTP client checks are run separately. Real tunnel and ChatGPT tests need external credentials and provider setup.

The cloud Docker builder has previously failed package-registry DNS resolution. A temporary equivalent image using hash-verified downloaded wheels validated the non-root container; the standard online Dockerfile must still run on a builder with working DNS. CI includes that build but has not been executed on GitHub here.

Database inspection, bounded queries, atomic audited writes, and retry deduplication are implemented. No application-specific tables or skills are seeded. Prefer relational schemas created through reviewed db_migrate plans; data_records remains optional legacy/document storage. A full finance workflow still needs your chosen record conventions and skill instructions.

## Dependency updates

`uv.lock` pins development dependencies; Docker uses a hash-verified production export:

```sh
uv lock
uv export --locked --no-dev --no-emit-project --output-file requirements.txt
```

Commit both files when intentionally updating dependencies and rerun tests/builds.

## Project memory

Memory stores user preferences and decisions shared by every authorized client of the same project, independently of skill instructions and application records. For example, remember that a particular loan belongs in the mid-month calculation without changing the loan's actual contractual due date. The agent must clarify ambiguous requests before saving a preference. It should recall relevant memories before a workflow, follow applicable skills, and use SQLite for financial facts and calculations. Memory is context, not permission to act or an instruction that overrides the current user.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `remember` | `name`, `subject`, `content`, optional `effective_from`, `expected_version` | Creates a preference or appends a revision |
| `recall` | Optional `name`, `subject`, `search`, `include_retired`, `history`, `limit`, `offset` | Finds current preferences or revision history |
| `forget` | `name`, `expected_version` | Appends a retirement revision; default recall hides it |

Example prompt: “Remember that loan:123 should be included in my mid-month calculations, starting November 1, 2026. This is a calculation preference, not a payment due-date change.” The agent could call:

```json
{"name":"loan.123.calculation-period","subject":"loan:123","content":"Include this loan in mid-month calculations. Its contractual payment due date is unchanged.","effective_from":"2026-11-01"}
```

Use `recall(subject="loan:123")` before calculating. Subjects match exact identifiers; `search` matches a literal, case-insensitive substring of name, subject, or content. Filters combine with AND. Default recall returns only the latest, non-retired revision of each name. Results are ordered by name/version, at most 20 per page; check `truncated` and use `next_offset`. Search does not perform semantic/vector matching. `effective_from` is an ISO calendar date for the agent to interpret; it does not schedule changes, automatically filter results, or make older revisions current until that date. Ask the user about conflicting or unclear preferences.

New names omit `expected_version`. Before changing or reactivating an existing memory, recall its latest revision (with `include_retired=true` if necessary) and supply its version. Every change appends a revision atomically and audits the operation; stale changes fail instead of overwriting another client's preference. `remember` supplies the full replacement subject/content/date; omitting `effective_from` clears that date. Names use the same 128-character lowercase identifier format as skills; subjects are limited to 256 characters and content to 8192. Nothing is automatically extracted or saved from conversations.

`forget` retires a preference rather than permanently erasing it. `recall(name=..., history=true, include_retired=true)` retrieves its history, including retired revisions. Historical revisions are not active guidance. Avoid storing credentials or secrets as memories. Logs record tool names, versions, and content length, not memory contents. Generic SQL tools cannot read or modify internal memory tables.

Existing project databases gain an empty `system_memories` table on authorized access; existing skills and financial records remain unchanged. Full SQL exports/restores include memory revisions. Backups predating this feature import with an empty memory table. Merge imports include missing memory revisions and block divergent values for the same name/version; the highest version becomes current, including a retirement revision. Current audit history remains intact. Review imported preferences as well as application records before applying. Older backend versions cannot import new snapshots containing the memory table; keep the upgraded backend with your backups.

After deployment, refresh the app tools in ChatGPT and Gemini so all three memory tools are discovered. No new environment variables or OAuth settings are required.

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

For upgrades, change APP_IMAGE to the new published tag and repeat pull/up. Keep the deployment directory and its `data` folder stable so the same databases are reused. The tunnel makes outbound connections; no inbound application port needs opening in the EC2 security group. Stop any local tunnel using the same ngrok hostname before starting it on EC2.

Docker Hub publication and EC2 deployment are user-operated steps; no registry image has been pushed or EC2 instance deployed by this repository setup.

## Diagnose OAuth 401 responses

Use `docker compose logs -f --tail=30 backend` while reconnecting ChatGPT. A missing-Bearer challenge is normal before sign-in. Repeated 401s alone do not establish that a token was rejected. Diagnostics distinguish a missing Bearer header from token expiry, audience/issuer mismatch, signing-key lookup failure, malformed tokens, and owner mismatch. By default they never log token values, claim values, or user IDs. For owner mismatch only, temporarily set `AUTH_DEBUG_USER_IDS=1` in `.env` and recreate the backend. The next rejected request logs `signed_in_user_id` from the verified token and `configured_owner_id` from the running configuration. These are quoted so hidden whitespace is visible. Tokens are never logged, and invalid tokens never produce a user-ID diagnostic. Confirm the signed-in identity is the intended owner before updating OWNER_SUBJECT. Set the flag back to `0` and recreate the backend after diagnosis. If sign-in completes but no authenticated request arrives, inspect Auth0's authorization/token-exchange logs and the ChatGPT connection status. Do not share tokens, authorization codes, client secrets, or unredacted authentication URLs.

## Verbose backend logging

Set `LOG_LEVEL=DEBUG` in `.env`, then rebuild/recreate the backend. The default is INFO; supported levels are DEBUG, INFO, WARNING, ERROR, and CRITICAL.

```sh
git pull
docker compose up --build -d backend
docker compose logs -f --tail=100 backend
```

Application lines include a timestamp, level, logger, generated request ID, and selected project. Every HTTP response includes `X-Request-ID`. INFO logs HTTP request start/completion, MCP method/tool names, database operation completion, durations, status codes, byte counts, result counts/versions, and content character counts. Small JSON responses also report whether structured content or tool/protocol errors are present. DEBUG adds project routing, database connections/transactions, signing-token validation progress, and successful owner authorization. Failed operations report exception types without dumping their payloads or exception messages.

Headers, bearer tokens, cookies, tool arguments, skill descriptions, Markdown contents, and raw responses are not logged by application tracing. User IDs remain off unless AUTH_DEBUG_USER_IDS=1 is explicitly enabled. Request-body inspection is capped at 64 KiB and response inspection at 4 KiB; larger messages still pass through intact, with byte totals and timing but reduced metadata. Unknown MCP method/tool strings are logged as `unknown`. Database context follows the request into worker threads. These logs help diagnose operations but are not a durable audit history.

Backend container logs rotate at 10 MiB with three files retained. Your Docker daemon must support the json-file driver used by Compose. For failures, share matching request-ID lines rather than credentials or raw traffic dumps.


## Store and retrieve project data

Four database tools use the same project selection and verified owner check as skill tools:

| Tool | Input | Output |
| --- | --- | --- |
| `db_schema` | None | Permitted tables, columns, and writability |
| `db_query` | `sql`, optional `params`, `max_rows` (default 100, max 500) | Structured columns, rows, count, and `truncated` |
| `db_execute` | `statements` (1–50 SQL/params objects), `idempotency_key` | Batch ID, per-statement change counts, optional insert row ID, audit IDs, replay status |
| `db_migrate` | `migration_id`, `statements`, optional `apply`, `preview_hash` | Schema changes, affected-row counts, preview hash, retry status |

Each project's existing SQLite file is upgraded additively on access, preserving skills and records. The default approach is a relational data_* schema: use typed columns, foreign keys, CHECK/UNIQUE constraints, and indexes, with SQLite joins and aggregation. Skills define domain workflows and SQL conventions; the Python backend contains no finance-specific logic. The compatibility data_records table remains available for document-shaped or legacy records, using `(collection, id)` as its primary key and JSON text in body. Choose stable record IDs and store money as integer minor units plus a currency in your skill's conventions. Caller-written updates must explicitly update `updated_at` when desired.

For compatibility/document storage only, `db_execute` arguments to save a JSON record:

```json
{
  "statements": [{
    "sql": "INSERT INTO data_records(collection, id, body) VALUES (?, ?, ?)",
    "params": ["expenses", "expense-example-001", "{\"amount_minor\":120000,\"currency\":\"PHP\",\"category\":\"shopping\"}"]
  }],
  "idempotency_key": "expense-example-001-create"
}
```

The JSON body above is a string bound as a SQL value, not a nested object parameter. Read it back with `SELECT id, body FROM data_records WHERE collection = ? AND id = ?` and params `["expenses", "expense-example-001"]`. SQLite JSON extraction functions are supported. Ask for missing fields before writing; examples are not real transactions.

Every write batch runs inside one transaction with its audit entries and idempotency receipt. If any statement fails, all earlier writes and audit entries in that batch roll back. Retrying the same batch with the same key returns its original result with `replayed=true`. Reusing that key for different SQL/params fails. Use a new key for each intended new batch; receipt history persists with the project database.

Query `audit_mutations` to inspect committed data writes: SQL text, JSON-bound parameters, changed-row count, batch/statement IDs, verified actor, request ID, and UTC timestamp. This is sensitive project data stored in SQLite, not console logging. Audit entries do not contain before/after row snapshots and do not cover skill-administration operations. Failed batches leave no mutation-audit entries; failure diagnostics remain in application logs.

SQLite's authorizer enforces a default-deny policy. Queries can read physical `data_*` tables and `audit_mutations`. Writes can modify only physical `data_*` tables. Internal system/skill tables, audit changes, schema changes, PRAGMAs, ATTACH/DETACH, transaction commands, views/triggers, and unapproved functions are denied. Schema changes use the dedicated owner-only db_migrate preview/apply flow below. Tables and named indexes must use lowercase `data_` names. Do not edit the database or its schema concurrently with the server.

Each SQL string is limited to one statement and 16 KiB. Bound scalar parameters are limited to 64 KiB per statement; batches to 256 KiB. SQL work is bounded by a progress limit of approximately one million VM steps or two seconds, plus SQLite's lock timeout. Reads cap the row payload at approximately 256 KiB and report truncation; page with deterministic ORDER BY and filters. BLOB results are encoded as base64 objects. Read/RETURNING statements are not accepted by db_execute. SQL limits may reject expensive queries even if valid.

After updating and refreshing the ChatGPT tools, try: “Inspect the schema, then tell me what information the finance.transactions skill requires before saving an expense. Do not invent a transaction.” When you supply an actual record and authorize saving it, ChatGPT can perform the parameterized write, retrieve it, and inspect its audit entry.


## Persistent data in the project directory

Both Compose files bind `./data` to `/srv/data`. All project SQLite files and the internal expiring-transfer store live here. The directory is already gitignored and excluded from Docker build context. No database, SQL upload, or credential is baked into the image.

For a fresh setup, create the directory and configure the host UID/GID before starting Compose:

```sh
mkdir -p data
id -u
id -g
```

Set `APP_UID` and `APP_GID` in `.env` to those two outputs (on your Mac or, separately, the EC2 host). The backend runs as those IDs so the files belong to your local account. If omitted, the image user IDs 10001:10001 are used, which require matching directory ownership. The bind source must exist; Compose will not silently create a root-owned directory. Do not chmod the directory world-writable.

### Move existing records out of the old named volume

Do this before recreating the backend with the updated bind mount. Do not delete the old volume. If `./data` already has databases, stop and choose a separate backup directory rather than overwriting them.

```sh
docker compose stop backend
mkdir -p data
docker compose cp backend:/srv/data/. ./data/
```

This copies the stopped container's original volume contents, including SQLite journals if any. On macOS, the copied files normally belong to the invoking user; verify `ls -ln data`. Set APP_UID/APP_GID as above, then run `git pull` and `docker compose up --build -d`. On EC2 use `docker compose -f compose.ec2.yaml ...` and the newly published image. If files belong to another account, correct ownership for the chosen UID/GID before startup. Retain the original named volume until you have verified your skills and records in ChatGPT.

## Download and upload full project SQL

Refresh the custom app's tools after upgrading. There are fifteen tools, including:

- `export_sql()`: consistent full-project SQL snapshot and a private download link.
- `import_sql()`: opens a private upload flow; `import_sql(import_id=...)` validates/previews; `import_sql(import_id=..., apply=true)` restores after explicit user authorization.

Ask ChatGPT: “Export this entire project as a downloadable SQL file.” Open the returned link in your browser to download `finance.sql` (or the selected project name). It contains the project schema, skills and versions, memory revisions, application records, audit history, and retry/restore receipts. It excludes OAuth configuration, other projects, and transfer capability credentials.

To restore, ask: “Prepare a SQL upload for this project.” Open the upload link, select the `.sql` file, and upload. The page only stages the file and shows its import ID. Return to ChatGPT and ask it to preview that ID. The preview validates SQL in an isolated in-memory database and makes no project changes. After reviewing it, explicitly authorize the restore. Applying it REPLACES the entire selected project, including current audit history, with the snapshot and appends a new restore audit entry. Snapshot audit history is imported as supplied, not independently authenticated history. Export your current project first if you may need to undo a restore.

### Merge a backup without replacing the project

Ask ChatGPT: “Prepare a SQL upload in merge mode. Preview the uploaded backup and show conflicts before applying.” Use `import_sql(mode="merge")` to prepare the upload, then `import_sql(import_id=..., mode="merge")` to preview it. The default mode remains `restore` for compatibility; specify `merge` on every merge call.

Merge accepts the same full-project SQL snapshots. It creates missing `data_*` tables and indexes and inserts missing application records, skills, and memory revisions. Identical rows are skipped. Existing rows and unrelated tables remain intact. Source audit history and internal retry/migration receipts are excluded; current history and receipts are preserved, and the merge appends one audit entry.

Different values under the same primary key, UNIQUE conflicts, or invalid foreign-key relationships block the entire merge. Existing table and index definitions must match exactly; use a reviewed `db_migrate` plan for schema differences. Every merged table needs an explicit, non-NULL primary key. Merge never guesses identities or overwrites conflicting records.

The preview runs inside a rolled-back transaction and reports inserted/identical rows, missing schema, and conflicts. After review and explicit authorization, apply with `import_sql(import_id=..., mode="merge", apply=true, preview_hash="...")`. A missing or stale preview hash rejects the apply. Schema, rows, audit, and receipt commit atomically; retrying the same successful merge returns its receipt. Client safety checks still apply. Merge is a separate operation, not a way to retry a blocked full restore.

You can also stage small SQL text with `import_sql(sql_content=...)`; it still requires a separate apply call using the returned import ID. The server cannot directly read ChatGPT attachment IDs or your computer's file paths, so the browser upload flow is the reliable route. ChatGPT must render the full upload/download URL, retaining the `#fragment`. The fragment holds the short-lived capability and is sent by browser JavaScript only in a request header; it is not present in HTTP request URLs or ordinary access logs. Anyone with the link can use that specific transfer until it expires; treat it as private. No bearer token needs pasting into the browser.

Links expire after 15 minutes; uploads are accepted once and capped at 2 MiB, 5000 SQL statements, and 32 active transfers across the deployment. Expired payloads are deleted on subsequent transfer operations. Use an offline SQLite backup for larger projects. A successful restore retried with the same import ID returns its receipt instead of applying again. Import IDs are bound to the originating project and verified user. Restoring a snapshot later can replace earlier restore receipts along with all other project state.

Supported snapshots contain current-version system schemas, ordinary `data_*` tables, and indexes. Views, triggers, virtual tables, arbitrary table names, extension loading, file attachment, and PRAGMAs in uploaded SQL are rejected. Different backend schema versions may require an offline migration. SQLite validates integrity and foreign keys before restore. The backup API applies a validated snapshot atomically; project operations are serialized in this single-process deployment to avoid in-flight writes being interleaved with restoration. Run one backend worker; coordinate any external database access yourself.

Local HTTP transfer/restore behavior is tested; ChatGPT's rendering of browser links and real ngrok browser transfers require end-to-end validation after deployment.


## Relational schemas and controlled migrations

The agent interprets user intent and follows skills. SQLite enforces storage rules and computes results; Python authenticates and controls access. A generic backend does not require every record to be stored as JSON. Prefer STRICT tables, foreign keys, NOT NULL, CHECK/UNIQUE constraints, appropriate indexes, and SQL joins/aggregation. Use JSON only for genuinely document-shaped optional data or compatibility with existing records.

`db_schema` now reports CREATE TABLE SQL, STRICT status, column defaults/generated status, foreign keys, and indexes as structured metadata. This exposes constraints so the agent can respect the database's actual rules.

`db_migrate` accepts:

```json
{
  "migration_id": "example-v1",
  "statements": [{"sql": "CREATE TABLE data_example(id TEXT PRIMARY KEY, amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), currency TEXT NOT NULL) STRICT", "params": []}],
  "apply": false
}
```

The preview executes in a transaction and rolls back, returning before/after schema changes, per-table row counts (including rows lost by table removal), changed-row count, and a preview_hash. Show that plan and its effects to the user before applying. Call the same tool with identical migration_id/statements, `apply=true`, and the returned preview_hash. The hash binds this project, starting schema, migration ID, and statements; a changed schema or plan requires another preview. It does not freeze current data: apply reexecutes the plan and rechecks all constraints against current records.

Applied migrations write one audit entry per statement and an internal retry receipt atomically. Repeating the same applied migration_id/statements returns its receipt; using that ID with different SQL fails. Failed plans roll back DDL, backfills, audit entries, and receipts together. No arbitrary transaction control, PRAGMAs, ATTACH, extension loading, protected-table changes, views, triggers, or virtual tables are exposed. Standard table/index creation, ALTER TABLE (add/rename/drop columns where SQLite supports it), table/index removal, and parameterized data backfills are supported. DROP and ALTER of the compatibility data_records schema are blocked; ordinary explicit data writes still use their existing permissions.

SQLite implements some ALTER operations using internal schema scans and quick-check helpers; those helpers are enabled only while executing a single ALTER TABLE statement. Direct PRAGMA access remains denied. Migrations are capped at 50 statements, 256 KiB input, approximately two million VM steps/five seconds, and the existing SQL/value limits. Large rebuilds require an offline migration.

An example finance schema is in `examples/finance_relational.json`. It proposes categories, expenses, constraints, and indexes; it is not automatically applied and does not move existing records. The agent can read or reproduce the plan, preview it, explain tradeoffs, and obtain authorization before applying. A relational summary can then be computed directly in SQLite:

```sql
SELECT c.name AS category, e.currency, SUM(e.amount_minor) AS total_minor
FROM data_expenses AS e
LEFT JOIN data_categories AS c ON c.id = e.category_id
GROUP BY c.id, c.name, e.currency;
```

For existing JSON records: inspect their actual fields, units, currencies, precision, and invalid/missing values first. Propose a separate, explicit INSERT ... SELECT backfill into relational tables. Preview and apply only after authorization. Verify row counts, totals per currency, constraints, and representative records; keep legacy rows until the user explicitly authorizes cleanup. Do not silently multiply money or invent missing fields. Update the project's skill after the agreed schema and conversion have been verified; backend upgrades never overwrite skill Markdown.

Full SQL export/import includes the migration ledger and relational schema/indexes. Pre-ledger backups can be restored by adding an empty migration ledger during validation. Other incompatible system schemas still require an offline migration.

### Diagnose rejected migrations

Migration errors identify the 1-based statement number and total (for example `statement 4/9`), SQLite error code, and a sanitized reason. Authorization failures identify the denied operation and safe table/function identifier. Validation failures after execution identify the validation phase instead of blaming an arbitrary statement. The same metadata appears in `database.migration.rejected` logs with the request ID. SQL text, bound values, and raw SQLite exception messages are not dumped into diagnostics. Failed previews/applies still roll back the entire migration.

JSON extraction operators `->` and `->>` are permitted in read, write, and migration SQL. `body -> '$.amount'` preserves the numeric token's textual representation (including exponent notation), which lets a SQL migration validate decimal syntax and precision before integer conversion. `->>` returns an extracted SQL scalar. JSON extraction alone does not make money conversion correct: explicitly reject unsupported currency, scale, notation, and range, and never rely on silent rounding.

### ngrok warning pages during SQL transfer

The browser client sends `ngrok-skip-browser-warning: true` on file download and upload requests. Downloads require `application/sql`, a project `.sql` attachment filename, the expected SQL snapshot marker, and a matching SHA-256 checksum supplied by the backend. HTML/interstitial responses are displayed as errors and never saved by the download client. Uploads also reject HTML files, so an ngrok warning page renamed to `.sql` cannot be staged for restoration.

The first navigation to the transfer page itself can still display ngrok's visitor warning; accept it in the normal browser, then reopen a fresh transfer link if needed. The bypass header applies to the browser client's file requests, not arbitrary links opened outside that client. Don't use “Save Page As” on the transfer page or have an agent fetch the launcher HTML and rename it `.sql`; open the returned link in a normal browser and let the verified download complete. Backend Content-Disposition headers alone cannot bypass an interstitial returned by ngrok before the request reaches Python.

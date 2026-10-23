import time
import json
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import JWTVerifier
from app.config import Settings
from app.main import create_app


@pytest.fixture
def signing(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: SimpleNamespace(key=key.public_key()))
    def token(**overrides):
        claims = {"iss": "https://issuer.example/", "aud": "https://workspace.example/mcp", "sub": "user", "exp": int(time.time()) + 300, "scope": ""}
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256")
    return token


def test_auth_and_mcp(tmp_path, signing):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    with TestClient(create_app(settings), base_url="https://workspace.example") as client:
        assert client.get("/health").json() == {"status": "ok"}
        denied = client.post("/finance/mcp", json={})
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/finance/mcp").json()
        assert metadata["resource"] == settings.public_url + "/finance/mcp"
        assert metadata["authorization_servers"] == [settings.issuer]
        headers = {"Authorization": "Bearer " + signing(), "Accept": "application/json, text/event-stream"}
        def call(method, params=None):
            return client.post("/finance/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        initialized = call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        assert initialized.status_code == 200, initialized.text
        tools = call("tools/list").json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"about", "list_skills", "get_skill", "create_skill", "update_skill", "delete_skill", "db_schema", "db_query", "db_execute"}
        result = call("tools/call", {"name": "about", "arguments": {}}).json()["result"]
        assert "No enabled skills" in result["content"][0]["text"]
        assert not result.get("isError")
        resources = call("resources/list").json()["result"]["resources"]
        assert resources[0]["uri"] == "workspace://about"
        for claims, status in [({"aud": "wrong"}, 401), ({"iss": "https://wrong.example/"}, 401), ({"exp": 1}, 401), ({"sub": "other-user"}, 401)]:
            headers["Authorization"] = "Bearer " + signing(**claims)
            assert call("tools/list").status_code == status


async def test_malformed_token_rejected(signing):
    verifier = JWTVerifier("https://issuer.example/", "https://workspace.example/mcp", "https://issuer.example/jwks")
    assert await verifier.verify_token("not-a-token") is None


def test_local_mode_requires_loopback():
    with pytest.raises(ValueError):
        Settings("https://public.example", local_no_auth=True)
    with pytest.raises(ValueError):
        Settings("http://127.0.0.1:8000")


def test_live_skill_directory(tmp_path):
    from app.database import SkillRegistry
    registry = SkillRegistry(str(tmp_path / "workspace.sqlite3"))
    registry.initialize()
    registry.initialize()
    with registry.connect() as connection:
        connection.execute("INSERT INTO system_skills (name, description, content) VALUES (?, ?, ?)", ("notes", "Manage notes", "# Notes"))
    assert registry.list_skills()[0]["name"] == "notes"
    assert "notes: Manage notes" in registry.about()
    with registry.connect() as connection:
        connection.execute("UPDATE system_skills SET enabled = 0 WHERE name = ?", ("notes",))
    assert registry.list_skills() == []


def test_project_isolation_and_owner(tmp_path, signing):
    from app.database import SkillRegistry
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        finance = SkillRegistry(str(tmp_path / "finance.sqlite3"))
        finance.initialize()
        with finance.connect() as connection:
            connection.execute("INSERT INTO system_skills (name, description, content) VALUES ('finance-only', 'Private finance skill', '# Finance')")
        headers = {"Authorization": "Bearer " + signing(), "Accept": "application/json, text/event-stream"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_skills", "arguments": {}}}
        response = client.post("/finance/mcp", headers=headers, json=body)
        assert response.status_code == 200
        assert "finance-only" in response.text
        assert client.post("/projects/mcp", headers={**headers, "Authorization": "Bearer " + signing(sub="other-user", scope="skills:read projects:manage")}, json=body).status_code == 401
        headers["Authorization"] = "Bearer " + signing()
        response = client.post("/projects/mcp", headers=headers, json=body)
        assert response.status_code == 200
        assert "finance-only" not in response.text
        assert client.post("/finance/mcp", headers={**headers, "Authorization": "Bearer " + signing(sub="other-user")}, json=body).status_code == 401
        for project in ("finance", "projects"):
            challenge = client.post(f"/{project}/mcp", json=body)
            metadata_url = f"{settings.public_url}/.well-known/oauth-protected-resource/{project}/mcp"
            assert metadata_url in challenge.headers["www-authenticate"]
            metadata = client.get(metadata_url).json()
            assert metadata["resource"] == f"{settings.public_url}/{project}/mcp"
            assert "scopes_supported" not in metadata
        assert client.post("/unknown/mcp", json=body).status_code == 401
        assert client.post("/mcp", json=body).status_code == 400
        assert not (tmp_path / "unknown.sqlite3").exists()


@pytest.mark.parametrize("project", ["../finance", "Finance", "/tmp/finance", "finance.sql", "health", "", "a" * 64])
def test_invalid_project_names(project):
    with pytest.raises(ValueError):
        Settings("http://127.0.0.1:8000", local_no_auth=True).project_path(project)


def test_database_symlink_escape(tmp_path):
    (tmp_path / "finance.sqlite3").symlink_to(tmp_path.parent / "outside.sqlite3")
    with pytest.raises(ValueError):
        Settings("http://127.0.0.1:8000", data_dir=str(tmp_path), local_no_auth=True).project_path("finance")


def test_automatic_provisioning(tmp_path, signing):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    headers = {"Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        assert client.get("/health").status_code == 200
        assert list(tmp_path.iterdir()) == []
        assert client.get("/.well-known/oauth-protected-resource/new-project/mcp").status_code == 200
        assert client.post("/new-project/mcp", headers=headers, json=body).status_code == 401
        headers["Authorization"] = "Bearer " + signing(sub="other-user")
        assert client.post("/new-project/mcp", headers=headers, json=body).status_code == 401
        headers["Authorization"] = "Bearer " + signing(sub="other-user", scope="skills:read projects:manage")
        assert client.post("/new-project/mcp", headers=headers, json=body).status_code == 401
        assert list(tmp_path.iterdir()) == []
        headers["Authorization"] = "Bearer " + signing()
        response = client.post("/new-project/mcp", headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert (tmp_path / "new-project.sqlite3").is_file()
        from app.database import SkillRegistry
        registry = SkillRegistry(str(tmp_path / "new-project.sqlite3"))
        with registry.connect() as connection:
            connection.execute("INSERT INTO system_skills (name, description, content) VALUES ('persist', 'keep', '# Keep')")
        assert client.post("/new-project/mcp", headers=headers, json=body).status_code == 200
        assert registry.list_skills()[0]["name"] == "persist"
        assert client.post("/second/mcp", headers=headers, json=body).status_code == 200
        assert (tmp_path / "second.sqlite3").is_file()


def test_remote_owner_configuration_required():
    kwargs = dict(public_url="https://workspace.example", issuer="https://issuer.example/", jwks_url="https://issuer.example/jwks", audience="workspace")
    for owner in ("", " user", "user "):
        with pytest.raises(ValueError, match="OWNER_SUBJECT"):
            Settings(**kwargs, owner_subject=owner)


def test_forged_owner_and_invalid_tokens_do_not_provision(tmp_path, signing):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    claims = {"iss": settings.issuer, "aud": settings.audience, "sub": "user", "exp": int(time.time()) + 300}
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, attacker_key, algorithm="RS256")
    tokens = [forged, signing(sub="user", exp=1), signing(sub="user", aud="other"), signing(sub="user", iss="https://other.example/"), signing(sub="other-user", scope="skills:read projects:manage")]
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        for token in tokens:
            response = client.post("/private/mcp", headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 401
            assert not (tmp_path / "private.sqlite3").exists()


def test_query_project_connections(tmp_path, signing):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    headers = {"Authorization": "Bearer " + signing(), "Accept": "application/json, text/event-stream"}
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        denied = client.post("/mcp?project=finance", json=body)
        assert denied.status_code == 401
        assert "https://workspace.example/.well-known/oauth-protected-resource/mcp" in denied.headers["www-authenticate"]
        assert list(tmp_path.iterdir()) == []
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == "https://workspace.example/mcp"
        assert client.post("/mcp?project=finance", headers={**headers, "Authorization": "Bearer " + signing(sub="other")}, json=body).status_code == 401
        assert list(tmp_path.iterdir()) == []
        for project in ("finance", "notes"):
            url = f"/mcp?project={project}"
            assert client.post(url, headers=headers, json=body).status_code == 200
            assert (tmp_path / f"{project}.sqlite3").is_file()
            listed = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
            assert {tool["name"] for tool in listed["result"]["tools"]} == {"about", "list_skills", "get_skill", "create_skill", "update_skill", "delete_skill", "db_schema", "db_query", "db_execute"}
            result = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "about", "arguments": {}}}).json()["result"]
            assert f"Project: {project}" in result["content"][0]["text"]
        from app.database import SkillRegistry
        finance = SkillRegistry(str(tmp_path / "finance.sqlite3"))
        with finance.connect() as connection:
            connection.execute("INSERT INTO system_skills (name, description, content) VALUES ('finance-only', 'Private', '# Private')")
        query = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_skills", "arguments": {}}}
        assert "finance-only" in client.post("/mcp?project=finance", headers=headers, json=query).text
        assert "finance-only" in client.post("/finance/mcp", headers=headers, json=query).text
        assert "finance-only" not in client.post("/mcp?project=notes", headers=headers, json=query).text


@pytest.mark.parametrize("url", ["/mcp", "/mcp?project=", "/mcp?project=../bad", "/mcp?project=%2Ftmp%2Fbad", "/mcp?project=finance&project=notes", "/mcp?project=finance&extra=notes", "/finance/mcp?project=notes"])
def test_invalid_query_selection_no_provision(tmp_path, signing, url):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        assert client.post(url, headers={"Authorization": "Bearer " + signing()}, json={}).status_code == 400
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("claims,reason", [
    ({"aud": "wrong"}, "audience does not match OAUTH_AUDIENCE"),
    ({"iss": "https://wrong.example/"}, "issuer does not match OAUTH_ISSUER"),
    ({"exp": 1}, "token expired"),
    ({"sub": "other-private-user"}, "signed-in user does not match OWNER_SUBJECT"),
])
def test_safe_auth_diagnostics(tmp_path, signing, caplog, claims, reason):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    token = signing(**claims)
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        response = client.post("/mcp?project=finance", headers={"Authorization": "Bearer " + token}, json={})
        assert response.status_code == 401
    assert reason in caplog.text
    assert token not in caplog.text
    assert "other-private-user" not in caplog.text
    assert list(tmp_path.iterdir()) == []


def test_missing_bearer_diagnostic(tmp_path, caplog):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        assert client.post("/mcp?project=finance", json={}).status_code == 401
    assert "request has no Bearer authorization header" in caplog.text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("enabled", [False, True])
def test_owner_user_id_diagnostic_opt_in(tmp_path, signing, caplog, enabled):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="configured-owner", auth_debug_user_ids=enabled)
    token = signing(sub="actual-signed-in-user")
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        response = client.post("/mcp?project=finance", headers={"Authorization": "Bearer " + token}, json={})
        assert response.status_code == 401
    assert ("signed_in_user_id='actual-signed-in-user'" in caplog.text) is enabled
    assert ("configured_owner_id='configured-owner'" in caplog.text) is enabled
    assert token not in caplog.text
    assert list(tmp_path.iterdir()) == []


def test_invalid_token_never_logs_user_id(tmp_path, signing, caplog):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="configured-owner", auth_debug_user_ids=True)
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        assert client.post("/mcp?project=finance", headers={"Authorization": "Bearer " + signing(sub="unverified-user", aud="wrong")}, json={}).status_code == 401
    assert "signed_in_user_id" not in caplog.text
    assert "unverified-user" not in caplog.text


@pytest.mark.parametrize("owner,subject,allowed", [
    ("12345", "google-oauth2|12345", True),
    ("12345", "auth0|12345", True),
    ("12345", "12345", True),
    ("12345", "google-oauth2|67890", False),
    ("12345", "google-oauth2|012345", False),
    ("12345", "google-oauth2|prefix|12345", False),
    ("12345", "|12345", False),
    ("google-oauth2|12345", "google-oauth2|12345", True),
    ("google-oauth2|12345", "auth0|12345", False),
    ("google-oauth2|12345", "12345", False),
])
def test_owner_id_matching(tmp_path, signing, owner, subject, allowed):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject=owner)
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        response = client.post("/mcp?project=finance", headers={"Authorization": "Bearer " + signing(sub=subject), "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == (200 if allowed else 401)
        assert (tmp_path / "finance.sqlite3").exists() is allowed


def test_mcp_skill_lifecycle_and_isolation(tmp_path, signing):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    headers = {"Authorization": "Bearer " + signing(), "Accept": "application/json, text/event-stream"}
    markdown = "# Transactions\n\nAsk for missing amounts. Never invent fees.\n"
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        def call(name, arguments, project="finance", auth=headers):
            response = client.post(f"/mcp?project={project}", headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            return result
        created = call("create_skill", {"name": "finance.transactions", "description": "Record expenses", "content": markdown})
        assert not created.get("isError")
        assert created["structuredContent"]["version"] == 1
        assert created["structuredContent"]["enabled"] is True
        fetched = call("get_skill", {"name": "finance.transactions"})
        assert fetched["structuredContent"]["content"] == markdown
        assert json.loads(fetched["content"][0]["text"])["content"] == markdown
        assert "finance.transactions" in call("about", {})["content"][0]["text"]
        assert "finance.transactions" not in str(call("list_skills", {}, project="other"))
        assert call("get_skill", {"name": "finance.transactions"}, project="other")["isError"]
        assert call("create_skill", {"name": "finance.transactions", "description": "Duplicate", "content": "Overwrite"})["isError"]
        updated = call("update_skill", {"name": "finance.transactions", "expected_version": 1, "enabled": False})
        assert updated["structuredContent"]["version"] == 2
        assert updated["structuredContent"]["enabled"] is False
        assert "finance.transactions" not in str(call("list_skills", {}))
        assert "finance.transactions" in str(call("list_skills", {"include_disabled": True}))
        assert "finance.transactions" not in call("about", {})["content"][0]["text"]
        fetched = call("get_skill", {"name": "finance.transactions"})
        assert fetched["structuredContent"]["content"] == markdown
        assert json.loads(fetched["content"][0]["text"])["content"] == markdown
        assert call("update_skill", {"name": "finance.transactions", "expected_version": 1, "content": "Stale"})["isError"]
        assert call("delete_skill", {"name": "finance.transactions", "expected_version": 1})["isError"]
        assert call("update_skill", {"name": "finance.transactions", "expected_version": 2, "description": "Updated", "enabled": True})["structuredContent"]["version"] == 3
        response = client.post("/mcp?project=finance", headers={**headers, "Authorization": "Bearer " + signing(sub="other-user")}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "delete_skill", "arguments": {"name": "finance.transactions", "expected_version": 3}}})
        assert response.status_code == 401
        assert call("get_skill", {"name": "finance.transactions"})["structuredContent"]["version"] == 3
        assert call("delete_skill", {"name": "finance.transactions", "expected_version": 3})["structuredContent"] == {"name": "finance.transactions", "deleted": True}
        assert call("get_skill", {"name": "finance.transactions"})["isError"]
        tools = client.post("/mcp?project=finance", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        assert by_name["get_skill"]["annotations"]["readOnlyHint"] is True
        assert by_name["delete_skill"]["annotations"]["destructiveHint"] is True
        assert by_name["create_skill"]["annotations"]["readOnlyHint"] is False
        for name in ("get_skill", "create_skill", "update_skill"):
            schema = by_name[name]["outputSchema"]
            assert schema["properties"]["content"]["type"] == "string"
            assert "content" in schema["required"]
        assert by_name["delete_skill"]["outputSchema"]["properties"]["deleted"]["type"] == "boolean"


def test_mcp_data_tools_owner_isolation_and_structured_audit(tmp_path, signing):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", owner_subject="user")
    headers = {"Authorization": "Bearer " + signing(), "Accept": "application/json, text/event-stream"}
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        def call(tool, arguments, project="finance"):
            response = client.post(f"/mcp?project={project}", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
            assert response.status_code == 200
            return response.json()["result"], response.headers["x-request-id"]
        schema, _ = call("db_schema", {})
        assert {table["name"] for table in schema["structuredContent"]["tables"]} == {"data_records", "audit_mutations"}
        args = {"statements": [{"sql": "INSERT INTO data_records(collection, id, body) VALUES (?, ?, ?)", "params": ["expenses", "expense-one", '{"amount_minor":120000,"currency":"PHP"}']}], "idempotency_key": "expense-one"}
        written, trace = call("db_execute", args)
        assert not written.get("isError")
        assert written["structuredContent"]["results"][0]["changed_rows"] == 1
        replay, _ = call("db_execute", args)
        assert replay["structuredContent"]["replayed"] is True
        saved, _ = call("db_query", {"sql": "SELECT id, body FROM data_records WHERE collection = ?", "params": ["expenses"]})
        assert saved["structuredContent"]["rows"][0]["id"] == "expense-one"
        other, _ = call("db_query", {"sql": "SELECT * FROM data_records"}, "other")
        assert other["structuredContent"]["rows"] == []
        audit, _ = call("db_query", {"sql": "SELECT actor, request_id, changed_rows FROM audit_mutations"})
        assert audit["structuredContent"]["rows"] == [{"actor": "user", "request_id": trace, "changed_rows": 1}]
        blocked, _ = call("db_query", {"sql": "SELECT * FROM system_skills"})
        assert blocked["isError"]
        denied = client.post("/mcp?project=denied", headers={**headers, "Authorization": "Bearer " + signing(sub="other-user")}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "db_execute", "arguments": args}})
        assert denied.status_code == 401
        assert not (tmp_path / "denied.sqlite3").exists()

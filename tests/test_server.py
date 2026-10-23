import time
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
        claims = {"iss": "https://issuer.example/", "aud": "https://workspace.example/mcp", "sub": "user", "exp": int(time.time()) + 300, "scope": "skills:read project:finance"}
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256")
    return token


def test_auth_and_mcp(tmp_path, signing):
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp")
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
        assert {t["name"] for t in tools} == {"about", "list_skills"}
        result = call("tools/call", {"name": "about", "arguments": {}}).json()["result"]
        assert "No enabled skills" in result["content"][0]["text"]
        assert not result.get("isError")
        resources = call("resources/list").json()["result"]["resources"]
        assert resources[0]["uri"] == "workspace://about"
        for claims, status in [({"aud": "wrong"}, 401), ({"iss": "https://wrong.example/"}, 401), ({"exp": 1}, 401), ({"scope": "data:read"}, 403)]:
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


def test_project_isolation_and_scopes(tmp_path, signing):
    from app.database import SkillRegistry
    settings = Settings("https://workspace.example", str(tmp_path), "https://issuer.example/", "https://issuer.example/jwks", "https://workspace.example/mcp", projects={"finance": "finance.sqlite3", "projects": "projects.sqlite3"})
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        finance = SkillRegistry(str(tmp_path / "finance.sqlite3"))
        with finance.connect() as connection:
            connection.execute("INSERT INTO system_skills (name, description, content) VALUES ('finance-only', 'Private finance skill', '# Finance')")
        headers = {"Authorization": "Bearer " + signing(), "Accept": "application/json, text/event-stream"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_skills", "arguments": {}}}
        response = client.post("/finance/mcp", headers=headers, json=body)
        assert response.status_code == 200
        assert "finance-only" in response.text
        assert client.post("/projects/mcp", headers=headers, json=body).status_code == 403
        headers["Authorization"] = "Bearer " + signing(scope="skills:read project:projects")
        response = client.post("/projects/mcp", headers=headers, json=body)
        assert response.status_code == 200
        assert "finance-only" not in response.text
        assert client.post("/finance/mcp", headers=headers, json=body).status_code == 403
        for project in ("finance", "projects"):
            challenge = client.post(f"/{project}/mcp", json=body)
            metadata_url = f"{settings.public_url}/.well-known/oauth-protected-resource/{project}/mcp"
            assert metadata_url in challenge.headers["www-authenticate"]
            metadata = client.get(metadata_url).json()
            assert metadata["resource"] == f"{settings.public_url}/{project}/mcp"
            assert f"project:{project}" in metadata["scopes_supported"]
        assert client.post("/unknown/mcp", json=body).status_code == 404
        assert client.post("/mcp", json=body).status_code == 404
        assert not (tmp_path / "unknown.sqlite3").exists()


@pytest.mark.parametrize("projects", [
    {"../finance": "finance.sqlite3"}, {"finance": "../finance.sqlite3"},
    {"finance": "/tmp/finance.sqlite3"}, {"finance": "finance.sql"},
    {"finance": "shared.sqlite3", "projects": "shared.sqlite3"}, {},
])
def test_invalid_project_registry(projects):
    with pytest.raises(ValueError):
        Settings("http://127.0.0.1:8000", local_no_auth=True, projects=projects)


def test_database_symlink_escape(tmp_path):
    (tmp_path / "finance.sqlite3").symlink_to(tmp_path.parent / "outside.sqlite3")
    with pytest.raises(ValueError):
        Settings("http://127.0.0.1:8000", data_dir=str(tmp_path), local_no_auth=True)

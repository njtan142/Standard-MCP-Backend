import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_verbose_trace_correlation_and_payload_redaction(tmp_path, caplog):
    settings = Settings("http://127.0.0.1:8000", data_dir=str(tmp_path), local_no_auth=True, log_level="DEBUG")
    private_content = "PRIVATE_MARKDOWN_BODY_DO_NOT_LOG"
    private_description = "PRIVATE_DESCRIPTION_DO_NOT_LOG"
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        response = client.post("/mcp?project=finance", headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer PRIVATE_ACCESS_TOKEN_DO_NOT_LOG", "Cookie": "PRIVATE_COOKIE_DO_NOT_LOG"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_skill", "arguments": {"name": "notes.capture", "description": private_description, "content": private_content}}})
        assert response.status_code == 200
        assert not response.json()["result"].get("isError")
        trace = response.headers["x-request-id"]
        records = [record for record in caplog.records if getattr(record, "request_id", None) == trace]
        text = "\n".join(record.getMessage() for record in records)
        assert "mcp.request.received" in text
        assert '"tool": "create_skill"' in text
        assert '"operation": "create_skill"' in text
        assert "database.transaction.committed" in text
        assert '"content_characters":' in text
        assert '"structured_content": true' in text
        assert '"duration_ms":' in text
        assert any(getattr(record, "project", None) == "finance" for record in records)
        for private in (private_content, private_description, "PRIVATE_ACCESS_TOKEN_DO_NOT_LOG", "PRIVATE_COOKIE_DO_NOT_LOG"):
            assert private not in caplog.text
        # Request-local IDs are reset, not reused across requests.
        next_response = client.get("/health")
        assert next_response.headers["x-request-id"] != trace


def test_tool_failure_in_trace_and_large_response_unchanged(tmp_path, caplog):
    settings = Settings("http://127.0.0.1:8000", data_dir=str(tmp_path), local_no_auth=True, log_level="DEBUG")
    markdown = "# Private skill\n" + "x" * 60000
    with TestClient(create_app(settings), base_url=settings.public_url) as client:
        def call(name, arguments):
            return client.post("/mcp?project=finance", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, headers={"Accept": "application/json, text/event-stream"})
        assert not call("create_skill", {"name": "notes", "description": "Notes", "content": markdown}).json()["result"].get("isError")
        fetched = call("get_skill", {"name": "notes"}).json()["result"]
        assert fetched["structuredContent"]["content"] == markdown
        assert json.loads(fetched["content"][0]["text"])["content"] == markdown
        failure = call("update_skill", {"name": "notes", "expected_version": 99, "content": "private edit"})
        assert failure.json()["result"]["isError"]
        assert "database.operation.failed" in caplog.text
        assert "database.transaction.rolled_back" in caplog.text
        assert '"tool_error": true' in caplog.text
        assert markdown not in caplog.text
        assert "private edit" not in caplog.text


def test_invalid_logging_level():
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings("http://127.0.0.1:8000", local_no_auth=True, log_level="EVERYTHING")

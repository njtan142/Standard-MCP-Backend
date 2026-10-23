"""Detailed application tracing without request arguments or response contents."""
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from functools import wraps

request_id = ContextVar("request_id", default="-")
project_name = ContextVar("project", default="-")
audit_actor = ContextVar("audit_actor", default="local-development")
logger = logging.getLogger(__name__)


class ContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        record.project = project_name.get()
        return True


def configure_logging(level):
    application = logging.getLogger("app")
    application.setLevel(level)
    if not any(getattr(handler, "_workspace_logging", False) for handler in application.handlers):
        handler = logging.StreamHandler()
        handler._workspace_logging = True
        handler.addFilter(ContextFilter())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s project=%(project)s %(message)s"
        ))
        application.addHandler(handler)


def event(name, level=logging.INFO, **fields):
    logger.log(level, "%s %s", name, json.dumps(fields, sort_keys=True))


def trace_operation(function):
    @wraps(function)
    def traced(*args, **kwargs):
        started = time.perf_counter()
        event("database.operation.start", logging.DEBUG, operation=function.__name__)
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            event("database.operation.failed", logging.WARNING, operation=function.__name__, error_type=type(exc).__name__, duration_ms=round((time.perf_counter()-started)*1000, 2))
            raise
        fields = {}
        if isinstance(result, list):
            fields["row_count"] = len(result)
        elif isinstance(result, dict):
            for key in ("version", "enabled", "deleted", "row_count", "truncated", "replayed"):
                if key in result:
                    fields[key] = result[key]
            if "content" in result:
                fields["content_characters"] = len(result["content"])
            if "tables" in result:
                fields["table_count"] = len(result["tables"])
            if "results" in result:
                fields["statement_count"] = len(result["results"])
                fields["changed_rows"] = sum(item["changed_rows"] for item in result["results"])
        event("database.operation.complete", operation=function.__name__, duration_ms=round((time.perf_counter()-started)*1000, 2), **fields)
        return result
    return traced


class RequestLoggingMiddleware:
    """Pure ASGI middleware; bounded observation never changes streamed messages."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        identifier = uuid.uuid4().hex
        request_token = request_id.set(identifier)
        project_token = project_name.set("-")
        actor_token = audit_actor.set("local-development")
        started = time.perf_counter()
        status = 500
        request_bytes = 0
        response_bytes = 0
        captured = bytearray()
        response_sample = bytearray()
        capture_limit = 65536
        response_limit = 4096
        method = "-"
        tool = "-"
        path = scope.get("path", "")
        safe_path = path if re.fullmatch(r"/[a-zA-Z0-9_./-]{0,200}", path) else "[other]"
        event("http.request.start", http_method=scope.get("method"), path=safe_path)

        def rpc_metadata():
            nonlocal method, tool
            if request_bytes > capture_limit:
                event("mcp.request.summary", logging.DEBUG, metadata="body_exceeds_capture_limit")
                return
            try:
                body = json.loads(captured)
            except (ValueError, UnicodeDecodeError):
                return
            if not isinstance(body, dict):
                return
            allowed_methods = {"initialize", "ping", "tools/list", "tools/call", "resources/list", "resources/read", "resources/templates/list", "prompts/list", "prompts/get", "notifications/initialized", "notifications/cancelled"}
            candidate_method = body.get("method")
            method = candidate_method if isinstance(candidate_method, str) and candidate_method in allowed_methods else "unknown"
            params = body.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if method == "tools/call":
                tool = name if isinstance(name, str) and name in {"about", "list_skills", "remember", "recall", "forget", "get_skill", "create_skill", "update_skill", "delete_skill", "db_schema", "db_query", "db_execute", "export_sql", "import_sql", "db_migrate"} else "unknown"
            event("mcp.request.received", rpc_method=method, tool=tool)

        async def observed_receive():
            nonlocal request_bytes
            message = await receive()
            if message["type"] == "http.request":
                data = message.get("body", b"")
                request_bytes += len(data)
                captured.extend(data[:max(0, capture_limit-len(captured))])
                if not message.get("more_body", False):
                    rpc_metadata()
            elif message["type"] == "http.disconnect":
                event("http.request.disconnected", logging.DEBUG)
            return message

        async def observed_send(message):
            nonlocal status, response_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", identifier.encode())]}
            elif message["type"] == "http.response.body":
                data = message.get("body", b"")
                response_bytes += len(data)
                response_sample.extend(data[:max(0, response_limit-len(response_sample))])
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except Exception as exc:
            event("http.request.failed", logging.ERROR, error_type=type(exc).__name__)
            raise
        finally:
            summary = {}
            if response_bytes <= response_limit:
                try:
                    body = json.loads(response_sample)
                    if isinstance(body, dict):
                        result = body.get("result")
                        summary["rpc_error"] = "error" in body
                        if isinstance(result, dict):
                            summary["tool_error"] = bool(result.get("isError", False))
                            summary["structured_content"] = "structuredContent" in result
                except (ValueError, UnicodeDecodeError):
                    pass
            event("http.request.complete", http_method=scope.get("method"), path=safe_path, status=status, rpc_method=method, tool=tool, request_bytes=request_bytes, response_bytes=response_bytes, duration_ms=round((time.perf_counter()-started)*1000, 2), **summary)
            request_id.reset(request_token)
            project_name.reset(project_token)
            audit_actor.reset(actor_token)

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    public_url: str
    data_dir: str = "data"
    issuer: str = ""
    jwks_url: str = ""
    audience: str = ""
    local_no_auth: bool = False
    projects: dict[str, str] = field(default_factory=lambda: {"finance": "finance.sqlite3"})

    def __post_init__(self):
        url = urlsplit(self.public_url)
        if url.path or url.query or url.fragment or url.username or not url.hostname:
            raise ValueError("PUBLIC_URL must be an origin without a path or trailing slash")
        if self.local_no_auth:
            if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("Unauthenticated mode requires a loopback HTTP PUBLIC_URL")
        elif url.scheme != "https":
            raise ValueError("PUBLIC_URL must use HTTPS")
        if not isinstance(self.projects, dict) or not self.projects:
            raise ValueError("PROJECTS_JSON must be a nonempty project-to-filename object")
        paths = set()
        for name, filename in self.projects.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) or name in {"health", "docs", "redoc", "openapi"}:
                raise ValueError("Invalid or reserved project name")
            if not isinstance(filename, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.sqlite3", filename):
                raise ValueError("Project database must be a plain .sqlite3 filename")
            path = (Path(self.data_dir) / filename).resolve()
            if path.parent != Path(self.data_dir).resolve() or path in paths:
                raise ValueError("Project databases must be distinct files inside DATA_DIR")
            paths.add(path)
        if not self.local_no_auth:
            for name, value in (("OAUTH_ISSUER", self.issuer), ("OAUTH_JWKS_URL", self.jwks_url)):
                if urlsplit(value).scheme != "https" or not urlsplit(value).hostname:
                    raise ValueError(f"{name} must be an HTTPS URL")
            if not self.audience:
                raise ValueError("OAUTH_AUDIENCE is required")

    @classmethod
    def from_env(cls):
        return cls(
            public_url=os.environ.get("PUBLIC_URL", "http://127.0.0.1:8000"),
            data_dir=os.environ.get("DATA_DIR", "data"),
            issuer=os.environ.get("OAUTH_ISSUER", ""),
            jwks_url=os.environ.get("OAUTH_JWKS_URL", ""),
            audience=os.environ.get("OAUTH_AUDIENCE", ""),
            local_no_auth=os.environ.get("LOCAL_NO_AUTH") == "1",
            projects=json.loads(os.environ.get("PROJECTS_JSON", '{"finance":"finance.sqlite3"}')),
        )

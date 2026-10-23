import os
import re
from dataclasses import dataclass
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
    owner_subject: str = ""
    auth_debug_user_ids: bool = False
    log_level: str = "INFO"

    def __post_init__(self):
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        url = urlsplit(self.public_url)
        if url.path or url.query or url.fragment or url.username or not url.hostname:
            raise ValueError("PUBLIC_URL must be an origin without a path or trailing slash")
        if self.local_no_auth:
            if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("Unauthenticated mode requires a loopback HTTP PUBLIC_URL")
        elif url.scheme != "https":
            raise ValueError("PUBLIC_URL must use HTTPS")
        if not self.local_no_auth:
            for name, value in (("OAUTH_ISSUER", self.issuer), ("OAUTH_JWKS_URL", self.jwks_url)):
                if urlsplit(value).scheme != "https" or not urlsplit(value).hostname:
                    raise ValueError(f"{name} must be an HTTPS URL")
            if not self.audience:
                raise ValueError("OAUTH_AUDIENCE is required")
            if not self.owner_subject or self.owner_subject != self.owner_subject.strip():
                raise ValueError("OWNER_SUBJECT must be a nonempty owner user ID without surrounding whitespace")

    @classmethod
    def from_env(cls):
        return cls(
            public_url=os.environ.get("PUBLIC_URL", "http://127.0.0.1:8000"),
            data_dir=os.environ.get("DATA_DIR", "data"),
            issuer=os.environ.get("OAUTH_ISSUER", ""),
            jwks_url=os.environ.get("OAUTH_JWKS_URL", ""),
            audience=os.environ.get("OAUTH_AUDIENCE", ""),
            local_no_auth=os.environ.get("LOCAL_NO_AUTH") == "1",
            owner_subject=os.environ.get("OWNER_SUBJECT", ""),
            auth_debug_user_ids=os.environ.get("AUTH_DEBUG_USER_IDS") == "1",
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )

    def project_path(self, project: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", project) or project in {"health", "docs", "redoc", "openapi"}:
            raise ValueError("Invalid or reserved project name")
        root = Path(self.data_dir).resolve()
        path = root / f"{project}.sqlite3"
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("Project database must be a regular file inside DATA_DIR")
        return path

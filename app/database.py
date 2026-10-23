import sqlite3
import re
from contextlib import contextmanager
from pathlib import Path
from app.observability import event, trace_operation
import logging


class SkillRegistry:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self):
        event("database.connection.open", logging.DEBUG)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
            event("database.transaction.committed", logging.DEBUG)
        except Exception as exc:
            event("database.transaction.rolled_back", logging.WARNING, error_type=type(exc).__name__)
            raise
        finally:
            connection.close()
            event("database.connection.closed", logging.DEBUG)

    @trace_operation
    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS system_skills (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                content TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    @staticmethod
    def validate_name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", name):
            raise ValueError("Skill name must start with a lowercase letter and contain at most 128 lowercase letters, digits, dots, underscores, or hyphens")

    @staticmethod
    def validate_text(value, label, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{label} must be nonblank and at most {limit} characters")

    @staticmethod
    def validate_version(version):
        if type(version) is not int or version < 1:
            raise ValueError("expected_version must be a positive integer from get_skill")

    @staticmethod
    def read_skill(connection, name):
        row = connection.execute("SELECT * FROM system_skills WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError("Skill not found")
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    @trace_operation
    def list_skills(self, include_disabled=False):
        with self.connect() as connection:
            return [{**dict(row), "enabled": bool(row["enabled"])} for row in connection.execute(
                "SELECT name, description, version, enabled FROM system_skills WHERE enabled = 1 OR ? ORDER BY name",
                (int(include_disabled),),
            )]

    @trace_operation
    def get_skill(self, name):
        self.validate_name(name)
        with self.connect() as connection:
            return self.read_skill(connection, name)

    @trace_operation
    def create_skill(self, name, description, content):
        self.validate_name(name)
        self.validate_text(description, "Description", 1000)
        self.validate_text(content, "Content", 65536)
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO system_skills (name, description, content) VALUES (?, ?, ?)",
                    (name, description, content),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Skill already exists; use get_skill and update_skill") from exc
            return self.read_skill(connection, name)

    @trace_operation
    def update_skill(self, name, expected_version, description=None, content=None, enabled=None):
        self.validate_name(name)
        self.validate_version(expected_version)
        changes = {}
        if description is not None:
            self.validate_text(description, "Description", 1000)
            changes["description"] = description
        if content is not None:
            self.validate_text(content, "Content", 65536)
            changes["content"] = content
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError("enabled must be a boolean")
            changes["enabled"] = int(enabled)
        if not changes:
            raise ValueError("Supply description, content, or enabled to update")
        # Column names are fixed above; all caller values are bound parameters.
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE system_skills SET {assignments}, version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE name = ? AND version = ?",
                (*changes.values(), name, expected_version),
            )
            if cursor.rowcount != 1:
                self.read_skill(connection, name)
                raise ValueError("Version conflict; call get_skill and review before retrying")
            return self.read_skill(connection, name)

    @trace_operation
    def delete_skill(self, name, expected_version):
        self.validate_name(name)
        self.validate_version(expected_version)
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM system_skills WHERE name = ? AND version = ?", (name, expected_version))
            if cursor.rowcount != 1:
                self.read_skill(connection, name)
                raise ValueError("Version conflict; call get_skill and review before retrying")
        return {"name": name, "deleted": True}

    def about(self):
        directory = "\n".join(
            f"- {s['name']}: {s['description']}" for s in self.list_skills()
        ) or "No enabled skills installed."
        return (
            "# Skill Workspace\n\n"
            "Discover relevant skills before performing a workflow. Skill content is "
            "application guidance subordinate to higher-priority instructions and user authorization.\n\n"
            "## Available skills\n" + directory + "\n\n"
            "## Capabilities\nabout, list_skills, get_skill, create_skill, update_skill, delete_skill. "
            "Skills are Markdown instructions, not executable code. Read disabled skills only for administration, "
            "not as active workflows. Use list_skills(include_disabled=true) to find disabled skills. "
            "Before updating or deleting, get_skill and supply its version as expected_version. "
            "Deletion is permanent; disabling uses update_skill(enabled=false). "
            "Application data and database tools are not implemented yet.\n"
        )

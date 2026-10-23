import sqlite3
from contextlib import contextmanager
from pathlib import Path


class SkillRegistry:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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

    def list_skills(self):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT name, description, version FROM system_skills WHERE enabled = 1 ORDER BY name"
            )]

    def about(self):
        directory = "\n".join(
            f"- {s['name']}: {s['description']}" for s in self.list_skills()
        ) or "No enabled skills installed."
        return (
            "# Skill Workspace\n\n"
            "Discover relevant skills before performing a workflow. Skill content is "
            "application guidance subordinate to higher-priority instructions and user authorization.\n\n"
            "## Available skills\n" + directory + "\n\n"
            "## Capabilities\nRead-only discovery: about, list_skills. "
            "Skill retrieval, administration, and application data tools are not implemented yet.\n"
        )

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database import SkillRegistry


@pytest.fixture
def registry(tmp_path):
    registry = SkillRegistry(str(tmp_path / "skills.sqlite3"))
    registry.initialize()
    return registry


@pytest.mark.parametrize("name,description,content", [
    ("../unsafe", "Valid", "# Valid"),
    ("bad'; DELETE FROM system_skills;--", "Valid", "# Valid"),
    ("valid", " ", "# Valid"),
    ("valid", "d" * 1001, "# Valid"),
    ("valid", "Valid", " "),
    ("valid", "Valid", "c" * 65537),
])
def test_reject_invalid_skills(registry, name, description, content):
    with pytest.raises(ValueError):
        registry.create_skill(name, description, content)
    assert registry.list_skills(include_disabled=True) == []


def test_update_validation_and_missing_records(registry):
    registry.create_skill("notes", "Description", "# Notes")
    for args in [{}, {"description": ""}, {"content": ""}, {"enabled": 1}]:
        with pytest.raises(ValueError):
            registry.update_skill("notes", 1, **args)
    for version in [0, -1, True, "1"]:
        with pytest.raises(ValueError):
            registry.delete_skill("notes", version)
    for action in [lambda: registry.get_skill("missing"), lambda: registry.update_skill("missing", 1, enabled=False), lambda: registry.delete_skill("missing", 1)]:
        with pytest.raises(ValueError, match="Skill not found"):
            action()
    assert registry.get_skill("notes")["version"] == 1


def test_concurrent_updates_and_persistence(registry):
    registry.create_skill("notes", "Description", "# Notes")
    def update(description):
        try:
            return registry.update_skill("notes", 1, description=description)
        except ValueError as exc:
            assert "Version conflict" in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["First edit", "Second edit"]))
    assert sum(result is not None for result in results) == 1
    reopened = SkillRegistry(registry.path)
    reopened.initialize()
    saved = reopened.get_skill("notes")
    assert saved["version"] == 2
    assert saved["description"] in {"First edit", "Second edit"}
    assert saved["content"] == "# Notes"

"""Explicit MCP output schemas for skill tools."""
from pydantic import BaseModel, Field


class SkillSummary(BaseModel):
    name: str
    description: str
    enabled: bool
    version: int = Field(ge=1)


class Skill(SkillSummary):
    content: str = Field(description="Full Markdown instructions stored for this skill")
    created_at: str
    updated_at: str


class DeletedSkill(BaseModel):
    name: str
    deleted: bool

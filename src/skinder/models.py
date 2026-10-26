from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Agent = Literal["codex", "claude-code"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(Model):
    agent: Agent = "codex"
    planning: Literal["balanced", "plan-first", "act-first"] = "balanced"
    communication: Literal["balanced", "concise", "detailed"] = "balanced"
    verification: Literal["balanced", "pragmatic", "thorough"] = "balanced"
    output_style: str = Field(default="", max_length=2000)


class Evidence(Model):
    trait: str
    excerpt: str


class InstallCommands(Model):
    codex: str | None = None
    claude_code: str | None = Field(default=None, alias="claude-code")


class Skill(Model):
    id: str
    name: str
    slug: str
    description: str
    category: str
    author: str
    repository: str
    source_url: str
    revision: str | None
    license: str | None
    # None means unknown support, so retrieval keeps the skill eligible for either agent.
    compatibility: list[Agent] | None
    # Empty imported metadata is missing evidence, not proof of no setup or style constraints.
    requirements: list[str]
    tags: list[str]
    workflow: list[str]
    output_style: list[str]
    evidence: list[Evidence]
    install_commands: InstallCommands
    provenance: Literal["bundled", "skillsmp"] = "bundled"
    provider_url: str | None = None
    content_language: str | None = None


class Catalog(Model):
    skills: list[Skill]
    categories: list[str]
    total: int = 0
    has_more: bool = False


class Recommendation(Model):
    skill_id: str
    task_fit: str = Field(min_length=1, max_length=2000)
    taste_fit: str = Field(min_length=1, max_length=2000)
    caveat: str | None


class MatchResult(Model):
    # Zero recommendations is a valid result when the available skills do not fit the task.
    recommendations: list[Recommendation] = Field(max_length=12)


class FeedbackInput(Model):
    session_id: str
    skill_id: str
    action: Literal["save", "pass"]
    reason: Literal["wrong-task", "wrong-style", "too-much-setup"] | None = None

    @model_validator(mode="after")
    def save_has_no_pass_reason(self):
        if self.action == "save" and self.reason is not None:
            raise ValueError("A save cannot have a pass reason.")
        return self


class Feedback(FeedbackInput):
    id: str
    created_at: str


class MatchInput(Model):
    task: str = Field(min_length=3, max_length=4000)
    agent: Agent
    session_id: str | None = None

    @field_validator("task", mode="before")
    @classmethod
    def strip_task(cls, value):
        # Normalize before length validation and continuation comparisons, without coercing types.
        return value.strip() if isinstance(value, str) else value


class MatchSession(Model):
    id: str
    task: str
    agent: Agent
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    recommendations: list[Recommendation]
    feedback: list[Feedback]
    error: str | None
    created_at: str
    updated_at: str


class CodexStatus(Model):
    installed: bool
    authenticated: bool
    message: str


class Health(Model):
    status: Literal["ok"] = "ok"
    catalog_count: int
    codex: CodexStatus
    indexed_count: int = 0
    retrieval_mode: Literal["hybrid", "keyword-only"] = "keyword-only"
    sync_status: dict | None = None


class SavedItem(Model):
    skill: Skill
    saved_at: str


class Saved(Model):
    items: list[SavedItem]


class FeedbackResult(Model):
    feedback: Feedback
    saved: Saved


class Error(Model):
    detail: str

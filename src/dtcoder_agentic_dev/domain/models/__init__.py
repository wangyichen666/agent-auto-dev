"""通用、可序列化领域记录；不依赖数据库和具体平台。"""

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, TypeVar


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
ACTIVE_STATUSES = set(RunStatus) - TERMINAL_STATUSES


class AttemptStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    WAITING = "WAITING"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class OperationStatus(str, Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class Issue:
    provider: str
    repository_id: str
    external_id: str
    number: int
    title: str
    body: str = ""
    author: str = ""
    url: str = ""
    state: str = "open"
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    requested_branch: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowRun:
    run_id: str
    workflow_name: str
    workflow_version: str
    repository_id: str
    issue_external_id: str
    issue_number: int
    status: RunStatus = RunStatus.QUEUED
    current_step: str = ""
    branch: str = ""
    base_branch: str = "main"
    workspace_path: str | None = None
    revision: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0
    updated_at: float = 0
    started_at: float | None = None
    finished_at: float | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    last_error: str | None = None


@dataclass
class Artifact:
    artifact_id: str
    run_id: str
    step_name: str
    kind: str
    relative_path: str
    sha256: str
    git_revision: str | None = None
    created_at: float = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepAttempt:
    attempt_id: str
    run_id: str
    step_name: str
    attempt_number: int
    status: AttemptStatus = AttemptStatus.RUNNING
    started_at: float = 0
    finished_at: float | None = None
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


@dataclass
class ExternalOperation:
    operation_id: str
    run_id: str
    operation_type: str
    idempotency_key: str
    status: OperationStatus = OperationStatus.PENDING
    external_id: str | None = None
    external_url: str | None = None
    request_snapshot: dict[str, Any] = field(default_factory=dict)
    response_snapshot: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0
    updated_at: float = 0


def to_dict(record: Any) -> dict[str, Any]:
    return asdict(record)


T = TypeVar("T")


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
    enum_type = {
        WorkflowRun: RunStatus,
        StepAttempt: AttemptStatus,
        ExternalOperation: OperationStatus,
    }.get(cls)
    if enum_type and "status" in values:
        values["status"] = enum_type(values["status"])
    if cls is StepAttempt:
        values["artifacts"] = [from_dict(Artifact, a) for a in values.get("artifacts", [])]
    return cls(**values)

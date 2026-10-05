"""事件名称是稳定的领域契约，payload 不包含认证数据。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    RUN_QUEUED = "RunQueued"
    RUN_STARTED = "RunStarted"
    STEP_STARTED = "StepStarted"
    STEP_SUCCEEDED = "StepSucceeded"
    STEP_FAILED = "StepFailed"
    REVIEW_BLOCKED = "ReviewBlocked"
    PIPELINE_STARTED = "PipelineStarted"
    PIPELINE_FINISHED = "PipelineFinished"
    PULL_REQUEST_CREATED = "PullRequestCreated"
    RUN_SUCCEEDED = "RunSucceeded"
    RUN_FAILED = "RunFailed"
    RUN_CANCELLED = "RunCancelled"


@dataclass
class DomainEvent:
    event_id: str
    type: EventType
    run_id: str
    created_at: float
    step: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

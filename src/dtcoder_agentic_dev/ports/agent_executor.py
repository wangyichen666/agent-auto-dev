from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol


class AgentExecutionStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


@dataclass(frozen=True)
class AgentExecutionRequest:
    run_id: str
    step_name: str
    attempt_number: int
    workspace: str
    prompt: str
    model: str | None = None
    timeout: float | None = None
    allow_no_repo: bool = False
    session_id: str | None = None
    cancel_requested: Callable[[], bool] | None = field(default=None, repr=False, compare=False)
    on_event: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class AgentExecutionResult:
    stdout: str
    stderr: str = ""
    returncode: int = 0
    duration_seconds: float = 0
    structured: dict[str, Any] = field(default_factory=dict)
    status: AgentExecutionStatus = AgentExecutionStatus.SUCCEEDED


class AgentExecutorPort(Protocol):
    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult: ...


class AsyncAgentExecutorPort(Protocol):
    async def execute_async(self, request: AgentExecutionRequest) -> AgentExecutionResult: ...

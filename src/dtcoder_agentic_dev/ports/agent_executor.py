from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AgentExecutionRequest:
    run_id: str
    step_name: str
    attempt_number: int
    workspace: str
    prompt: str


@dataclass(frozen=True)
class AgentExecutionResult:
    stdout: str
    stderr: str = ""
    returncode: int = 0
    duration_seconds: float = 0
    structured: dict[str, Any] = field(default_factory=dict)


class AgentExecutorPort(Protocol):
    def execute(self, request: AgentExecutionRequest) -> AgentExecutionResult: ...

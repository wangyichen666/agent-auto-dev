from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolRequest:
    workspace: str
    config: dict[str, Any]
    timeout: float


@dataclass(frozen=True)
class ToolResult:
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolExecutor(Protocol):
    def execute(self, request: ToolRequest) -> ToolResult: ...

"""声明式定义：数据与安全条件，不依赖 YAML、CLI 或具体执行器。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExecutionPolicy:
    human_agent_type: str = "auto"
    timeout: float = 1800
    retries: int = 0
    allow_skip: bool = False


@dataclass(frozen=True)
class OutputSpec:
    path: str
    kind: str = "file"
    parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobSpec:
    name: str
    stage: str
    type: str = "agent"
    agent: str = "default"
    tool_name: str | None = None
    prompt: str | None = None
    skill: str | None = None
    model: str | None = None
    inputs: dict[str, dict[str, str]] = field(default_factory=dict)
    outputs: dict[str, OutputSpec] = field(default_factory=dict)
    artifact_check: tuple[dict[str, Any], ...] = ()
    post_actions: tuple[dict[str, Any], ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)


@dataclass(frozen=True)
class LoopSpec:
    name: str
    jobs: tuple[str, ...]
    max_rounds: int
    producer: str
    parameter: str
    equals: Any
    on_exhausted: str = "fail"

    def satisfied(self, parameters: dict) -> bool:
        values = parameters.get(self.producer, {})
        actual = values.get(self.parameter)
        return (
            self.parameter in values and type(actual) is type(self.equals) and actual == self.equals
        )


@dataclass(frozen=True)
class YamlWorkflow:
    name: str
    version: str
    description: str
    context: dict
    stages: tuple[str, ...]
    jobs: tuple[JobSpec, ...]
    loops: tuple[LoopSpec, ...]
    metadata: dict

    @property
    def ordered_jobs(self) -> tuple[JobSpec, ...]:
        return tuple(job for stage in self.stages for job in self.jobs if job.stage == stage)

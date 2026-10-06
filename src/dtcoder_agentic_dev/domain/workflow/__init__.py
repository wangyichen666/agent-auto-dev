"""显式工作流节点图、步骤请求与结果。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from dtcoder_agentic_dev.domain.errors import ConfigurationError
from dtcoder_agentic_dev.domain.models import Artifact, Issue, WorkflowRun
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutorPort
from dtcoder_agentic_dev.ports.artifact_store import ArtifactStore
from dtcoder_agentic_dev.ports.clock import Clock
from dtcoder_agentic_dev.ports.execution_journal import ExecutionJournal
from dtcoder_agentic_dev.ports.prompt_builder import PromptBuilder


class OutcomeType(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    WAITING = "WAITING"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class StepRequest:
    run: WorkflowRun
    issue: Issue
    repository: Any
    workspace: str
    prior_outputs: Mapping[str, Any]
    attempt_number: int
    input_snapshot: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepServices:
    agent_executor: AgentExecutorPort
    artifact_store: ArtifactStore
    clock: Clock
    execution_journal: ExecutionJournal | None = None
    prompt_builder: PromptBuilder | None = None


@dataclass(frozen=True)
class OutcomeError:
    type: str
    message: str
    code: str | None = None


@dataclass
class StepOutcome:
    type: OutcomeType
    artifacts: list[Artifact] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    external_refs: dict[str, Any] = field(default_factory=dict)
    error: OutcomeError | None = None
    next_poll_at: float | None = None


class WorkflowStep(Protocol):
    name: str

    def execute(self, request: StepRequest, services: StepServices) -> StepOutcome: ...


@dataclass(frozen=True)
class NodeDefinition:
    name: str
    step: str
    terminal: bool = False


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    on: OutcomeType = OutcomeType.SUCCEEDED
    condition: Callable[[Mapping[str, Any]], bool] | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: str
    start: str
    nodes: Mapping[str, NodeDefinition]
    transitions: list[Transition]

    def validate(self) -> None:
        if not self.name or not self.version or self.start not in self.nodes:
            raise ConfigurationError("工作流名称、版本或起始节点无效")
        names = [n.name for n in self.nodes.values()]
        if len(set(names)) != len(names) or any(k != n.name for k, n in self.nodes.items()):
            raise ConfigurationError("工作流节点名称重复或键名不一致")
        seen = set()
        unconditional = set()
        for t in self.transitions:
            if t.source not in self.nodes or t.target not in self.nodes:
                raise ConfigurationError("工作流迁移引用不存在的节点")
            if t.condition is not None and not callable(t.condition):
                raise ConfigurationError("迁移条件必须是 Python callable")
            key = (t.source, t.target, t.on, id(t.condition))
            if key in seen:
                raise ConfigurationError("重复的工作流迁移")
            seen.add(key)
            if t.condition is None:
                source_key = (t.source, t.on)
                if source_key in unconditional:
                    raise ConfigurationError("同一节点结果存在多个无条件迁移")
                unconditional.add(source_key)
            if t.on in {
                OutcomeType.WAITING,
                OutcomeType.FAILED,
                OutcomeType.PAUSED,
                OutcomeType.CANCELLED,
            }:
                raise ConfigurationError("WAITING、FAILED 和 PAUSED 由引擎处理，不能定义迁移")
            if self.nodes[t.source].terminal:
                raise ConfigurationError("终止节点不能有出边")
        reachable = {self.start}
        while True:
            expanded = reachable | {t.target for t in self.transitions if t.source in reachable}
            if expanded == reachable:
                break
            reachable = expanded
        if reachable != set(self.nodes):
            raise ConfigurationError("工作流存在不可达节点")
        terminable = {n.name for n in self.nodes.values() if n.terminal}
        while True:
            expanded = terminable | {t.source for t in self.transitions if t.target in terminable}
            if expanded == terminable:
                break
            terminable = expanded
        if set(self.nodes) - terminable:
            raise ConfigurationError("工作流存在没有终止路径的节点")

    def next_node(self, current: str, outcome: OutcomeType, facts: Mapping[str, Any]) -> str | None:
        matches = [
            t.target
            for t in self.transitions
            if t.source == current
            and t.on == outcome
            and (t.condition is None or t.condition(facts))
        ]
        if len(matches) > 1:
            raise ConfigurationError(f"节点 {current} 出现多个匹配迁移")
        if not matches:
            if self.nodes[current].terminal and outcome in {
                OutcomeType.SUCCEEDED,
                OutcomeType.SKIPPED,
            }:
                return None
            raise ConfigurationError(f"节点 {current} 没有 {outcome.value} 的迁移")
        return matches[0]

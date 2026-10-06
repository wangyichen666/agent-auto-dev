from dtcoder_agentic_dev.domain.errors import ConfigurationError
from dtcoder_agentic_dev.domain.workflow import WorkflowDefinition, WorkflowStep


class StepRegistry:
    def __init__(self):
        self._steps: dict[str, WorkflowStep] = {}

    def register(self, step: WorkflowStep) -> None:
        if step.name in self._steps:
            raise ConfigurationError(f"步骤名称重复：{step.name}")
        self._steps[step.name] = step

    def resolve(self, name: str) -> WorkflowStep:
        try:
            return self._steps[name]
        except KeyError as exc:
            raise ConfigurationError(f"未注册步骤：{name}") from exc

    def validate(self, workflow: WorkflowDefinition) -> None:
        workflow.validate()
        for node in workflow.nodes.values():
            self.resolve(node.step)


class ExecutorRegistry:
    """名称显式路由；未知引擎或工具不能静默回退。"""

    def __init__(self):
        self._agents = {}
        self._tools = {}

    def _register(self, registry, name, executor):
        if not name or name in registry:
            raise ConfigurationError(f"执行器名称为空或重复：{name}")
        registry[name] = executor

    def register_agent(self, name, executor):
        self._register(self._agents, name, executor)

    def register_tool(self, name, executor):
        self._register(self._tools, name, executor)

    def _resolve(self, registry, name, kind):
        if name not in registry:
            raise ConfigurationError(f"未知{kind}：{name}；可用：{', '.join(registry)}")
        return registry[name]

    def resolve_agent(self, name):
        return self._resolve(self._agents, name, " agent")

    def resolve_tool(self, name):
        return self._resolve(self._tools, name, " tool")

    @property
    def agent_names(self):
        return set(self._agents)

    @property
    def tool_names(self):
        return set(self._tools)

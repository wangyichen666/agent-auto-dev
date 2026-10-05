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

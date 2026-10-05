"""产品的默认研发节点图；核心引擎不知道这些节点的含义。"""

from dtcoder_agentic_dev.domain.workflow import (
    NodeDefinition,
    OutcomeType,
    Transition,
    WorkflowDefinition,
)


def pipeline_enabled(facts):
    return bool(facts.get("pipeline_enabled", False))


def pipeline_disabled(facts):
    return not pipeline_enabled(facts)


def issue_development_workflow():
    return WorkflowDefinition(
        "issue-development",
        "1",
        "requirements",
        {
            name: NodeDefinition(name, name, name == "create_pr")
            for name in ("requirements", "coding", "review", "fix", "pipeline", "create_pr")
        },
        [
            Transition("requirements", "coding"),
            Transition("coding", "review"),
            Transition("coding", "review", OutcomeType.SKIPPED),
            Transition("review", "fix", OutcomeType.BLOCKED),
            Transition("fix", "review"),
            Transition("review", "pipeline", condition=pipeline_enabled),
            Transition("review", "create_pr", condition=pipeline_disabled),
            Transition("pipeline", "create_pr"),
            Transition("pipeline", "create_pr", OutcomeType.SKIPPED),
        ],
    )

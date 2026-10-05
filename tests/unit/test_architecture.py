"""依赖方向以及第二产品独立接入的契约验证。"""

import ast
from dataclasses import replace
from pathlib import Path

from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.domain.models import RunStatus
from dtcoder_agentic_dev.domain.workflow import (
    NodeDefinition,
    OutcomeType,
    StepOutcome,
    StepServices,
    WorkflowDefinition,
)
from dtcoder_agentic_dev.engine.registry import StepRegistry
from dtcoder_agentic_dev.engine.runner import WorkflowRunner


def test_core_imports_only_domain_ports_engine():
    root = Path(__file__).parents[2] / "src/dtcoder_agentic_dev"
    for folder in ("domain", "engine"):
        for path in (root / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.startswith("dtcoder_agentic_dev.")
                ):
                    assert node.module.split(".")[1] in {"domain", "ports", "engine"}, path
                if isinstance(node, ast.Import):
                    assert all(
                        a.name not in {"sqlite3", "subprocess", "click"} for a in node.names
                    ), path


def test_second_product_without_core_modification(run, issue, clock, ids):
    class Publish:
        name = "publish-report"

        def execute(self, request, services):
            return StepOutcome(OutcomeType.SUCCEEDED, facts={"report_ready": True})

    graph = WorkflowDefinition(
        "documentation-product",
        "2",
        "publish",
        {"publish": NodeDefinition("publish", "publish-report", True)},
        [],
    )
    store = InMemoryRunRepository()
    new = replace(
        run,
        workflow_name=graph.name,
        workflow_version=graph.version,
        current_step="publish",
        workspace_path="/fixture",
    )
    store.create_run(new)
    store.claim_run("owner", clock.now(), 100)
    registry = StepRegistry()
    registry.register(Publish())
    engine = WorkflowRunner(
        store,
        registry,
        graph,
        StepServices(None, None, clock),
        EventDispatcher(store, clock, ids),
        ids,
        clock,
    )
    result = engine.run(run.run_id, issue, object(), "owner")
    assert result.status is RunStatus.SUCCEEDED and result.context["report_ready"]

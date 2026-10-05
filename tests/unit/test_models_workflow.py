import pytest

from dtcoder_agentic_dev.domain.errors import ConfigurationError
from dtcoder_agentic_dev.domain.models import (
    AttemptStatus,
    Issue,
    RunStatus,
    StepAttempt,
    WorkflowRun,
    from_dict,
    to_dict,
)
from dtcoder_agentic_dev.domain.workflow import (
    NodeDefinition,
    OutcomeType,
    Transition,
    WorkflowDefinition,
)
from dtcoder_agentic_dev.engine.registry import StepRegistry


@pytest.mark.parametrize(
    "model",
    [
        Issue("x", "r", "i", 1, "需求"),
        WorkflowRun("r", "w", "1", "repo", "i", 1),
        StepAttempt("a", "r", "s", 1),
    ],
)
def test_round_trip(model):
    assert from_dict(type(model), to_dict(model)) == model


def test_independent_defaults():
    a, b = Issue("x", "r", "i", 1, "a"), Issue("x", "r", "j", 2, "b")
    a.labels.append("a")
    assert b.labels == []
    assert WorkflowRun("r", "w", "1", "p", "i", 1).status is RunStatus.QUEUED
    assert StepAttempt("a", "r", "s", 1).status is AttemptStatus.RUNNING


def simple():
    return WorkflowDefinition(
        "w",
        "1",
        "a",
        {"a": NodeDefinition("a", "a"), "b": NodeDefinition("b", "b", True)},
        [Transition("a", "b")],
    )


def test_valid_graph():
    graph = simple()
    graph.validate()
    assert graph.next_node("a", OutcomeType.SUCCEEDED, {}) == "b"
    assert graph.next_node("b", OutcomeType.SUCCEEDED, {}) is None


@pytest.mark.parametrize(
    "change", ["start", "target", "unreachable", "loop", "duplicate", "condition"]
)
def test_invalid_graph(change):
    g = simple()
    nodes, transitions, start = dict(g.nodes), list(g.transitions), g.start
    if change == "start":
        start = "missing"
    if change == "target":
        transitions = [Transition("a", "missing")]
    if change == "unreachable":
        nodes["c"] = NodeDefinition("c", "c", True)
    if change == "loop":
        nodes["b"] = NodeDefinition("b", "b")
        transitions.append(Transition("b", "a"))
    if change == "duplicate":
        transitions *= 2
    if change == "condition":
        transitions = [Transition("a", "b", condition="evil")]
    with pytest.raises(ConfigurationError):
        WorkflowDefinition("w", "1", start, nodes, transitions).validate()


def test_registry_duplicates_and_missing():
    registry = StepRegistry()
    with pytest.raises(ConfigurationError):
        registry.resolve("missing")

    class Step:
        name = "a"

    registry.register(Step())
    with pytest.raises(ConfigurationError):
        registry.register(Step())


def test_unhashable_callable_condition_is_valid():
    class Condition:
        __hash__ = None

        def __call__(self, facts):
            return True

    graph = WorkflowDefinition(
        "w",
        "1",
        "a",
        {"a": NodeDefinition("a", "a"), "b": NodeDefinition("b", "b", True)},
        [Transition("a", "b", condition=Condition())],
    )
    graph.validate()
    assert graph.next_node("a", OutcomeType.SUCCEEDED, {}) == "b"

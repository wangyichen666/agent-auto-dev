from dataclasses import replace

import pytest

from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.application.workflows import issue_development_workflow
from dtcoder_agentic_dev.config import RepoConfig
from dtcoder_agentic_dev.domain.errors import BusinessError, FatalError, TechnicalError
from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import AttemptStatus, RunStatus
from dtcoder_agentic_dev.domain.workflow import (
    NodeDefinition,
    OutcomeType,
    StepOutcome,
    StepServices,
    WorkflowDefinition,
)
from dtcoder_agentic_dev.engine.registry import StepRegistry
from dtcoder_agentic_dev.engine.retry import RetryPolicy
from dtcoder_agentic_dev.engine.runner import WorkflowRunner


class ScriptStep:
    def __init__(self, name, script):
        self.name, self.script, self.calls = name, list(script), 0

    def execute(self, request, services):
        self.calls += 1
        value = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(request, services)
        return value


def setup(run, issue, clock, ids, steps, workflow=None, max_nodes=20, handlers=()):
    store = InMemoryRunRepository()
    store.create_run(replace(run, workspace_path="/fixture"))
    store.save_issue(issue)
    registry = StepRegistry()
    for step in steps:
        registry.register(step)
    graph = workflow or issue_development_workflow()
    events = EventDispatcher(store, clock, ids, handlers)
    engine = WorkflowRunner(
        store,
        registry,
        graph,
        StepServices(None, None, clock),
        events,
        ids,
        clock,
        RetryPolicy(2, 1),
        max_nodes,
    )

    def execute():
        claimed = store.claim_run("worker", clock.now(), 1000, run.run_id)
        if not claimed:
            return None
        try:
            return engine.run(run.run_id, issue, RepoConfig("r", "local"), "worker")
        finally:
            store.release_lease(run.run_id, "worker")

    return store, engine, execute


def outcome(type=OutcomeType.SUCCEEDED, **kwargs):
    return StepOutcome(type, **kwargs)


def single_graph():
    return WorkflowDefinition(
        "issue-development",
        "1",
        "requirements",
        {"requirements": NodeDefinition("requirements", "requirements", True)},
        [],
    )


def default_steps(review=None):
    return [
        ScriptStep(name, [outcome()])
        for name in ("requirements", "coding", "fix", "pipeline", "create_pr")
    ] + [ScriptStep("review", review or [outcome()])]


def test_normal_path_and_pipeline_disabled(run, issue, clock, ids):
    steps = default_steps()
    store, _, execute = setup(run, issue, clock, ids, steps)
    assert execute().status is RunStatus.SUCCEEDED
    names = [a.step_name for a in store.list_attempts(run.run_id)]
    assert names == ["requirements", "coding", "review", "create_pr"]
    assert EventType.RUN_SUCCEEDED in [e.type for e in store.list_events(run.run_id)]


def test_business_branch_and_retry_not_counted(run, issue, clock, ids):
    steps = default_steps([BusinessError("Blocker"), outcome()])
    store, _, execute = setup(run, issue, clock, ids, steps)
    assert execute().status is RunStatus.SUCCEEDED
    assert [a.step_name for a in store.list_attempts(run.run_id)] == [
        "requirements",
        "coding",
        "review",
        "fix",
        "review",
        "create_pr",
    ]
    assert clock.sleeps == []
    assert store.list_attempts(run.run_id)[2].status is AttemptStatus.BLOCKED


def test_technical_retry_and_limit(run, issue, clock, ids):
    step = ScriptStep(
        "requirements", [TechnicalError("暂时失败"), TechnicalError("再次失败"), outcome()]
    )
    store, _, execute = setup(run, issue, clock, ids, [step], single_graph())
    assert execute().status is RunStatus.SUCCEEDED
    assert clock.sleeps == [1, 2]
    assert [a.status for a in store.list_attempts(run.run_id)] == [
        AttemptStatus.FAILED,
        AttemptStatus.FAILED,
        AttemptStatus.SUCCEEDED,
    ]
    assert RetryPolicy(3, 100).delay(100) == 30


def test_retry_exhausted(run, issue, clock, ids):
    store, _, execute = setup(
        run,
        issue,
        clock,
        ids,
        [ScriptStep("requirements", [TechnicalError("失败")])],
        single_graph(),
    )
    assert execute().status is RunStatus.FAILED
    assert len(store.list_attempts(run.run_id)) == 3
    assert clock.sleeps == [1, 2]


def test_fatal_not_retried(run, issue, clock, ids):
    store, _, execute = setup(
        run, issue, clock, ids, [ScriptStep("requirements", [FatalError("停止")])], single_graph()
    )
    assert execute().status is RunStatus.FAILED
    assert len(store.list_attempts(run.run_id)) == 1 and clock.sleeps == []


def test_waiting_due_time(run, issue, clock, ids):
    steps = default_steps()
    for s in steps:
        if s.name == "pipeline":
            s.script = [outcome(OutcomeType.WAITING, next_poll_at=1030), outcome()]
    run.context["pipeline_enabled"] = True
    store, _, execute = setup(run, issue, clock, ids, steps)
    assert execute().status is RunStatus.WAITING
    assert execute() is None
    clock.value = 1030
    assert execute().status is RunStatus.SUCCEEDED
    assert [a.step_name for a in store.list_attempts(run.run_id)].count("pipeline") == 2


def test_max_nodes_checkpoint_and_request_is_snapshot(run, issue, clock, ids):
    steps = default_steps()
    steps[0].script = [
        lambda request, services: setattr(request.run, "branch", "tampered") or outcome()
    ]
    store, _, execute = setup(run, issue, clock, ids, steps, max_nodes=1)
    assert execute().current_step == "coding"
    assert store.load_run(run.run_id).branch == run.branch
    while store.load_run(run.run_id).status is RunStatus.RUNNING:
        execute()
    assert store.load_run(run.run_id).status is RunStatus.SUCCEEDED


def test_event_handler_failure_is_warning(run, issue, clock, ids, caplog):
    def fail(event):
        raise RuntimeError("通知故障")

    store, _, execute = setup(run, issue, clock, ids, default_steps(), handlers=[fail])
    assert execute().status is RunStatus.SUCCEEDED
    assert any(e.payload.get("handler_failures") for e in store.list_events(run.run_id))
    assert "事件处理器失败" in caplog.text


def test_crash_running_attempt_is_audited(run, issue, clock, ids):
    from dtcoder_agentic_dev.domain.models import StepAttempt

    store, _, execute = setup(run, issue, clock, ids, default_steps())
    store.save_attempt(
        StepAttempt(
            "interrupted",
            run.run_id,
            "requirements",
            1,
            input_snapshot={"untracked_before": ["manual.txt"]},
        )
    )
    assert execute().status is RunStatus.SUCCEEDED
    attempts = store.list_attempts(run.run_id)
    assert attempts[0].error_type == "ProcessInterrupted"
    assert attempts[1].attempt_number == 2


def test_pause_and_cancel_during_atomic_step_preserve_control(run, issue, clock, ids):
    steps = default_steps()
    store, _, execute = setup(run, issue, clock, ids, steps)

    def pause(request, services):
        changed = store.load_run(request.run.run_id)
        changed.status = RunStatus.PAUSED
        store.update_run(changed)
        return outcome()

    steps[0].script = [pause]
    assert execute().status is RunStatus.PAUSED
    assert store.load_run(run.run_id).current_step == "coding"
    saved = store.load_run(run.run_id)
    saved.status = RunStatus.QUEUED
    store.update_run(saved)

    def cancel(request, services):
        changed = store.load_run(request.run.run_id)
        changed.status = RunStatus.CANCELLED
        store.update_run(changed)
        return outcome()

    next(s for s in steps if s.name == "coding").script = [cancel]
    assert execute().status is RunStatus.CANCELLED
    assert len(store.list_attempts(run.run_id)) == 2


def test_attempt_and_run_rollback_together(run, issue, clock, ids, mocker):
    store, _, execute = setup(run, issue, clock, ids, default_steps())
    original = store.update_run

    def fail_checkpoint(changed):
        if changed.current_step == "coding":
            raise RuntimeError("模拟 checkpoint 保存失败")
        return original(changed)

    mocker.patch.object(store, "update_run", side_effect=fail_checkpoint)
    with pytest.raises(RuntimeError):
        execute()
    assert store.load_run(run.run_id).current_step == "requirements"
    assert store.list_attempts(run.run_id)[0].status is AttemptStatus.RUNNING


def test_graceful_stop_finishes_only_current_atomic_step(run, issue, clock, ids):
    steps = default_steps()
    store, engine, execute = setup(run, issue, clock, ids, steps)
    stopped = False
    engine.stop_requested = lambda: stopped

    def stop_after_step(request, services):
        nonlocal stopped
        stopped = True
        return outcome()

    steps[0].script = [stop_after_step]
    result = execute()
    assert result.status is RunStatus.RUNNING and result.current_step == "coding"
    assert len(store.list_attempts(run.run_id)) == 1
    stopped = False
    assert execute().status is RunStatus.SUCCEEDED

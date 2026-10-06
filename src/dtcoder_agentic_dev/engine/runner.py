"""工作流内核仅依赖领域和端口，事务内先保存尝试及产物，再推进运行。"""

import logging
from copy import deepcopy
from dataclasses import asdict, replace
from types import MappingProxyType
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

from dtcoder_agentic_dev.domain.errors import (
    BusinessError,
    ConcurrencyConflict,
    FatalError,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import (
    TERMINAL_STATUSES,
    AttemptStatus,
    RunStatus,
    StepAttempt,
)
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.domain.workflow import (
    OutcomeError,
    OutcomeType,
    StepOutcome,
    StepRequest,
    StepServices,
    WorkflowDefinition,
)
from dtcoder_agentic_dev.engine.checkpoint import checkpoint_context
from dtcoder_agentic_dev.engine.registry import StepRegistry
from dtcoder_agentic_dev.engine.retry import RetryPolicy
from dtcoder_agentic_dev.ports.clock import IdGenerator, Sleeper
from dtcoder_agentic_dev.ports.event_dispatcher import EventDispatcherPort
from dtcoder_agentic_dev.ports.run_repository import RunRepository


class WorkflowRunner:
    def __init__(
        self,
        repository: RunRepository,
        registry: StepRegistry,
        workflow: WorkflowDefinition,
        services: StepServices,
        events: EventDispatcherPort,
        ids: IdGenerator,
        sleeper: Sleeper,
        retry_policy: RetryPolicy | None = None,
        max_nodes_per_tick: int = 20,
        head_reader: Callable[[str], str] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ):
        registry.validate(workflow)
        if max_nodes_per_tick <= 0:
            raise ValueError("单次调度节点数必须大于零")
        self.repository, self.registry, self.workflow = repository, registry, workflow
        self.services, self.events, self.ids, self.sleeper = services, events, ids, sleeper
        self.retry = retry_policy or RetryPolicy()
        self.max_nodes, self.head_reader = max_nodes_per_tick, head_reader
        self.stop_requested = stop_requested or (lambda: False)
        self.logger = logging.getLogger(__name__)

    def _event(self, type, run, step=None, payload=None):
        event = self.events.make(type, run.run_id, step, payload)
        self.repository.save_event(event)
        return event

    def _lease(self, run, owner):
        self.repository.assert_lease(run.run_id, owner, self.services.clock.now())

    def run(self, run_id, issue, repository_config, owner):
        run = self.repository.load_run(run_id)
        if run.status in TERMINAL_STATUSES or run.status is RunStatus.PAUSED:
            return run
        self._lease(run, owner)
        if (run.workflow_name, run.workflow_version) != (self.workflow.name, self.workflow.version):
            raise FatalError("运行的工作流版本与已装配版本不一致")
        if not run.workspace_path:
            raise FatalError("运行尚未准备隔离工作区")
        pending = []
        with self.repository.transaction():
            run = self.repository.load_run(run_id)
            self._lease(run, owner)
            if (
                run.status in TERMINAL_STATUSES
                or run.status is RunStatus.PAUSED
                or self.stop_requested()
            ):
                return run
            run.status = RunStatus.RUNNING
            run.context["next_poll_at"] = None
            if run.started_at is None:
                run.started_at = self.services.clock.now()
                pending.append(self._event(EventType.RUN_STARTED, run))
            run.updated_at = self.services.clock.now()
            # 崩溃留下的尝试单独记录，新的执行使用新的尝试序号。
            for attempt in self.repository.list_attempts(run_id):
                if attempt.status is AttemptStatus.RUNNING:
                    run.context.setdefault("recovery_inputs", {})[attempt.step_name] = deepcopy(
                        attempt.input_snapshot
                    )
                    attempt.status = AttemptStatus.FAILED
                    attempt.finished_at = self.services.clock.now()
                    attempt.error_type = "ProcessInterrupted"
                    attempt.error_code = "PROCESS_INTERRUPTED"
                    attempt.error_message = "先前 worker 中断，执行恢复尝试"
                    self.repository.save_attempt(attempt)
            self.repository.update_run(run)
        self.events.publish(pending)
        for _ in range(self.max_nodes):
            run = self.repository.load_run(run_id)
            if (
                run.status in TERMINAL_STATUSES
                or run.status is RunStatus.PAUSED
                or self.stop_requested()
            ):
                return run
            self._lease(run, owner)
            node = self.workflow.nodes.get(run.current_step)
            if node is None:
                raise FatalError("运行的 current_step 不存在")
            step = self.registry.resolve(node.step)
            attempts = self.repository.list_attempts(run_id)
            number = 1 + max(
                (a.attempt_number for a in attempts if a.step_name == node.name), default=0
            )
            request = StepRequest(
                deepcopy(run),
                deepcopy(issue),
                deepcopy(repository_config),
                run.workspace_path,
                MappingProxyType(deepcopy(run.context.get("outputs", {}))),
                number,
            )
            attempt = StepAttempt(
                str(uuid5(NAMESPACE_URL, f"attempt:{run_id}:{node.name}:{number}"))
                if getattr(step, "stable_identity", False)
                else self.ids.new(),
                run_id,
                node.name,
                number,
                started_at=self.services.clock.now(),
                input_snapshot={
                    "run": asdict(run),
                    "issue": asdict(issue),
                    "prior_outputs": dict(request.prior_outputs),
                },
            )
            with self.repository.transaction():
                self._lease(run, owner)
                current = self.repository.load_run(run_id)
                if (
                    current.status in TERMINAL_STATUSES
                    or current.status is RunStatus.PAUSED
                    or self.stop_requested()
                ):
                    return current
                self.repository.save_attempt(attempt)
                if hasattr(step, "stage"):
                    round_number = step.round(request)
                    stage_id = str(
                        uuid5(
                            NAMESPACE_URL,
                            f"stage:{run_id}:{step.stage}:{getattr(step.loop, 'name', '')}:"
                            f"{current.context.get('declarative_epoch', 0)}:{round_number}",
                        )
                    )
                    attempt.metadata.update(
                        stage_execution_id=stage_id, stage=step.stage, round=round_number
                    )
                    stages = current.context.setdefault("stage_executions", [])
                    stage = next((s for s in stages if s["execution_id"] == stage_id), None)
                    if stage is None:
                        stage = {
                            "execution_id": stage_id,
                            "stage": step.stage,
                            "round": round_number,
                            "started_at": self.services.clock.now(),
                        }
                        stages.append(stage)
                    stage["status"] = "RUNNING"
                    stage["finished_at"] = None
                    self.repository.update_run(current)
                    self.repository.save_attempt(attempt)
                event = self._event(
                    EventType.STEP_STARTED, run, node.name, {"attempt_number": number}
                )
            self.events.publish([event])
            technical = False
            try:
                # 可选快照钩子在持久化 RUNNING 后、外部执行前记录模板摘要。
                snapshot = getattr(step, "input_snapshot", None)
                if snapshot is not None:
                    attempt.input_snapshot.update(snapshot(request))
                    if getattr(step, "stable_identity", False):
                        attempt.input_snapshot = redact(attempt.input_snapshot)
                    with self.repository.transaction():
                        self._lease(run, owner)
                        self.repository.save_attempt(attempt)
                request = replace(
                    request, input_snapshot=MappingProxyType(deepcopy(attempt.input_snapshot))
                )
                self._lease(run, owner)
                boundary = self.repository.load_run(run_id)
                if self.stop_requested():
                    return boundary
                if boundary.status is RunStatus.CANCELLED:
                    outcome = StepOutcome(OutcomeType.CANCELLED)
                elif boundary.status is RunStatus.PAUSED:
                    outcome = StepOutcome(
                        OutcomeType.PAUSED,
                        facts={
                            "pause_point": boundary.context.get(
                                "pause_point", {"job": node.name, "reason": "safe_boundary"}
                            )
                        },
                    )
                else:
                    outcome = step.execute(request, self.services)
                if not isinstance(outcome, StepOutcome):
                    raise FatalError("步骤必须返回 StepOutcome")
            except TechnicalError as exc:
                technical = True
                outcome = StepOutcome(
                    OutcomeType.FAILED,
                    error=OutcomeError(type(exc).__name__, str(exc), exc.code),
                    facts={
                        "execution": getattr(
                            exc,
                            "execution_metadata",
                            {
                                "stdout": getattr(exc, "stdout", ""),
                                "stderr": getattr(exc, "stderr", ""),
                            },
                        )
                    }
                    if getattr(step, "stable_identity", False)
                    else {},
                )
            except BusinessError as exc:
                outcome = StepOutcome(
                    OutcomeType.FAILED
                    if getattr(step, "stable_identity", False)
                    else OutcomeType.BLOCKED,
                    error=OutcomeError(type(exc).__name__, str(exc), exc.code),
                    facts={"execution": getattr(exc, "execution_metadata", {})}
                    if getattr(step, "stable_identity", False)
                    else {},
                )
            except ConcurrencyConflict:
                raise
            except Exception as exc:
                outcome = StepOutcome(
                    OutcomeType.FAILED,
                    error=OutcomeError(
                        type(exc).__name__, str(exc), getattr(exc, "code", "EXECUTION_FAILED")
                    ),
                    facts={"execution": getattr(exc, "execution_metadata", {})}
                    if getattr(step, "stable_identity", False)
                    else {},
                )
            if getattr(step, "stable_identity", False):
                outcome.facts = redact(outcome.facts)
                if outcome.error:
                    outcome.error = OutcomeError(
                        outcome.error.type, redact(outcome.error.message), outcome.error.code
                    )
                execution = outcome.facts.get("execution", {})
                execution.setdefault("engine", attempt.input_snapshot.get("engine"))
                execution.setdefault("model", attempt.input_snapshot.get("model"))
                if outcome.error:
                    execution.update(
                        error_type=outcome.error.type,
                        error_code=outcome.error.code or outcome.error.type,
                        error=outcome.error.message,
                    )
                journal = self.services.execution_journal
                if journal is not None:
                    try:
                        attempt.metadata["logs"] = journal.save(
                            run_id, node.name, number, execution
                        )
                    except Exception as exc:
                        # 日志存储失败不得掩盖真实节点结果。
                        attempt.metadata["log_error"] = type(exc).__name__
                for key in ("stdout", "stderr"):
                    if key in execution:
                        execution[key] = execution[key][:2048]
            retry_policy = getattr(step, "retry_policy", self.retry)
            head = None
            if self.head_reader:
                try:
                    head = self.head_reader(run.workspace_path)
                except TechnicalError as exc:
                    technical = True
                    outcome = StepOutcome(
                        OutcomeType.FAILED, error=OutcomeError(type(exc).__name__, str(exc))
                    )
                except ConcurrencyConflict:
                    raise
                except Exception as exc:
                    outcome = StepOutcome(
                        OutcomeType.FAILED, error=OutcomeError(type(exc).__name__, str(exc))
                    )
            pending = []
            retry_delay = None
            with self.repository.transaction():
                self._lease(run, owner)
                latest = self.repository.load_run(run_id)
                attempt.status = AttemptStatus(outcome.type.value)
                attempt.finished_at = self.services.clock.now()
                attempt.output = {
                    "type": outcome.type.value,
                    "facts": outcome.facts,
                    "external_refs": outcome.external_refs,
                    "next_poll_at": outcome.next_poll_at,
                }
                attempt.error_type = outcome.error.type if outcome.error else None
                attempt.error_message = outcome.error.message if outcome.error else None
                attempt.error_code = (
                    (outcome.error.code or outcome.error.type) if outcome.error else None
                )
                for artifact in outcome.artifacts:
                    artifact.git_revision = head
                attempt.artifacts = outcome.artifacts
                attempt.metadata["git_head"] = head
                attempt.metadata["duration_seconds"] = max(
                    0, attempt.finished_at - attempt.started_at
                )
                if getattr(step, "stable_identity", False):
                    execution = outcome.facts.get("execution", {})
                    attempt.metadata.update(
                        {k: execution.get(k) for k in ("engine", "model", "session_id")}
                    )
                self.repository.save_attempt(attempt)
                for artifact in outcome.artifacts:
                    self.repository.save_artifact(artifact)
                previous_status = latest.status
                if previous_status not in {RunStatus.PAUSED, RunStatus.CANCELLED}:
                    if technical:
                        failures = (
                            latest.context.get("technical_failures", {}).get(node.name, 0) + 1
                        )
                        latest.context["technical_failures"] = {node.name: failures}
                        latest.context.setdefault("recovery_inputs", {})[node.name] = deepcopy(
                            attempt.input_snapshot
                        )
                        latest.last_error = attempt.error_message
                        if failures <= retry_policy.max_retries:
                            retry_delay = retry_policy.delay(failures)
                            latest.context["next_poll_at"] = self.services.clock.now() + retry_delay
                        else:
                            self._fail(latest, attempt.error_message)
                    else:
                        latest.context = checkpoint_context(
                            latest.context, node.name, outcome, head
                        )
                        latest.last_error = outcome.error.message if outcome.error else None
                        if outcome.type is OutcomeType.FAILED:
                            self._fail(latest, latest.last_error or "步骤报告失败")
                        elif outcome.type is OutcomeType.PAUSED:
                            latest.status = RunStatus.PAUSED
                            point = latest.context.get("pause_point", {})
                            point.update(
                                created_at=self.services.clock.now(),
                                revision=latest.revision,
                                attempt_id=attempt.attempt_id,
                            )
                            latest.context["pause_point"] = point
                        elif outcome.type is OutcomeType.CANCELLED:
                            latest.status = RunStatus.CANCELLED
                            latest.finished_at = self.services.clock.now()
                        elif outcome.type is OutcomeType.WAITING:
                            if outcome.next_poll_at is None:
                                self._fail(latest, "WAITING 必须提供 next_poll_at")
                            else:
                                latest.status = RunStatus.WAITING
                        else:
                            try:
                                next_node = self.workflow.next_node(
                                    node.name, outcome.type, latest.context
                                )
                            except Exception as exc:
                                self._fail(latest, str(exc))
                            else:
                                latest.current_step = next_node or node.name
                                if next_node is None:
                                    latest.status = RunStatus.SUCCEEDED
                                    latest.finished_at = self.services.clock.now()
                else:
                    # 允许用户在原子步骤执行期间暂停或取消，完成记录仍保留。
                    latest.context = checkpoint_context(latest.context, node.name, outcome, head)
                    latest.last_error = outcome.error.message if outcome.error else None
                    if previous_status is RunStatus.PAUSED and outcome.type is OutcomeType.FAILED:
                        if technical:
                            failures = (
                                run.context.get("technical_failures", {}).get(node.name, 0) + 1
                            )
                            latest.context["technical_failures"] = {node.name: failures}
                            latest.context.setdefault("recovery_inputs", {})[node.name] = deepcopy(
                                attempt.input_snapshot
                            )
                            if failures > retry_policy.max_retries:
                                self._fail(latest, latest.last_error)
                        else:
                            self._fail(latest, latest.last_error or "步骤报告失败")
                    if previous_status is RunStatus.PAUSED and outcome.type is OutcomeType.WAITING:
                        latest.context["paused_from"] = RunStatus.WAITING.value
                    if (
                        previous_status is RunStatus.PAUSED
                        or getattr(step, "stable_identity", False)
                    ) and outcome.type in {
                        OutcomeType.SUCCEEDED,
                        OutcomeType.SKIPPED,
                        OutcomeType.BLOCKED,
                    }:
                        try:
                            next_node = self.workflow.next_node(
                                node.name, outcome.type, latest.context
                            )
                        except Exception as exc:
                            if previous_status is RunStatus.PAUSED:
                                self._fail(latest, str(exc))
                        else:
                            if next_node:
                                latest.current_step = next_node
                            else:
                                latest.context[
                                    "completed_while_paused"
                                    if previous_status is RunStatus.PAUSED
                                    else "completed_while_cancelled"
                                ] = True
                if hasattr(step, "stage"):
                    stage = next(
                        s
                        for s in latest.context["stage_executions"]
                        if s["execution_id"] == attempt.metadata["stage_execution_id"]
                    )
                    stage["status"] = (
                        "FAILED"
                        if latest.status is RunStatus.FAILED
                        else "CANCELLED"
                        if latest.status is RunStatus.CANCELLED
                        else "PAUSED"
                        if latest.status is RunStatus.PAUSED
                        else "COMPLETED"
                        if (
                            not hasattr(
                                self.registry.resolve(
                                    self.workflow.nodes[latest.current_step].step
                                ),
                                "stage",
                            )
                            or self.registry.resolve(
                                self.workflow.nodes[latest.current_step].step
                            ).stage
                            != step.stage
                            or getattr(
                                self.registry.resolve(
                                    self.workflow.nodes[latest.current_step].step
                                ),
                                "loop",
                                None,
                            )
                            != step.loop
                            or (
                                step.loop
                                and outcome.facts.get("loop_decisions", {}).get(step.loop.name)
                                == "repeat"
                            )
                        )
                        else "RUNNING"
                    )
                    if stage["status"] != "RUNNING":
                        stage["finished_at"] = self.services.clock.now()
                    if latest.status is RunStatus.FAILED:
                        latest.context["fail_point"] = {
                            "stage": step.stage,
                            "job": node.name,
                            "round": attempt.metadata["round"],
                            "attempt_id": attempt.attempt_id,
                            "error_code": attempt.error_code or "EXECUTION_FAILED",
                        }
                latest.updated_at = self.services.clock.now()
                self.repository.update_run(latest)
                event_type = (
                    EventType.STEP_CANCELLED
                    if outcome.type is OutcomeType.CANCELLED
                    else EventType.STEP_FAILED
                    if outcome.type is OutcomeType.FAILED
                    else EventType.REVIEW_BLOCKED
                    if outcome.type is OutcomeType.BLOCKED
                    else EventType.STEP_SUCCEEDED
                    if outcome.type in {OutcomeType.SUCCEEDED, OutcomeType.SKIPPED}
                    else None
                )
                if event_type:
                    pending.append(
                        self._event(
                            event_type, latest, node.name, {"attempt_id": attempt.attempt_id}
                        )
                    )
                for name in outcome.facts.get("events", []):
                    pending.append(
                        self._event(
                            EventType(name),
                            latest,
                            node.name,
                            {
                                **outcome.external_refs,
                                **{k: v for k, v in outcome.facts.items() if k != "events"},
                            },
                        )
                    )
                if latest.status is RunStatus.SUCCEEDED:
                    pending.append(self._event(EventType.RUN_SUCCEEDED, latest))
                elif latest.status is RunStatus.FAILED:
                    pending.append(
                        self._event(
                            EventType.RUN_FAILED, latest, payload={"error_type": attempt.error_type}
                        )
                    )
                elif outcome.type is OutcomeType.PAUSED:
                    pending.append(self._event(EventType.RUN_PAUSED, latest, node.name))
            self.events.publish(pending)
            if latest.status is not RunStatus.RUNNING or self.stop_requested():
                return latest
            if retry_delay is not None:
                self.sleeper.sleep(retry_delay)
                # 退避状态已持久化；暂停/取消期间不重新开始步骤。
                with self.repository.transaction():
                    latest = self.repository.load_run(run_id)
                    self._lease(latest, owner)
                    if latest.status in {RunStatus.PAUSED, RunStatus.CANCELLED}:
                        return latest
                    latest.context["next_poll_at"] = None
                    self.repository.update_run(latest)
        return self.repository.load_run(run_id)

    def _fail(self, run, message):
        run.status = RunStatus.FAILED
        run.finished_at = self.services.clock.now()
        run.last_error = message
        run.context["next_poll_at"] = None

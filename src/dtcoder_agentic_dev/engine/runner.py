"""工作流内核仅依赖领域和端口，事务内先保存尝试及产物，再推进运行。"""

import logging
from copy import deepcopy
from dataclasses import asdict, replace
from types import MappingProxyType
from typing import Callable

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
                self.ids.new(),
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
                    self.repository.save_attempt(attempt)
                request = replace(
                    request, input_snapshot=MappingProxyType(deepcopy(attempt.input_snapshot))
                )
                self._lease(run, owner)
                outcome = step.execute(request, self.services)
                if not isinstance(outcome, StepOutcome):
                    raise FatalError("步骤必须返回 StepOutcome")
            except TechnicalError as exc:
                technical = True
                outcome = StepOutcome(
                    OutcomeType.FAILED, error=OutcomeError(type(exc).__name__, str(exc))
                )
            except BusinessError as exc:
                outcome = StepOutcome(
                    OutcomeType.BLOCKED, error=OutcomeError(type(exc).__name__, str(exc))
                )
            except ConcurrencyConflict:
                raise
            except Exception as exc:
                outcome = StepOutcome(
                    OutcomeType.FAILED, error=OutcomeError(type(exc).__name__, str(exc))
                )
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
                for artifact in outcome.artifacts:
                    artifact.git_revision = head
                attempt.artifacts = outcome.artifacts
                attempt.metadata["git_head"] = head
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
                        if failures <= self.retry.max_retries:
                            retry_delay = self.retry.delay(failures)
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
                            if failures > self.retry.max_retries:
                                self._fail(latest, latest.last_error)
                        else:
                            self._fail(latest, latest.last_error or "步骤报告失败")
                    if previous_status is RunStatus.PAUSED and outcome.type is OutcomeType.WAITING:
                        latest.context["paused_from"] = RunStatus.WAITING.value
                    if previous_status is RunStatus.PAUSED and outcome.type in {
                        OutcomeType.SUCCEEDED,
                        OutcomeType.SKIPPED,
                        OutcomeType.BLOCKED,
                    }:
                        next_node = self.workflow.next_node(node.name, outcome.type, latest.context)
                        if next_node:
                            latest.current_step = next_node
                        else:
                            latest.context["completed_while_paused"] = True
                latest.updated_at = self.services.clock.now()
                self.repository.update_run(latest)
                event_type = (
                    EventType.STEP_FAILED
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
                        self._event(EventType(name), latest, node.name, outcome.external_refs)
                    )
                if latest.status is RunStatus.SUCCEEDED:
                    pending.append(self._event(EventType.RUN_SUCCEEDED, latest))
                elif latest.status is RunStatus.FAILED:
                    pending.append(
                        self._event(
                            EventType.RUN_FAILED, latest, payload={"error_type": attempt.error_type}
                        )
                    )
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

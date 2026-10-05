"""回退创建后继运行；原运行尝试不重写。操作意图及部分失败永久保留。"""

import getpass
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    AttemptStatus,
    ExternalOperation,
    OperationStatus,
    RunStatus,
    WorkflowRun,
)
from dtcoder_agentic_dev.ports.pipeline import PipelineStatus

STEPS = ("requirements", "coding", "review", "fix", "pipeline", "create_pr")


class RollbackService:
    def __init__(self, runs):
        self.runs = runs
        self.store, self.git = runs.store, runs.git

    def _lock(self, run_id):
        guard = self.runs.execution_guard
        return getattr(guard, "try_acquire", guard)(run_id)

    def _plan(self, run_id, step, reason, keep_pr=False, keep_pipeline=False, backup=True):
        if self.runs.config.dry_run:
            raise ConfigurationError("演练模式不执行或模拟回退")
        if step not in STEPS or not reason.strip():
            raise ConfigurationError("回退需要合法步骤和非空原因")
        run = self.store.load_run(run_id)
        if run.status not in TERMINAL_STATUSES | {RunStatus.PAUSED}:
            raise ConfigurationError("只能回退终止或暂停运行")
        if run.lease_owner and (run.lease_expires_at or 0) > self.runs.clock.now():
            raise ConcurrencyConflict("有效租约存在，拒绝回退")
        if any(
            r.status in ACTIVE_STATUSES
            and r.run_id != run_id
            and r.issue_external_id == run.issue_external_id
            for r in self.store.list_runs(run.repository_id)
        ):
            raise ConcurrencyConflict("该 Issue 已有其他活动运行")
        self.runs.assert_no_pending_rollback(run)
        attempts = self.store.list_attempts(run_id)
        if any(a.status is AttemptStatus.RUNNING for a in attempts):
            raise ConcurrencyConflict("存在未结束的原子步骤，拒绝回退")
        targets = [a for a in attempts if a.step_name == step]
        if not targets:
            raise ConfigurationError("目标节点没有已记录输入，拒绝猜测修订")
        target = max(targets, key=lambda a: a.attempt_number)
        context = deepcopy(target.input_snapshot.get("run", {}).get("context", {}))
        revision = context.get("git_head") or context.get("base_revision")
        recorded = (
            {a.git_revision for a in self.store.list_artifacts(run_id)}
            | {a.metadata.get("git_head") for a in attempts}
            | {run.context.get("base_revision")}
        )
        if (
            not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)
            or revision not in recorded
        ):
            raise ConfigurationError("目标输入缺少本运行已记录的完整 Git 修订")
        if not run.workspace_path:
            raise ConfigurationError("原运行工作区已清理，无法安全回退")
        # recover 校验路径和分支归属，不创建或重置工作区。
        repo = self.runs.resolve_repository(run.repository_id)
        expected = Path(self.runs.config.workspace.workspaces) / run.repository_id / run.run_id
        if (
            not expected.resolve().is_relative_to(
                Path(self.runs.config.workspace.workspaces).resolve()
            )
            or Path(run.workspace_path).resolve() != expected.resolve()
        ):
            raise ConfigurationError("原运行工作区不属于受管路径")
        if (
            not self.git.is_repository(run.workspace_path)
            or self.git.current_branch(run.workspace_path) != run.branch
        ):
            raise ConfigurationError("原运行工作区分支身份不匹配")
        if not self.git.is_ancestor(run.workspace_path, revision):
            raise ConfigurationError("目标修订不是当前运行分支的祖先")
        actions = []
        for op in self.store.list_operations(run_id):
            if op.operation_type == "create_pr":
                if not op.external_id:
                    pr = self.runs.host.find_pull_request(run.repository_id, op.idempotency_key)
                    if pr:
                        pr = self.runs.host.get_pull_request(run.repository_id, pr.external_id)
                else:
                    pr = self.runs.host.get_pull_request(run.repository_id, op.external_id)
                if pr and pr.state.lower() not in {"open", "opened", "closed", "merged"}:
                    raise ConfigurationError("PR 状态不明，拒绝猜测合并情况")
                if pr and pr.state.lower() == "merged":
                    raise ConfigurationError("PR 已合并，拒绝自动回退")
                if pr and pr.state.lower() not in {"closed", "merged"} and not keep_pr:
                    actions.append({"type": "close_pr", "external_id": pr.external_id})
            if (
                op.operation_type == "pipeline"
                and not keep_pipeline
                and STEPS.index(step) <= STEPS.index("pipeline")
            ):
                if not op.external_id:
                    raise ConfigurationError("流水线意图尚未对账；先恢复其远端身份再回退")
                result = self.runs.pipeline.get_status(run.repository_id, op.external_id)
                if result.status in {PipelineStatus.PENDING, PipelineStatus.RUNNING}:
                    actions.append({"type": "cancel_pipeline", "external_id": op.external_id})
        return {
            "run_id": run_id,
            "step": step,
            "reason": reason,
            "before_revision": self.git.head_sha(run.workspace_path),
            "after_revision": revision,
            "source_revision": run.revision,
            "backup": backup,
            "keep_pr": keep_pr,
            "keep_pipeline": keep_pipeline,
            "remote_actions": actions,
            "context": context,
            "repository_id": repo.repository_id,
        }

    def plan(self, run_id, step, reason, **options):
        with self._lock(run_id):
            plan = self._plan(run_id, step, reason, **options)
        return {k: v for k, v in plan.items() if k != "context"}

    def execute(self, run_id, step, reason, *, expected_revision=None, **options):
        with self._lock(run_id):
            plan = self._plan(run_id, step, reason, **options)
            if expected_revision is not None and plan["source_revision"] != expected_revision:
                raise ConcurrencyConflict("回退计划生成后运行已变化，请重新查看计划")
            run = self.store.load_run(run_id)
            now = self.runs.clock.now()
            operation_id, successor_id = self.runs.ids.new(), self.runs.ids.new()
            operation = ExternalOperation(
                operation_id,
                run_id,
                "rollback",
                f"{run_id}:rollback:{operation_id}",
                request_snapshot={k: v for k, v in plan.items() if k != "context"},
                created_at=now,
                updated_at=now,
            )
            operation.request_snapshot["operator"] = getpass.getuser()
            operation.request_snapshot["successor_id"] = successor_id
            with self.store.transaction():
                latest = self.store.load_run(run_id)
                if latest.revision != plan["source_revision"]:
                    raise ConcurrencyConflict("回退意图落库前运行已变化")
                self.store.save_operation(operation)
            results = []
            try:
                if plan["backup"]:
                    timestamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    reference = f"refs/dtcoder/backups/{run_id}/{timestamp}-{operation_id}"
                    self.git.backup_ref(run.workspace_path, reference, plan["before_revision"])
                    operation.response_snapshot["backup_ref"] = reference
                    self.store.save_operation(operation)
                for action in plan["remote_actions"]:
                    result = dict(action)
                    try:
                        if action["type"] == "close_pr":
                            self.runs.host.close_pull_request(
                                run.repository_id, action["external_id"]
                            )
                        else:
                            self.runs.pipeline.cancel(run.repository_id, action["external_id"])
                        result["status"] = "SUCCEEDED"
                    except Exception as exc:
                        result.update(status="FAILED", error_type=type(exc).__name__)
                    results.append(result)
                    operation.response_snapshot["remote_actions"] = results
                    self.store.save_operation(operation)
                if any(r["status"] == "FAILED" for r in results):
                    raise TechnicalError("远端回退动作失败；部分动作已审计，请检查后重新生成计划")
                context = plan["context"]
                for name in (
                    "next_poll_at",
                    "technical_failures",
                    "recovery_inputs",
                    "completed_while_paused",
                    "paused_from",
                    "events",
                ):
                    context.pop(name, None)
                context.update(
                    rollback_from=run_id,
                    rollback_operation=operation_id,
                    rollback_revision=plan["after_revision"],
                )
                successor = WorkflowRun(
                    successor_id,
                    run.workflow_name,
                    run.workflow_version,
                    run.repository_id,
                    run.issue_external_id,
                    run.issue_number,
                    current_step=step,
                    branch=f"agent/rollback-{successor_id}",
                    base_branch=run.base_branch,
                    context=context,
                    created_at=now,
                    updated_at=now,
                )
                # 先将可恢复后继意图和原运行状态变更一起落库；准备工作区失败仍保留记录。
                with self.store.transaction():
                    latest = self.store.load_run(run_id)
                    if latest.revision != plan["source_revision"]:
                        raise ConcurrencyConflict("原运行已变化，拒绝创建后继")
                    if any(
                        r.status in ACTIVE_STATUSES
                        and r.run_id != run_id
                        and r.issue_external_id == run.issue_external_id
                        for r in self.store.list_runs(run.repository_id)
                    ):
                        raise ConcurrencyConflict("该 Issue 已有其他活动运行")
                    if latest.status is RunStatus.PAUSED:
                        latest.status = RunStatus.CANCELLED
                        latest.finished_at = now
                        latest.context["superseded_by"] = successor_id
                        self.store.update_run(latest)
                    # 后继先暂停；工作区准备完成后再允许调度。
                    successor.status = RunStatus.PAUSED
                    self.store.create_run(successor)
                path = self.runs.workspace.prepare_at(
                    successor,
                    self.runs.resolve_repository(run.repository_id),
                    plan["after_revision"],
                )
                with self.store.transaction():
                    successor = self.store.load_run(successor_id)
                    successor.workspace_path, successor.status = path, RunStatus.QUEUED
                    self.store.update_run(successor)
                    operation.status = OperationStatus.SUCCEEDED
                    operation.response_snapshot["successor_id"] = successor_id
                    operation.updated_at = self.runs.clock.now()
                    self.store.save_operation(operation)
                    event = self.runs.events.make(
                        EventType.RUN_ROLLED_BACK,
                        run_id,
                        step,
                        {"status": "SUCCEEDED", "successor_id": successor_id},
                    )
                    self.store.save_event(event)
                self.runs.events.publish([event])
                return successor
            except Exception as exc:
                operation.status = OperationStatus.FAILED
                operation.response_snapshot["error_type"] = type(exc).__name__
                operation.updated_at = self.runs.clock.now()
                self.store.save_operation(operation)
                event = self.runs.events.make(
                    EventType.RUN_ROLLED_BACK,
                    run_id,
                    step,
                    {"status": "FAILED", "error_type": type(exc).__name__},
                )
                self.store.save_event(event)
                self.runs.events.publish([event])
                raise

    def recover(self, run_id, reason):
        """显式对账崩溃留下的意图；仅已有后继才恢复，不猜测未执行动作成功。"""
        if self.runs.config.dry_run or not reason.strip():
            raise ConfigurationError("回退恢复需要非演练模式和非空原因")
        with self._lock(run_id):
            run = self.store.load_run(run_id)
            if run.status not in TERMINAL_STATUSES | {RunStatus.PAUSED} or (
                run.lease_owner and (run.lease_expires_at or 0) > self.runs.clock.now()
            ):
                raise ConcurrencyConflict("原运行仍活动或持有租约，拒绝恢复回退")
            pending = [
                o
                for o in self.store.list_operations(run_id)
                if o.operation_type == "rollback" and o.status is OperationStatus.PENDING
            ]
            if len(pending) != 1:
                raise ConfigurationError("需要唯一的 PENDING 回退操作；请用 show 检查审计")
            op = pending[0]
            successor_id = op.request_snapshot["successor_id"]
            try:
                successor = self.store.load_run(successor_id)
            except KeyError:
                successor = None
            if successor:
                with self._lock(successor_id):
                    if (
                        successor.context.get("rollback_operation") != op.operation_id
                        or successor.status is not RunStatus.PAUSED
                        or (
                            successor.lease_owner
                            and (successor.lease_expires_at or 0) > self.runs.clock.now()
                        )
                    ):
                        raise ConcurrencyConflict("后继运行身份或状态不符合恢复条件")
                    actions = op.response_snapshot.get("remote_actions", [])
                    if len(actions) != len(op.request_snapshot["remote_actions"]) or any(
                        a.get("status") != "SUCCEEDED" for a in actions
                    ):
                        raise ConfigurationError("远端动作没有完整成功审计，拒绝恢复后继")
                    path = self.runs.workspace.prepare_at(
                        successor,
                        self.runs.resolve_repository(successor.repository_id),
                        op.request_snapshot["after_revision"],
                    )
                    with self.store.transaction():
                        latest = self.store.load_run(successor_id)
                        if latest.revision != successor.revision:
                            raise ConcurrencyConflict("后继运行在恢复期间已变化")
                        latest.workspace_path, latest.status = path, RunStatus.QUEUED
                        self.store.update_run(latest)
                        op.status = OperationStatus.SUCCEEDED
                        op.response_snapshot["successor_id"] = successor_id
                        op.response_snapshot["recovery_reason"] = reason
                        op.updated_at = self.runs.clock.now()
                        self.store.save_operation(op)
            else:
                # 请求可能已完成部分远端动作；记录中断，下一次计划重新查询远端。
                op.status = OperationStatus.FAILED
                op.response_snapshot.update(
                    error_type="ProcessInterrupted",
                    recovery_reason=reason,
                    recovered_by=getpass.getuser(),
                )
                op.updated_at = self.runs.clock.now()
                self.store.save_operation(op)
            event = self.runs.events.make(
                EventType.RUN_ROLLED_BACK,
                run_id,
                op.request_snapshot["step"],
                {
                    "status": op.status.value,
                    "error_type": op.response_snapshot.get("error_type", ""),
                    "successor_id": successor_id if successor else "",
                },
            )
            self.store.save_event(event)
            self.runs.events.publish([event])
            return op

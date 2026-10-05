"""应用用例：显式新运行、状态控制和清理，不依赖 Click。"""

from contextlib import nullcontext
from dataclasses import asdict

from dtcoder_agentic_dev.application.dto import RunDetails
from dtcoder_agentic_dev.config import validate_ref
from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict, ConfigurationError
from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    OperationStatus,
    RunStatus,
    WorkflowRun,
)


class RunService:
    def __init__(
        self,
        store,
        config,
        code_host,
        workspace,
        events,
        clock,
        ids,
        execution_guard=None,
        *,
        pipeline=None,
        git=None,
    ):
        self.store, self.config, self.host = store, config, code_host
        self.workspace, self.events, self.clock, self.ids = workspace, events, clock, ids
        self.pipeline, self.git = pipeline, git
        self.execution_guard = execution_guard or (lambda run_id: nullcontext())

    def resolve_repository(self, hint):
        exact = [
            r
            for r in self.config.repositories
            if r.name == hint or r.repository_id == hint or r.project == hint
        ]
        matches = exact or [
            r
            for r in self.config.repositories
            if hint in r.name or r.repository_id.startswith(hint)
        ]
        if len(matches) != 1:
            raise ConfigurationError("仓库提示没有唯一匹配，请使用仓库名称或完整 ID")
        return matches[0]

    def queue_issue(self, issue, repo, *, explicit=False):
        if (
            issue.repository_id != repo.repository_id
            or not issue.external_id
            or type(issue.number) is not int
            or issue.number <= 0
        ):
            raise ConfigurationError("Issue 的仓库、外部 ID 或编号无效")
        events = []
        with self.store.transaction():
            existing = [
                r
                for r in self.store.list_runs(repo.repository_id)
                if r.issue_external_id == issue.external_id
            ]
            if any(
                any(
                    o.operation_type == "rollback" and o.status is OperationStatus.PENDING
                    for o in self.store.list_operations(r.run_id)
                )
                for r in existing
            ):
                raise ConcurrencyConflict("Issue 存在尚未完成的回退意图，请检查审计")
            if any(r.status in ACTIVE_STATUSES for r in existing):
                if explicit:
                    raise ConcurrencyConflict("此 Issue 已有活动运行，请先完成或取消")
                return None
            if existing and not explicit:
                return None
            now = self.clock.now()
            run_id = self.ids.new()
            prefix = issue.requested_branch or f"agent/issue-{issue.number}"
            validate_ref(prefix)
            branch = f"{prefix}-{run_id}"
            run = WorkflowRun(
                run_id,
                self.config.workflow.name,
                self.config.workflow.version,
                repo.repository_id,
                issue.external_id,
                issue.number,
                current_step="requirements",
                branch=branch,
                base_branch=repo.base_branch,
                created_at=now,
                updated_at=now,
                context={"pipeline_enabled": repo.pipeline_enabled, "dry_run": self.config.dry_run},
            )
            snapshot = asdict(repo)
            snapshot["validation_commands"] = ["<已隐藏>"] if repo.validation_commands else []
            snapshot["pipeline"]["params"] = {k: "<已隐藏>" for k in snapshot["pipeline"]["params"]}
            self.store.save_repository(repo.repository_id, snapshot)
            self.store.save_issue(issue)
            self.store.create_run(run)
            event = self.events.make(EventType.RUN_QUEUED, run.run_id)
            self.store.save_event(event)
            events.append(event)
        self.events.publish(events)
        return run

    def process(self, hint, number):
        repo = self.resolve_repository(hint)
        issue = self.host.get_issue(repo.repository_id, number)
        if issue.repository_id != repo.repository_id or issue.number != number:
            raise ConfigurationError("代码托管适配器返回的 Issue 与请求不一致")
        return self.queue_issue(issue, repo, explicit=True)

    def details(self, run_id):
        return RunDetails(
            self.store.load_run(run_id),
            self.store.list_attempts(run_id),
            self.store.list_artifacts(run_id),
            self.store.list_operations(run_id),
        )

    def control(self, run_id, action):
        transitions = {
            "pause": ({RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING}, RunStatus.PAUSED),
            "resume": ({RunStatus.PAUSED}, RunStatus.QUEUED),
            "cancel": (ACTIVE_STATUSES, RunStatus.CANCELLED),
        }
        if action not in transitions:
            raise ConfigurationError("未知运行控制动作")
        allowed, target = transitions[action]
        events = []
        with self.store.transaction():
            run = self.store.load_run(run_id)
            self.assert_no_pending_rollback(run)
            if run.status not in allowed:
                raise ConfigurationError(f"{run.status.value} 状态不能执行 {action}")
            if action == "pause":
                run.context["paused_from"] = run.status.value
            if action == "resume":
                if run.context.pop("completed_while_paused", False):
                    target = RunStatus.SUCCEEDED
                elif run.context.pop("paused_from", None) == RunStatus.WAITING.value:
                    target = RunStatus.WAITING
            run.status, run.updated_at = target, self.clock.now()
            if target in TERMINAL_STATUSES:
                run.finished_at = self.clock.now()
            self.store.update_run(run)
            type = (
                EventType.RUN_CANCELLED
                if action == "cancel"
                else EventType.RUN_SUCCEEDED
                if target is RunStatus.SUCCEEDED
                else None
            )
            if type:
                event = self.events.make(type, run_id)
                self.store.save_event(event)
                events.append(event)
        self.events.publish(events)
        return run

    def retry(self, run_id):
        old = self.store.load_run(run_id)
        if old.status not in TERMINAL_STATUSES:
            raise ConfigurationError("只能为终止运行创建新的重跑记录")
        repo = self.resolve_repository(old.repository_id)
        issue = self.store.load_issue(old.repository_id, old.issue_external_id)
        return self.queue_issue(issue, repo, explicit=True)

    def cleanup(self, run_id):
        if self.config.dry_run:
            raise ConfigurationError("演练模式不执行工作区清理")
        with self.execution_guard(run_id):
            with self.store.transaction():
                run = self.store.load_run(run_id)
                self.assert_no_pending_rollback(run)
                if run.status not in TERMINAL_STATUSES:
                    raise ConfigurationError("不能清理活动运行；请先取消或等待终止")
                if run.lease_owner and (run.lease_expires_at or 0) > self.clock.now():
                    raise ConcurrencyConflict("原子步骤仍持有租约，暂不能清理工作区")
            self.workspace.cleanup(run)
            with self.store.transaction():
                run = self.store.load_run(run_id)
                run.workspace_path = None
                run.context["workspace_cleaned"] = True
                run.updated_at = self.clock.now()
                self.store.update_run(run)

    def assert_no_pending_rollback(self, run):
        source_ids = [run.run_id]
        if run.context.get("rollback_from"):
            source_ids.append(run.context["rollback_from"])
        if any(
            o.operation_type == "rollback" and o.status is OperationStatus.PENDING
            for source in source_ids
            for o in self.store.list_operations(source)
        ):
            raise ConcurrencyConflict("存在未完成的回退意图，请执行 rollback-recover 并检查审计")

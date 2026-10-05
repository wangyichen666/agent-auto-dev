"""远端副作用步骤：先保存操作意图，再执行或查询，最后保存远端身份。"""

from dtcoder_agentic_dev.domain.errors import TechnicalError
from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import ExternalOperation, OperationStatus
from dtcoder_agentic_dev.domain.workflow import OutcomeError, OutcomeType, StepOutcome
from dtcoder_agentic_dev.engine.steps.development import PromptStep, _read, changes_directory
from dtcoder_agentic_dev.ports.pipeline import PipelineStatus


class PipelineStep:
    name = "pipeline"

    def __init__(self, pipeline, repository, ids, poll_interval=30):
        self.pipeline, self.repository, self.ids = pipeline, repository, ids
        self.poll_interval = poll_interval

    def execute(self, request, services):
        key = f"{request.run.run_id}:pipeline"
        op = self.repository.find_operation(key)
        new_trigger = op is None or op.external_id is None
        if op is None:
            op = ExternalOperation(
                self.ids.new(),
                request.run.run_id,
                "pipeline",
                key,
                request_snapshot={
                    "repository_id": request.run.repository_id,
                    "branch": request.run.branch,
                },
                created_at=services.clock.now(),
                updated_at=services.clock.now(),
            )
            self.repository.save_operation(op)
        if op.external_id is None:
            # trigger 的 Port 契约要求远端同键去重，以恢复 PENDING 意图。
            result = self.pipeline.trigger(request.run.repository_id, request.run.branch, key)
            if not result.external_id:
                raise TechnicalError("流水线触发没有返回 external_id")
            op.external_id, op.external_url = result.external_id, result.url
            op.status = OperationStatus.SUCCEEDED
            op.response_snapshot = {"status": result.status.value}
            op.updated_at = services.clock.now()
            self.repository.save_operation(op)
        else:
            result = self.pipeline.get_status(request.run.repository_id, op.external_id)
            op.response_snapshot = {"status": result.status.value}
            op.updated_at = services.clock.now()
            self.repository.save_operation(op)
        refs = {"pipeline_id": op.external_id, "pipeline_url": op.external_url}
        if new_trigger or result.status in {PipelineStatus.PENDING, PipelineStatus.RUNNING}:
            return StepOutcome(
                OutcomeType.WAITING,
                external_refs=refs,
                facts={"events": [EventType.PIPELINE_STARTED.value] if new_trigger else []},
                next_poll_at=services.clock.now() + self.poll_interval,
            )
        if (
            result.status in {PipelineStatus.FAILED, PipelineStatus.CANCELLED}
            and request.repository.pipeline_failure_blocks
        ):
            return StepOutcome(
                OutcomeType.FAILED,
                error=OutcomeError("PipelineRejected", f"流水线未通过：{result.status.value}"),
                external_refs=refs,
                facts={
                    "pipeline_status": result.status.value,
                    "events": [EventType.PIPELINE_FINISHED.value],
                },
            )
        return StepOutcome(
            OutcomeType.SUCCEEDED,
            external_refs=refs,
            facts={
                "pipeline_status": result.status.value,
                "events": [EventType.PIPELINE_FINISHED.value],
            },
        )


class CreatePRStep(PromptStep):
    name, template = "create_pr", "pr_create"

    def __init__(self, renderer, git, code_host, repository, ids):
        super().__init__(renderer, git)
        self.host, self.repository, self.ids = code_host, repository, ids

    def execute(self, request, services):
        path = f"{changes_directory(request)}/pr_body.md"
        try:
            body = _read(services.artifact_store, request.workspace, path)
            self.validate_body(body)
        except TechnicalError:
            self.invoke(request, services)
            body = _read(services.artifact_store, request.workspace, path)
            self.validate_body(body)
        artifacts = self.capture(request, services, [path])
        self.commit(request, [path])
        key = f"{request.run.run_id}:create_pr"
        op = self.repository.find_operation(key)
        if op is None:
            op = ExternalOperation(
                self.ids.new(),
                request.run.run_id,
                "create_pr",
                key,
                request_snapshot={
                    "repository_id": request.run.repository_id,
                    "branch": request.run.branch,
                    "base_branch": request.run.base_branch,
                    "title": request.issue.title,
                    "body": body,
                },
                created_at=services.clock.now(),
                updated_at=services.clock.now(),
            )
            self.repository.save_operation(op)
        if op.status is OperationStatus.SUCCEEDED and op.external_id and op.external_url:
            return StepOutcome(
                OutcomeType.SUCCEEDED,
                artifacts=artifacts,
                external_refs={"pr_id": op.external_id, "pr_url": op.external_url},
            )
        pr = self.host.find_pull_request(request.run.repository_id, key)
        if pr is None:
            self.git.push(request.workspace, request.run.branch)
            snapshot = op.request_snapshot
            pr = self.host.create_pull_request(
                request.run.repository_id,
                branch=snapshot["branch"],
                base_branch=snapshot["base_branch"],
                title=snapshot["title"],
                body=snapshot["body"],
                idempotency_key=key,
            )
        if not pr.external_id or not pr.url:
            raise TechnicalError("PR 返回结果缺少身份或 URL")
        op.status, op.external_id, op.external_url = (
            OperationStatus.SUCCEEDED,
            pr.external_id,
            pr.url,
        )
        op.response_snapshot = {"external_id": pr.external_id, "url": pr.url, "state": pr.state}
        op.updated_at = services.clock.now()
        self.repository.save_operation(op)
        return StepOutcome(
            OutcomeType.SUCCEEDED,
            artifacts=artifacts,
            external_refs={"pr_id": pr.external_id, "pr_url": pr.url},
            facts={"events": [EventType.PULL_REQUEST_CREATED.value]},
        )

    @staticmethod
    def validate_body(body):
        import re

        for heading in ("Summary", "Linked Issue", "Changes", "Test Plan"):
            match = re.search(
                r"^#{1,6}\s+" + re.escape(heading) + r"\s*\n(.*?)(?=^#{1,6}\s|\Z)",
                body,
                re.M | re.S,
            )
            if not match or not match.group(1).strip():
                raise TechnicalError(f"PR 描述缺少非空章节：{heading}")

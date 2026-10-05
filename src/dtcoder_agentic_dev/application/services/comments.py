"""可选的平台评论事件处理器，评论失败只影响事件投递审计。"""

from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import ExternalOperation, OperationStatus


class IssueCommentHandler:
    MESSAGES = {
        EventType.RUN_STARTED: "研发运行已开始。",
        EventType.REVIEW_BLOCKED: "代码评审发现阻断问题，将进入修复流程。",
        EventType.PIPELINE_STARTED: "流水线已触发，等待结果。",
        EventType.PIPELINE_FINISHED: "流水线查询已完成。",
        EventType.PULL_REQUEST_CREATED: "Pull Request 已创建。",
        EventType.RUN_FAILED: "研发运行失败，请检查运行记录。",
        EventType.RUN_CANCELLED: "研发运行已取消。",
        EventType.RUN_SUCCEEDED: "研发运行已完成。",
    }

    def __init__(self, host, store, clock, ids):
        self.host, self.store, self.clock, self.ids = host, store, clock, ids

    def __call__(self, event):
        message = self.MESSAGES.get(event.type)
        if not message:
            return
        run = self.store.load_run(event.run_id)
        key = f"{event.run_id}:comment:{event.event_id}"
        op = self.store.find_operation(key)
        if op and op.status is OperationStatus.SUCCEEDED:
            return
        if op is None:
            body = f"{message}\n运行 ID：{run.run_id}"
            if event.type is EventType.PULL_REQUEST_CREATED:
                body += f"\nPR：{event.payload.get('pr_url', '')}"
            op = ExternalOperation(
                self.ids.new(),
                run.run_id,
                "comment",
                key,
                request_snapshot={"body": body},
                created_at=self.clock.now(),
                updated_at=self.clock.now(),
            )
            self.store.save_operation(op)
        # Port 要求评论也按 key 在远端去重。
        external_id = self.host.create_issue_comment(
            run.repository_id, run.issue_external_id, op.request_snapshot["body"], key
        )
        op.external_id, op.status = external_id, OperationStatus.SUCCEEDED
        op.updated_at = self.clock.now()
        self.store.save_operation(op)

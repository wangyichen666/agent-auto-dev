"""评论意图落库，远端标记对账；处理失败交给事件分发器审计。"""

from dtcoder_agentic_dev.application.services.event_views import EventViewBuilder
from dtcoder_agentic_dev.domain.models import ExternalOperation, OperationStatus


class IssueCommentHandler:
    def __init__(self, host, store, clock, ids, views=None):
        self.host, self.store, self.clock, self.ids = host, store, clock, ids
        self.views = views or EventViewBuilder(store)

    def __call__(self, event):
        view = self.views.build(event)
        if not view:
            return
        run = self.store.load_run(event.run_id)
        key = f"{event.run_id}:comment:{event.event_id}"
        op = self.store.find_operation(key)
        if op and op.status is OperationStatus.SUCCEEDED:
            return
        if op is None:
            op = ExternalOperation(
                self.ids.new(),
                run.run_id,
                "comment",
                key,
                request_snapshot={"body": view["text"]},
                created_at=self.clock.now(),
                updated_at=self.clock.now(),
            )
            self.store.save_operation(op)
        external_id = self.host.create_issue_comment(
            run.repository_id, run.issue_external_id, op.request_snapshot["body"], key
        )
        op.external_id, op.status = external_id, OperationStatus.SUCCEEDED
        op.updated_at = self.clock.now()
        self.store.save_operation(op)

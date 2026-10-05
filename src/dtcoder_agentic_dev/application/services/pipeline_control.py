"""取消运行时及迟到的触发事件发生后，尽力取消远端流水线。"""

from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import RunStatus
from dtcoder_agentic_dev.ports.pipeline import PipelineStatus


class PipelineCancellationHandler:
    def __init__(self, store, pipeline):
        self.store, self.pipeline = store, pipeline

    def __call__(self, event):
        if event.type not in {EventType.RUN_CANCELLED, EventType.PIPELINE_STARTED}:
            return
        run = self.store.load_run(event.run_id)
        if run.status is not RunStatus.CANCELLED:
            return
        for op in self.store.list_operations(run.run_id):
            if op.operation_type == "pipeline" and op.external_id:
                result = self.pipeline.get_status(run.repository_id, op.external_id)
                if result.status in {PipelineStatus.PENDING, PipelineStatus.RUNNING}:
                    self.pipeline.cancel(run.repository_id, op.external_id)

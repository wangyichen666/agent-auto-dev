"""评论和通知共享的事件视图；只描述已提交的事实。"""

from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.prompts.renderer import StrictPromptRenderer

MESSAGES = {
    "run_started": "研发运行已开始。",
    "requirements": "需求产物已完成。",
    "coding": "编码与验证已完成。",
    "review": "代码评审已通过。",
    "review_blocked": "代码评审发现阻断问题。",
    "pipeline_started": "流水线已触发，等待结果。",
    "pipeline_passed": "流水线已通过。",
    "pipeline_failed": "流水线未通过。",
    "pr_created": "Pull Request 已创建。",
    "run_failed": "研发运行失败，请检查运行记录。",
    "run_cancelled": "研发运行已取消。",
    "run_succeeded": "研发运行已完成。",
    "rollback": "回退操作已记录，请检查远端动作结果。",
}
DEFAULT_TEMPLATES = {
    name: message
    + "\n运行 ID：{run_id}\n分支：{branch}\n步骤：{step}\n评审轮次：{round}\n产物：{artifacts}\n流水线：{pipeline_url}\nPR：{pr_url}\n失败类型：{error_type}\n结果：{result}\n"
    for name, message in MESSAGES.items()
}


class EventViewBuilder:
    def __init__(self, store, directory=None):
        self.store = store
        self.renderer = StrictPromptRenderer(directory) if directory else None

    def build(self, event):
        names = {
            EventType.RUN_STARTED: "run_started",
            EventType.REVIEW_BLOCKED: "review_blocked",
            EventType.PIPELINE_STARTED: "pipeline_started",
            EventType.PULL_REQUEST_CREATED: "pr_created",
            EventType.RUN_FAILED: "run_failed",
            EventType.RUN_CANCELLED: "run_cancelled",
            EventType.RUN_SUCCEEDED: "run_succeeded",
            EventType.RUN_ROLLED_BACK: "rollback",
        }
        name = names.get(event.type)
        if event.type is EventType.STEP_SUCCEEDED and event.step in {
            "requirements",
            "coding",
            "review",
        }:
            name = event.step
        if event.type is EventType.PIPELINE_FINISHED:
            name = (
                "pipeline_passed"
                if event.payload.get("pipeline_status") == "SUCCEEDED"
                else "pipeline_failed"
            )
        if not name:
            return None
        run = self.store.load_run(event.run_id)
        issue = self.store.load_issue(run.repository_id, run.issue_external_id)
        attempts = self.store.list_attempts(run.run_id)
        attempt = next(
            (a for a in attempts if a.attempt_id == event.payload.get("attempt_id")), None
        )
        variables = {
            "run_id": run.run_id,
            "branch": run.branch,
            "step": event.step or run.current_step,
            "round": str(
                (attempt.output.get("facts", {}) if attempt else {}).get(
                    "review_round", run.context.get("review_round", "")
                )
            ),
            "artifacts": ", ".join(a.relative_path for a in (attempt.artifacts if attempt else [])),
            "pipeline_url": event.payload.get("pipeline_url", run.context.get("pipeline_url", ""))
            or "",
            "pr_url": event.payload.get("pr_url", run.context.get("pr_url", "")) or "",
            "error_type": event.payload.get("error_type")
            or (attempt.error_type if attempt else "")
            or "",
            "result": event.payload.get("pipeline_status", event.payload.get("status", "")),
        }
        text = (
            self.renderer.render(name, variables).text
            if self.renderer
            else DEFAULT_TEMPLATES[name].format_map(variables)
        )
        return {
            "text": text,
            "variables": variables,
            "assignees": issue.assignees,
            "event_id": event.event_id,
        }

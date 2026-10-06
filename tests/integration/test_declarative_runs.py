"""YAML 任务真实文件/SQLite 闭环，所有外部执行均使用 fake。"""

from dataclasses import replace

import pytest
import yaml

from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.adapters.tools.local import LocalTools
from dtcoder_agentic_dev.application.services.declarative import DeclarativeRunService
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict, ConfigurationError
from dtcoder_agentic_dev.domain.models import AttemptStatus, RunStatus
from dtcoder_agentic_dev.domain.workflow import StepServices
from dtcoder_agentic_dev.engine.registry import ExecutorRegistry
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import FileArtifactStore
from dtcoder_agentic_dev.infrastructure.filesystem.definitions import WorkflowDefinitionStore
from dtcoder_agentic_dev.infrastructure.filesystem.workspace import LocalTaskWorkspace


def source(*, loop=False, pause=False, retries=0):
    definition = {
        "name": "local-files",
        "version": "1",
        "stages": ["write"],
        "jobs": {
            "write": {
                "stage": "write",
                "type": "tool",
                "toolName": "file.write",
                "config": {
                    "path": "result.json",
                    "content": '{"passed": false}',
                    "execute": {
                        "humanAgentType": "approval" if pause else "auto",
                        "retries": retries,
                    },
                },
                "outputs": {"result": {"path": "result.json", "parameters": ["passed"]}},
                "artifact_check": [{"type": "json", "path": "result.json", "fields": ["passed"]}],
            }
        },
    }
    if loop:
        definition["loops"] = [
            {
                "name": "review",
                "jobs": ["write"],
                "max_rounds": 2,
                "until": {"job": "write", "parameter": "passed", "equals": True},
                "on_exhausted": "pause",
            }
        ]
    return yaml.safe_dump(definition, sort_keys=False)


def service(tmp_path, clock, ids, store=None):
    store = store or SQLiteRunRepository(tmp_path / "state.db")
    registry = ExecutorRegistry()
    LocalTools(None).register(registry)
    events = EventDispatcher(store, clock, ids)
    service = DeclarativeRunService(
        store,
        WorkflowDefinitionStore(tmp_path / "definitions"),
        registry,
        StepServices(None, FileArtifactStore(clock, ids), clock),
        events,
        ids,
        clock,
        str(tmp_path / "workspaces"),
        clock,
        local_workspace=LocalTaskWorkspace(tmp_path / "workspaces"),
    )
    return service


def execute(service, run_id, clock):
    claimed = service.store.claim_run("test", clock.now(), 100, run_id)
    assert claimed is not None
    try:
        run = service.prepare_workspace(claimed, None)
        issue = service.store.load_issue(run.repository_id, run.issue_external_id)
        return service.runner_for(run).run(run.run_id, issue, None, "test")
    finally:
        service.store.release_lease(run_id, "test")


def test_no_repo_sqlite_reopen_and_frozen_definition(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(), "写文件")
    assert run.repository_id == "" and not (tmp_path / "workspaces" / run.run_id / ".git").exists()
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.SUCCEEDED
    assert result.context["stage_executions"][0]["status"] == "COMPLETED"
    attempts = svc.store.list_attempts(run.run_id)
    assert attempts[0].metadata["stage_execution_id"]
    assert attempts[0].metadata["duration_seconds"] >= 0
    assert len(svc.store.list_artifacts(run.run_id)) == 1
    svc.store.close()
    reopened = service(tmp_path, clock, ids)
    assert reopened.store.load_run(run.run_id).status is RunStatus.SUCCEEDED
    assert reopened.definitions.read(run.run_id, run.context["workflow_definition"])[1]


def test_approval_and_feedback_preserved_then_resume(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(pause=True), "审批")
    paused = execute(svc, run.run_id, clock)
    assert paused.status is RunStatus.PAUSED
    assert paused.context["pause_point"]["reason"] == "approval"
    assert not (tmp_path / "workspaces" / run.run_id / "result.json").exists()
    resumed = svc.control(run.run_id, "resume", feedback="Authorization: Bearer abc123\n继续")
    assert "abc123" not in str(resumed.context["feedback"])
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.SUCCEEDED
    assert [a.status for a in svc.store.list_attempts(run.run_id)][:2] == [
        AttemptStatus.PAUSED,
        AttemptStatus.SUCCEEDED,
    ]
    assert len(result.context["pause_history"]) == 1


def test_loop_exhaustion_pause_preserves_history(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(loop=True), "有限循环")
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.PAUSED
    assert result.context["pause_point"]["reason"] == "loop_exhausted"
    assert len(svc.store.list_attempts(run.run_id)) == 2
    assert len(result.context["stage_executions"]) == 2
    svc.control(run.run_id, "resume", feedback="重新检查", mode="revise")
    again = execute(svc, run.run_id, clock)
    assert again.status is RunStatus.PAUSED
    assert len(svc.store.list_attempts(run.run_id)) == 4
    assert len(again.context["feedback"]) == 1


def test_retry_starts_failed_job_keeps_completed_outputs(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    data = yaml.safe_load(source())
    data["jobs"]["check"] = {
        "stage": "write",
        "type": "tool",
        "toolName": "file.copy",
        "config": {"source": "missing.txt", "target": "copy.txt"},
    }
    run = svc.submit(yaml.safe_dump(data, sort_keys=False), "失败后重试")
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.FAILED
    svc.control(run.run_id, "retry")
    workspace = tmp_path / "workspaces" / run.run_id
    (workspace / "missing.txt").write_text("恢复输入")
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.SUCCEEDED
    assert len([a for a in svc.store.list_attempts(run.run_id) if a.step_name == "write"]) == 1
    assert len([a for a in svc.store.list_attempts(run.run_id) if a.step_name == "check"]) == 2


def test_snapshot_tamper_and_stale_writer_rejected(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(), "快照")
    stale = replace(run)
    svc.control(run.run_id, "pause")
    with pytest.raises(ConcurrencyConflict):
        svc.store.update_run(stale)
    (tmp_path / "definitions" / run.run_id / "resolved_workflow.yaml").write_text(
        source(pause=True)
    )
    with pytest.raises(ConfigurationError, match="摘要"):
        svc.runner_for(run)


def test_continue_conversation_explicitly_rejected_without_runtime(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(pause=True), "暂停")
    execute(svc, run.run_id, clock)
    with pytest.raises(ConfigurationError, match="会话"):
        svc.control(run.run_id, "resume", mode="continue_conversation", feedback="继续")
    assert svc.store.load_run(run.run_id).status is RunStatus.PAUSED


def test_agent_routing_model_inputs_and_loop_exit(tmp_path, clock, ids):
    import json

    from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult

    class Agent:
        engine = "fake-agent"

        def __init__(self):
            self.requests = []

        def execute(self, request):
            self.requests.append(request)
            (tmp_path / "workspaces" / request.run_id / "result.json").write_text(
                json.dumps({"passed": len(self.requests) == 2})
            )
            return AgentExecutionResult(
                "Authorization: Bearer do-not-store", structured={"session_id": "session-1"}
            )

    svc = service(tmp_path, clock, ids)
    agent = Agent()
    svc.executors.register_agent("selected", agent)
    data = yaml.safe_load(source(loop=True))
    job = data["jobs"]["write"]
    job.update(type="agent", agent="selected", model="explicit-model", prompt="按要求产出结果")
    job.pop("toolName")
    job["config"] = {"execute": {"timeout": 10}}
    run = svc.submit(yaml.safe_dump(data, sort_keys=False), "模型任务")
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.SUCCEEDED
    assert len(agent.requests) == 2
    assert agent.requests[0].model == "explicit-model"
    assert agent.requests[0].timeout == 10
    assert agent.requests[0].allow_no_repo
    attempt = svc.store.list_attempts(run.run_id)[0]
    assert attempt.metadata["engine"] == "fake-agent"
    assert attempt.metadata["session_id"] == "session-1"
    assert "do-not-store" not in str(attempt.output)
    assert all(s["status"] == "COMPLETED" for s in result.context["stage_executions"])


def test_node_timeout_is_failed_not_success(tmp_path, clock, ids):
    from dtcoder_agentic_dev.ports.tool_executor import ToolResult

    class Slow:
        def execute(self, request):
            clock.value += 2
            return ToolResult()

    svc = service(tmp_path, clock, ids)
    svc.executors.register_tool("slow", Slow())
    data = yaml.safe_load(source())
    data["jobs"]["write"].update(toolName="slow", outputs={}, artifact_check=[])
    data["jobs"]["write"]["config"] = {"execute": {"timeout": 1}}
    run = svc.submit(yaml.safe_dump(data), "超时")
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.FAILED
    assert svc.store.list_attempts(run.run_id)[0].error_type == "ExternalCommandTimeout"
    assert result.context["fail_point"]["error_code"] == "TIMEOUT"


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_control_during_atomic_action_preserves_result_and_stops_next(tmp_path, clock, ids, action):
    from dtcoder_agentic_dev.ports.tool_executor import ToolResult

    svc = service(tmp_path, clock, ids)

    class Controlled:
        def execute(self, request):
            svc.control(run.run_id, action)
            return ToolResult()

    svc.executors.register_tool("controlled", Controlled())
    data = yaml.safe_load(source())
    data["jobs"]["write"].update(toolName="controlled", outputs={}, artifact_check=[], config={})
    data["jobs"]["next"] = {
        "stage": "write",
        "type": "tool",
        "toolName": "file.write",
        "config": {"path": "next.txt", "content": "下一节点"},
    }
    run = svc.submit(yaml.safe_dump(data, sort_keys=False), "并发控制")
    result = execute(svc, run.run_id, clock)
    assert result.status is (RunStatus.PAUSED if action == "pause" else RunStatus.CANCELLED)
    assert svc.store.list_attempts(run.run_id)[0].status is AttemptStatus.SUCCEEDED
    assert not (tmp_path / "workspaces" / run.run_id / "next.txt").exists()
    svc.control(run.run_id, "resume" if action == "pause" else "retry")
    assert execute(svc, run.run_id, clock).status is RunStatus.SUCCEEDED


def test_human_node_validates_manual_artifact_without_executing_tool(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    data = yaml.safe_load(source(pause=True))
    data["jobs"]["write"]["config"]["execute"]["humanAgentType"] = "human"
    run = svc.submit(yaml.safe_dump(data), "人工填写")
    execute(svc, run.run_id, clock)
    path = tmp_path / "workspaces" / run.run_id / "result.json"
    path.write_text('{"passed": true}')
    svc.control(run.run_id, "resume", feedback="已填写")
    assert execute(svc, run.run_id, clock).status is RunStatus.SUCCEEDED
    assert path.read_text() == '{"passed": true}'


def test_crash_recovery_keeps_attempt_and_same_stage_id(tmp_path, clock, ids):
    from dtcoder_agentic_dev.domain.models import StepAttempt

    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(), "进程恢复")
    claimed = svc.store.claim_run("dead", clock.now(), 1, run.run_id)
    svc.prepare_workspace(claimed, None)
    svc.store.save_attempt(
        StepAttempt(
            "interrupted",
            run.run_id,
            "write",
            1,
            input_snapshot={"prepared": True},
            started_at=clock.now(),
        )
    )
    svc.store.close()
    clock.value += 2
    reopened = service(tmp_path, clock, ids)
    result = execute(reopened, run.run_id, clock)
    assert result.status is RunStatus.SUCCEEDED
    attempts = reopened.store.list_attempts(run.run_id)
    assert attempts[0].error_type == "ProcessInterrupted"
    assert attempts[1].attempt_number == 2


def test_resume_while_lease_active_rejected(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(), "活动租约")
    svc.store.claim_run("active", clock.now(), 100, run.run_id)
    svc.control(run.run_id, "pause")
    with pytest.raises(ConcurrencyConflict):
        svc.control(run.run_id, "resume")


def test_frozen_skill_and_raw_definition_are_independent_of_user_updates(tmp_path, clock, ids):
    from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult

    class Agent:
        def execute(self, request):
            assert "原始技能" in request.prompt and "后续修改" not in request.prompt
            (tmp_path / "workspaces" / request.run_id / "result.json").write_text(
                '{"passed": true}'
            )
            return AgentExecutionResult("完成")

    svc = service(tmp_path, clock, ids)
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    (skill_root / "skill.md").write_text("原始技能")
    svc.definitions.skills = skill_root
    svc.executors.register_agent("default", Agent())
    data = yaml.safe_load(source())
    job = data["jobs"]["write"]
    job.update(type="agent", skill="skill.md")
    job.pop("toolName")
    job["config"] = {}
    raw = yaml.safe_dump(data, sort_keys=False)
    run = svc.submit(raw, "模板冻结")
    (skill_root / "skill.md").write_text("后续修改")
    original, resolved = svc.definitions.read(run.run_id, run.context["workflow_definition"])
    assert original == raw and "原始技能" in resolved
    assert execute(svc, run.run_id, clock).status is RunStatus.SUCCEEDED


def test_command_whitelist_script_and_no_shell(tmp_path, clock, ids, mocker):
    from dtcoder_agentic_dev.ports.command_runner import CommandResult

    commands = mocker.Mock()
    commands.run.return_value = CommandResult(
        ("checker", "result.json"), 0, "token=private-value", "", 0.2
    )
    svc = service(tmp_path, clock, ids)
    registry = ExecutorRegistry()
    LocalTools(commands, [["checker"]]).register(registry)
    svc.executors = registry
    data = yaml.safe_load(source())
    data["jobs"]["write"]["artifact_check"].append(
        {"type": "script", "path": "result.json", "command": ["checker", "result.json"]}
    )
    run = svc.submit(yaml.safe_dump(data), "受控验证")
    assert execute(svc, run.run_id, clock).status is RunStatus.SUCCEEDED
    commands.run.assert_called_once_with(
        ["checker", "result.json"],
        cwd=str(tmp_path / "workspaces" / run.run_id),
        timeout=1800,
        check=True,
    )
    data["jobs"]["write"]["artifact_check"][-1]["command"] = ["unknown", "result.json"]
    denied = svc.submit(yaml.safe_dump(data), "不允许的命令")
    assert execute(svc, denied.run_id, clock).status is RunStatus.FAILED
    assert commands.run.call_count == 1


def test_legacy_sqlite_payload_still_readable_after_new_records(tmp_path, clock, ids, run):
    import json
    from dataclasses import asdict

    from dtcoder_agentic_dev.domain.models import StepAttempt

    store = SQLiteRunRepository(tmp_path / "state.db")
    store.create_run(run)
    old = StepAttempt("old-attempt", run.run_id, "requirements", 1)
    payload = asdict(old)
    payload.pop("error_code")
    store.connection.execute(
        "INSERT INTO step_attempts VALUES(?,?,?,?,?)",
        (old.attempt_id, run.run_id, old.step_name, 1, json.dumps(payload)),
    )
    store.close()
    svc = service(tmp_path, clock, ids)
    fresh = svc.submit(source(), "兼容新任务")
    assert execute(svc, fresh.run_id, clock).status is RunStatus.SUCCEEDED
    assert svc.store.load_run(run.run_id).context == run.context
    assert svc.store.list_attempts(run.run_id)[0].error_code is None


def test_skip_requires_policy_and_records_real_skipped_attempt(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(pause=True), "不允许跳过")
    execute(svc, run.run_id, clock)
    with pytest.raises(ConfigurationError):
        svc.control(run.run_id, "skip")
    data = yaml.safe_load(source(pause=True))
    data["jobs"]["write"]["config"]["execute"]["allow_skip"] = True
    skippable = svc.submit(yaml.safe_dump(data), "允许跳过")
    execute(svc, skippable.run_id, clock)
    svc.control(skippable.run_id, "skip")
    assert execute(svc, skippable.run_id, clock).status is RunStatus.SUCCEEDED
    assert svc.store.list_attempts(skippable.run_id)[1].status is AttemptStatus.SKIPPED
    assert not (tmp_path / "workspaces" / skippable.run_id / "result.json").exists()


def test_loop_failure_retry_resets_bounded_rounds(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    data = yaml.safe_load(source(loop=True))
    data["loops"][0]["on_exhausted"] = "fail"
    run = svc.submit(yaml.safe_dump(data), "循环失败")
    assert execute(svc, run.run_id, clock).status is RunStatus.FAILED
    svc.control(run.run_id, "retry")
    assert execute(svc, run.run_id, clock).status is RunStatus.FAILED
    assert [a.metadata["round"] for a in svc.store.list_attempts(run.run_id)] == [1, 2, 1, 2]


def test_workspaces_do_not_adopt_or_delete_foreign_directories(tmp_path, clock, ids):
    from dtcoder_agentic_dev.domain.errors import FatalError

    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(), "身份检查")
    directory = tmp_path / "workspaces" / run.run_id
    directory.mkdir(parents=True)
    (directory / "user.txt").write_text("用户内容")
    with pytest.raises(FatalError):
        svc.local_workspace.prepare(run)
    with pytest.raises(FatalError):
        svc.local_workspace.cleanup(run)
    assert (directory / "user.txt").read_text() == "用户内容"


def test_cancel_between_snapshot_and_execute_does_not_start_action(tmp_path, clock, ids, mocker):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(), "安全边界")
    claimed = svc.store.claim_run("test", clock.now(), 100, run.run_id)
    svc.prepare_workspace(claimed, None)
    engine = svc.runner_for(run)
    step = engine.registry.resolve("write")
    original = step.input_snapshot

    def snapshot(request):
        result = original(request)
        svc.control(run.run_id, "cancel")
        return result

    mocker.patch.object(step, "input_snapshot", side_effect=snapshot)
    issue = svc.store.load_issue(run.repository_id, run.issue_external_id)
    try:
        assert engine.run(run.run_id, issue, None, "test").status is RunStatus.CANCELLED
    finally:
        svc.store.release_lease(run.run_id, "test")
    assert not (tmp_path / "workspaces" / run.run_id / "result.json").exists()
    assert svc.store.list_attempts(run.run_id)[0].status.value == "CANCELLED"


def test_no_repo_rejects_repository_only_tools_before_creating_run(tmp_path, clock, ids):
    class RepoOnly:
        requires_repository = True

        def execute(self, request):
            raise AssertionError("无仓节点不能执行")

    svc = service(tmp_path, clock, ids)
    svc.executors.register_tool("repo-only", RepoOnly())
    data = yaml.safe_load(source())
    data["jobs"]["write"].update(toolName="repo-only", config={}, outputs={}, artifact_check=[])
    with pytest.raises(ConfigurationError, match="仓库"):
        svc.submit(yaml.safe_dump(data), "拒绝 repo-only")
    assert svc.store.list_runs() == []


def test_failed_artifact_retains_executor_output_in_file_journal(tmp_path, clock, ids):
    from dtcoder_agentic_dev.infrastructure.filesystem.journal import FileExecutionJournal
    from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult

    class Agent:
        engine = "fake-agent"

        def execute(self, request):
            (tmp_path / "workspaces" / request.run_id / "result.json").write_text("invalid")
            return AgentExecutionResult("开始验证\ntoken=private-value", stderr="失败诊断")

    svc = service(tmp_path, clock, ids)
    svc.services = replace(svc.services, execution_journal=FileExecutionJournal(tmp_path / "logs"))
    svc.executors.register_agent("default", Agent())
    data = yaml.safe_load(source())
    job = data["jobs"]["write"]
    job.update(type="agent", prompt="产生文件")
    job.pop("toolName")
    job["config"] = {}
    run = svc.submit(yaml.safe_dump(data), "失败日志")
    assert execute(svc, run.run_id, clock).status is RunStatus.FAILED
    attempt = svc.store.list_attempts(run.run_id)[0]
    path = tmp_path / "logs" / attempt.metadata["logs"]["files"]["stdout"]["path"]
    assert "开始验证" in path.read_text() and "private-value" not in path.read_text()
    assert attempt.metadata["engine"] == "fake-agent"


def test_scheduler_stop_keeps_yaml_run_recoverable_without_starting_next(tmp_path, clock, ids):
    from dtcoder_agentic_dev.ports.tool_executor import ToolResult

    svc = service(tmp_path, clock, ids)
    stopping = False
    svc.stop_requested = lambda: stopping

    class Stopper:
        def execute(self, request):
            nonlocal stopping
            stopping = True
            return ToolResult()

    svc.executors.register_tool("stopper", Stopper())
    data = yaml.safe_load(source())
    data["jobs"]["write"].update(toolName="stopper", config={}, artifact_check=[], outputs={})
    data["jobs"]["next"] = {
        "stage": "write",
        "type": "tool",
        "toolName": "file.write",
        "config": {"path": "next.txt", "content": "下一节点"},
    }
    run = svc.submit(yaml.safe_dump(data, sort_keys=False), "优雅停止")
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.RUNNING and result.current_step == "next"
    assert not (tmp_path / "workspaces" / run.run_id / "next.txt").exists()
    stopping = False
    assert execute(svc, run.run_id, clock).status is RunStatus.SUCCEEDED


def test_command_failure_cannot_be_reported_as_success(tmp_path, clock, ids, mocker):
    from dtcoder_agentic_dev.ports.command_runner import CommandResult

    commands = mocker.Mock()
    commands.run.return_value = CommandResult(("check",), 1, "失败输出", "失败诊断", 0.1)
    svc = service(tmp_path, clock, ids)
    registry = ExecutorRegistry()
    LocalTools(commands, [["check"]]).register(registry)
    svc.executors = registry
    data = yaml.safe_load(source())
    data["jobs"]["write"].update(
        toolName="test", config={"command": ["check"]}, outputs={}, artifact_check=[]
    )
    run = svc.submit(yaml.safe_dump(data), "失败验证")
    result = execute(svc, run.run_id, clock)
    assert result.status is RunStatus.FAILED
    assert svc.store.list_attempts(run.run_id)[0].error_code == "COMMAND_FAILED"


def test_oversized_resolved_skill_rejected_before_persistence(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    svc.executors.register_agent("default", object())
    data = yaml.safe_load(source())
    job = data["jobs"]["write"]
    job.update(type="agent", skill="oversize.md")
    job.pop("toolName")
    job["config"] = {}
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    svc.definitions.skills = skill_root
    (skill_root / "oversize.md").write_text("x" * (1024 * 1024 + 1))
    with pytest.raises(ConfigurationError, match="1 MiB"):
        svc.submit(yaml.safe_dump(data), "拒绝超大技能")
    assert svc.store.list_runs() == []


def test_single_repo_yaml_reuses_workspace_and_records_git_anchor(tmp_path, clock, ids, mocker):
    from dtcoder_agentic_dev.config import RepoConfig

    svc = service(tmp_path, clock, ids)
    repo = RepoConfig("sample", "https://example.invalid/sample.git")
    run = svc.submit(source(), "单仓节点", repo)
    assert run.repository_id == repo.repository_id
    workspace = mocker.Mock()
    path = tmp_path / "workspaces" / repo.repository_id / run.run_id

    def prepare(current, repository):
        assert repository == repo
        path.mkdir(parents=True)
        current.context["base_revision"] = "baseline-fixture"
        return str(path)

    workspace.prepare.side_effect = prepare
    svc.head_reader = lambda directory: "head-fixture"
    claimed = svc.store.claim_run("test", clock.now(), 100, run.run_id)
    try:
        current = svc.prepare_workspace(claimed, repo, workspace)
        issue = svc.store.load_issue(repo.repository_id, run.issue_external_id)
        result = svc.runner_for(current).run(run.run_id, issue, repo, "test")
    finally:
        svc.store.release_lease(run.run_id, "test")
    assert result.status is RunStatus.SUCCEEDED
    assert result.context["base_revision"] == "baseline-fixture"
    assert svc.store.list_artifacts(run.run_id)[0].git_revision == "head-fixture"
    workspace.prepare.assert_called_once()
    workspace.recover.assert_not_called()


def test_expired_writer_cannot_overwrite_recovered_attempt_snapshot(tmp_path, clock, ids, mocker):
    svc = service(tmp_path, clock, ids)
    run = svc.submit(source(), "快照 fencing")
    claimed = svc.store.claim_run("old", clock.now(), 100, run.run_id)
    svc.prepare_workspace(claimed, None)
    engine = svc.runner_for(run)
    step = engine.registry.resolve("write")
    original = step.input_snapshot

    def snapshot(request):
        result = original(request)
        clock.value += 101
        assert svc.store.claim_run("new", clock.now(), 100, run.run_id)
        interrupted = svc.store.list_attempts(run.run_id)[0]
        interrupted.status = AttemptStatus.FAILED
        interrupted.error_type = "ProcessInterrupted"
        svc.store.save_attempt(interrupted)
        return result

    mocker.patch.object(step, "input_snapshot", side_effect=snapshot)
    issue = svc.store.load_issue(run.repository_id, run.issue_external_id)
    with pytest.raises(ConcurrencyConflict):
        engine.run(run.run_id, issue, None, "old")
    assert svc.store.list_attempts(run.run_id)[0].status is AttemptStatus.FAILED
    assert svc.store.load_run(run.run_id).lease_owner == "new"


def test_retry_archives_fail_point_instead_of_leaving_stale_active_pointer(tmp_path, clock, ids):
    svc = service(tmp_path, clock, ids)
    data = yaml.safe_load(source(loop=True))
    data["loops"][0]["on_exhausted"] = "fail"
    run = svc.submit(yaml.safe_dump(data), "失败点历史")
    failed = execute(svc, run.run_id, clock)
    point = failed.context["fail_point"]
    retried = svc.control(run.run_id, "retry")
    assert "fail_point" not in retried.context
    assert retried.context["fail_history"][0]["attempt_id"] == point["attempt_id"]


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "TIMED_OUT"])
def test_injected_agent_terminal_status_is_never_success(tmp_path, clock, ids, status):
    from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult, AgentExecutionStatus

    svc = service(tmp_path, clock, ids)

    class Agent:
        def execute(self, request):
            return AgentExecutionResult("未完成", returncode=0, status=AgentExecutionStatus(status))

    svc.executors.register_agent("default", Agent())
    data = {
        "name": "terminal",
        "version": "1",
        "stages": ["run"],
        "jobs": {"agent": {"stage": "run", "prompt": "执行任务"}},
    }
    run = svc.submit(yaml.safe_dump(data), "不能伪成功")
    result = execute(svc, run.run_id, clock)
    assert result.status is (RunStatus.CANCELLED if status == "CANCELLED" else RunStatus.FAILED)

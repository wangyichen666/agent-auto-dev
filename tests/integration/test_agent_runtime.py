"""真正复用 SQLite/Dispatcher 的多执行器任务，模型调用全部替身。"""

import json
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

import pytest
import yaml

from dtcoder_agentic_dev.application.services.initialization import initialize
from dtcoder_agentic_dev.cli.bootstrap import build_runtime
from dtcoder_agentic_dev.domain.errors import AgentCancelled, ConfigurationError
from dtcoder_agentic_dev.domain.models import AttemptStatus, RunStatus, StepAttempt
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionResult
from dtcoder_agentic_dev.ports.command_runner import CommandResult


def workflow(agent="default"):
    return yaml.safe_dump(
        {
            "name": "document",
            "version": "1",
            "stages": ["write"],
            "jobs": {
                "write": {
                    "stage": "write",
                    "agent": agent,
                    "prompt": "生成 report.md",
                    "outputs": {"report": "report.md"},
                    "artifact_check": [{"type": "nonempty", "path": "report.md"}],
                }
            },
        },
        allow_unicode=True,
        sort_keys=False,
    )


def runtime(tmp_path, clock, ids, commands=None):
    path = tmp_path / "config.yaml"
    initialize(path)
    config = yaml.safe_load(path.read_text())
    config["agents"] = {"default_engine": "claude-cli"}
    path.write_text(yaml.safe_dump(config))
    return build_runtime(
        path,
        commands=commands or Mock(spec=["run"]),
        clock=clock,
        sleeper=clock,
        ids=ids,
        console_logging=False,
    )


def test_default_freezes_and_dispatches_claude_without_network(tmp_path, clock, ids):
    commands = Mock(spec=["run"])

    def invoke(args, **kwargs):
        assert args[:2] == ["claude", "--print"]
        Path(kwargs["cwd"], "report.md").write_text("真实文件产物")
        return CommandResult(
            tuple(args),
            0,
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": "session-1",
                    "result": "完成",
                }
            ),
            "",
            1,
        )

    commands.run.side_effect = invoke
    rt = runtime(tmp_path, clock, ids, commands)
    try:
        run = rt.declarative.submit(workflow(), "需求")
        _, resolved = rt.declarative.definitions.read(
            run.run_id, run.context["workflow_definition"]
        )
        assert yaml.safe_load(resolved)["jobs"]["write"]["agent"] == "claude-cli"
        rt.config.agents.default_engine = "codex-cli"
        assert rt.dispatcher.dispatch_once(run.run_id).status is RunStatus.SUCCEEDED
        attempt = rt.store.list_attempts(run.run_id)[0]
        assert attempt.metadata["engine"] == "claude-cli"
        assert attempt.metadata["session_id"] == "session-1"
    finally:
        rt.close()


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_persistent_control_cooperatively_stops_owned_agent(tmp_path, clock, ids, action):
    rt = runtime(tmp_path, clock, ids)
    entered = Event()
    requests = []

    class Agent:
        engine = "claude-cli"
        supports_resume = True

        def execute(self, req):
            requests.append(req)
            req.on_event({"type": "system", "session_id": "session-1"})
            entered.set()
            for _ in range(200):
                if req.cancel_requested():
                    raise AgentCancelled("已停止")
                Event().wait(0.005)
            pytest.fail("未观察到持久化控制意图")

    rt.declarative.executors.resolve_agent("claude-cli").executor = Agent()
    run = rt.declarative.submit(workflow(), "控制")
    results = []
    worker = Thread(target=lambda: results.append(rt.dispatcher.dispatch_once(run.run_id)))
    worker.start()
    try:
        assert entered.wait(2)
        rt.runs.control(run.run_id, action)
        worker.join(3)
        assert not worker.is_alive()
        assert results[0].status is (RunStatus.PAUSED if action == "pause" else RunStatus.CANCELLED)
        attempts = rt.store.list_attempts(run.run_id)
        assert attempts[0].status is (
            AttemptStatus.PAUSED if action == "pause" else AttemptStatus.CANCELLED
        )
        assert attempts[0].metadata["session_id"] == "session-1"
        assert rt.store.load_run(run.run_id).lease_owner is None
        if action == "pause":
            resumed = rt.runs.control(
                run.run_id,
                "resume",
                feedback="token=secret\n补充结论",
                mode="continue_conversation",
            )
            assert "secret" not in str(resumed.context)
            assert resumed.context["agent_resume"]["session_id"] == "session-1"

            class ResumedAgent:
                def execute(self, req):
                    assert req.session_id == "session-1"
                    assert "补充结论" in req.prompt
                    Path(req.workspace, "report.md").write_text("反馈后产物")
                    return AgentExecutionResult(
                        "完成", structured={"engine": "claude-cli", "session_id": "session-1"}
                    )

            rt.declarative.executors.resolve_agent("claude-cli").executor = ResumedAgent()
            assert rt.dispatcher.dispatch_once(run.run_id).status is RunStatus.SUCCEEDED
            assert [
                a.attempt_number
                for a in rt.store.list_attempts(run.run_id)
                if a.step_name == "write"
            ] == [1, 2]
    finally:
        if worker.is_alive():
            rt.runs.control(run.run_id, "cancel")
            worker.join(3)
        rt.close()


@pytest.mark.parametrize("has_session", [False, True])
def test_restart_recovers_only_matching_persisted_session(tmp_path, clock, ids, has_session):
    rt = runtime(tmp_path, clock, ids)
    run = rt.declarative.submit(workflow(), "崩溃恢复")
    claimed = rt.store.claim_run("dead-worker", clock.now(), 1, run.run_id)
    rt.declarative.prepare_workspace(claimed, None)
    definition = rt.declarative.parse(
        rt.declarative.definitions.read(run.run_id, run.context["workflow_definition"])[1]
    )
    current = rt.store.load_run(run.run_id)
    current.status = RunStatus.RUNNING
    rt.store.update_run(current)
    rt.store.save_attempt(
        StepAttempt(
            "interrupted",
            run.run_id,
            "write",
            1,
            input_snapshot={"job": redact(asdict(definition.jobs[0]))},
            metadata={"engine": "claude-cli", "session_id": "session-1" if has_session else None},
        )
    )
    rt.close()
    clock.value += 2
    commands = Mock(spec=["run"])

    def resumed(args, **kwargs):
        assert args[-2:] == ["--resume", "session-1"]
        Path(kwargs["cwd"], "report.md").write_text("恢复产物")
        return CommandResult(
            tuple(args),
            0,
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": "session-1",
                }
            ),
            "",
            0,
        )

    commands.run.side_effect = resumed
    rt = build_runtime(
        tmp_path / "config.yaml",
        commands=commands,
        clock=clock,
        sleeper=clock,
        ids=ids,
        console_logging=False,
    )
    try:
        result = rt.dispatcher.dispatch_once(run.run_id)
        if has_session:
            assert result.status is RunStatus.SUCCEEDED
            assert rt.store.list_attempts(run.run_id)[0].error_code == "PROCESS_INTERRUPTED"
            assert len(rt.store.list_attempts(run.run_id)) == 3
        else:
            assert result.status is RunStatus.PAUSED
            assert result.context["pause_point"]["reason"] == "recovery_requires_review"
            commands.run.assert_not_called()
            assert rt.dispatcher.dispatch_once(run.run_id) is None
            with pytest.raises(ConfigurationError):
                rt.runs.control(run.run_id, "resume", mode="continue_conversation")
    finally:
        rt.close()


def test_resume_failure_preserves_history_feedback_and_retry_session(tmp_path, clock, ids):
    from dtcoder_agentic_dev.domain.errors import TechnicalError

    rt = runtime(tmp_path, clock, ids)
    agent = rt.declarative.executors.resolve_agent("claude-cli")

    class Pause:
        def execute(self, req):
            req.on_event({"session_id": "session-1"})
            rt.runs.control(req.run_id, "pause")
            raise AgentCancelled("暂停")

    agent.executor = Pause()
    run = rt.declarative.submit(workflow(), "续聊故障")
    try:
        assert rt.dispatcher.dispatch_once(run.run_id).status is RunStatus.PAUSED
        rt.runs.control(run.run_id, "resume", feedback="保留反馈", mode="continue_conversation")

        class TemporaryFailure:
            def execute(self, req):
                assert req.session_id == "session-1"
                raise TechnicalError("临时连接故障")

        agent.executor = TemporaryFailure()
        failed = rt.dispatcher.dispatch_once(run.run_id)
        assert failed.status is RunStatus.FAILED
        before = rt.store.list_attempts(run.run_id)
        assert len(failed.context["feedback"]) == 1
        rt.runs.retry(run.run_id, mode="continue_conversation")

        class Complete:
            def execute(self, req):
                assert req.session_id == "session-1"
                assert "保留反馈" in req.prompt
                Path(req.workspace, "report.md").write_text("完成")
                return AgentExecutionResult("ok", structured={"session_id": "session-1"})

        agent.executor = Complete()
        assert rt.dispatcher.dispatch_once(run.run_id).status is RunStatus.SUCCEEDED
        assert rt.store.list_attempts(run.run_id)[: len(before)] == before
    finally:
        rt.close()


def test_prompt_template_is_frozen_and_language_selected(tmp_path, clock, ids):
    rt = runtime(tmp_path, clock, ids)
    try:
        data = yaml.safe_load(workflow())
        data["context"] = {"language": "en", "knowledge": ["notes.md"]}
        run = rt.declarative.submit(yaml.safe_dump(data), "任务需求")
        frozen = yaml.safe_load(
            rt.declarative.definitions.read(run.run_id, run.context["workflow_definition"])[1]
        )
        assert frozen["metadata"]["prompt_template"]["language"] == "en"
        (tmp_path / "prompts/agent_en.txt").write_text("后来修改的模板 {instructions} {context}")
        prompts = []

        class Agent:
            def execute(self, req):
                prompts.append(req.prompt)
                Path(req.workspace, "report.md").write_text("完成")
                return AgentExecutionResult("ok", structured={"session_id": "session-1"})

        rt.declarative.executors.resolve_agent("claude-cli").executor = Agent()
        assert rt.dispatcher.dispatch_once(run.run_id).status is RunStatus.SUCCEEDED
        assert "Task context" in prompts[0]
        assert "后来修改" not in prompts[0]
        assert "notes.md" in prompts[0] and "任务需求" in prompts[0]
    finally:
        rt.close()


@pytest.mark.parametrize("mismatch", ["engine", "definition"])
def test_recovery_refuses_wrong_engine_or_definition(tmp_path, clock, ids, mismatch):
    rt = runtime(tmp_path, clock, ids)
    try:
        run = rt.declarative.submit(workflow(), "身份核验")
        claimed = rt.store.claim_run("dead-worker", clock.now(), 1, run.run_id)
        rt.declarative.prepare_workspace(claimed, None)
        definition = rt.declarative.parse(
            rt.declarative.definitions.read(run.run_id, run.context["workflow_definition"])[1]
        )
        job = redact(asdict(definition.jobs[0]))
        if mismatch == "definition":
            job["prompt"] = "其他任务的指令"
        rt.store.save_attempt(
            StepAttempt(
                "interrupted",
                run.run_id,
                "write",
                1,
                input_snapshot={"job": job},
                metadata={
                    "session_id": "session-1",
                    "engine": "codex-cli" if mismatch == "engine" else "claude-cli",
                },
            )
        )
        clock.value += 2
        result = rt.dispatcher.dispatch_once(run.run_id)
        assert result.status is RunStatus.PAUSED
        assert result.context["pause_point"]["reason"] == "recovery_requires_review"
        rt.commands.run.assert_not_called()
    finally:
        rt.close()


def test_stale_worker_session_event_cannot_overwrite_new_owner(tmp_path, clock, ids):
    rt = runtime(tmp_path, clock, ids)

    class Agent:
        def execute(self, req):
            clock.value += 200
            rt.store.claim_run("other-worker", clock.now(), 100, req.run_id)
            req.on_event({"session_id": "stale-session"})
            pytest.fail("过期写入应被拒绝")

    rt.declarative.executors.resolve_agent("claude-cli").executor = Agent()
    try:
        run = rt.declarative.submit(workflow(), "并发")
        result = rt.dispatcher.dispatch_once(run.run_id)
        assert result.lease_owner == "other-worker"
        assert rt.store.list_attempts(run.run_id)[0].metadata.get("session_id") is None
        assert rt.store.list_attempts(run.run_id)[0].status is AttemptStatus.RUNNING
    finally:
        rt.close()


def test_orphan_process_blocks_recovery_and_manual_revise(tmp_path, clock, ids):
    from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict

    rt = runtime(tmp_path, clock, ids)
    try:
        run = rt.declarative.submit(workflow(), "孤儿保护")
        claimed = rt.store.claim_run("dead-worker", clock.now(), 1, run.run_id)
        rt.declarative.prepare_workspace(claimed, None)
        definition = rt.declarative.parse(
            rt.declarative.definitions.read(run.run_id, run.context["workflow_definition"])[1]
        )
        rt.store.save_attempt(
            StepAttempt(
                "orphan",
                run.run_id,
                "write",
                1,
                input_snapshot={"job": redact(asdict(definition.jobs[0]))},
                metadata={
                    "engine": "claude-cli",
                    "session_id": "session-1",
                    "process": {"pid": 123, "process_group": 123},
                },
            )
        )
        rt.declarative.process_is_alive = lambda process: True
        clock.value += 2
        assert rt.dispatcher.dispatch_once(run.run_id).status is RunStatus.PAUSED
        assert (
            rt.store.load_run(run.run_id).context["pause_point"]["reason"]
            == "orphan_process_active"
        )
        with pytest.raises(ConcurrencyConflict):
            rt.runs.control(run.run_id, "resume", mode="revise")
        rt.commands.run.assert_not_called()
        rt.declarative.process_is_alive = lambda process: False
        assert rt.runs.control(run.run_id, "resume", mode="revise").status is RunStatus.QUEUED
    finally:
        rt.close()


def test_selected_model_default_is_frozen(tmp_path, clock, ids):
    rt = runtime(tmp_path, clock, ids)
    try:
        rt.declarative.executors.resolve_agent("claude-cli").default_model = "sonnet"
        run = rt.declarative.submit(workflow(), "固定模型")
        frozen = yaml.safe_load(
            rt.declarative.definitions.read(run.run_id, run.context["workflow_definition"])[1]
        )
        assert frozen["jobs"]["write"]["model"] == "sonnet"
    finally:
        rt.close()

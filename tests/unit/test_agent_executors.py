"""多引擎契约：模型服务使用替身，子进程仅运行临时 Python 脚本。"""

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dtcoder_agentic_dev.adapters.agents.cli import GenericCLIExecutor
from dtcoder_agentic_dev.adapters.claude.cli import ClaudeCLIExecutor
from dtcoder_agentic_dev.adapters.claude.sdk import ClaudeSDKExecutor
from dtcoder_agentic_dev.application.agent_runtime import AgentRouter
from dtcoder_agentic_dev.config import AgentConfig, ClaudeConfig, load_config
from dtcoder_agentic_dev.domain.errors import (
    AgentCancelled,
    CapabilityNotConfigured,
    ConfigurationError,
    ExternalCommandTimeout,
    SessionLost,
    TechnicalError,
)
from dtcoder_agentic_dev.infrastructure.process.agent_manager import AgentManager
from dtcoder_agentic_dev.infrastructure.process.cancellable import CancellableCommandRunner
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionRequest, AgentExecutionResult
from dtcoder_agentic_dev.ports.command_runner import CommandResult


@pytest.fixture
def agent_request(tmp_path):
    return AgentExecutionRequest("run", "write", 1, str(tmp_path), "写文件", allow_no_repo=True)


def terminal(**updates):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "session-1",
        "result": "已完成",
        **updates,
    }


def cli(tmp_path, data=None, returncode=0):
    commands = Mock(spec=["run"])
    commands.run.return_value = CommandResult((), returncode, json.dumps(data or terminal()), "", 1)
    return ClaudeCLIExecutor(ClaudeConfig(), commands, str(tmp_path / "logs")), commands


def test_claude_first_and_resume_parameters_and_logs(tmp_path, agent_request):
    executor, commands = cli(tmp_path)
    executor.config.allowed_tools = ["Read", "Write"]
    first = executor.execute(replace(agent_request, model="sonnet", timeout=10))
    assert first.structured["engine"] == "claude-cli"
    assert first.structured["session_id"] == "session-1"
    args = commands.run.call_args.args[0]
    assert args == [
        "claude",
        "--print",
        "--output-format",
        "json",
        "--max-turns",
        "20",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read",
        "Write",
        "--model",
        "sonnet",
    ]
    assert commands.run.call_args.kwargs["stdin"] == "写文件"
    executor.execute(replace(agent_request, attempt_number=2, session_id="session-1"))
    assert commands.run.call_args.args[0][-2:] == ["--resume", "session-1"]
    assert commands.run.call_args.kwargs["cwd"] == agent_request.workspace


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"type": "assistant"},
        terminal(is_error=True),
        terminal(subtype="error_max_turns"),
        terminal(is_error="false"),
    ],
)
def test_claude_never_reports_malformed_or_error_as_success(tmp_path, agent_request, data):
    executor, commands = cli(tmp_path)
    commands.run.return_value = CommandResult((), 0, json.dumps(data), "", 0)
    with pytest.raises(TechnicalError):
        executor.execute(agent_request)


def test_claude_session_lost_separate_from_temporary_failure(tmp_path, agent_request):
    executor, commands = cli(tmp_path)
    commands.run.return_value = CommandResult(
        (), 1, "", "No conversation found with session ID: session-1", 0
    )
    with pytest.raises(SessionLost):
        executor.execute(replace(agent_request, session_id="session-1"))
    commands.run.return_value = CommandResult((), 1, "", "Connection refused token=secret", 0)
    with pytest.raises(TechnicalError) as raised:
        executor.execute(replace(agent_request, session_id="session-1"))
    assert not isinstance(raised.value, SessionLost)
    assert "secret" not in str(raised.value)
    assert "secret" not in (tmp_path / "logs/run/write/attempt-1/stderr.txt").read_text()


def test_stream_result_and_session_events(tmp_path, agent_request):
    executor, commands = cli(tmp_path)
    executor.config.output_format = "stream-json"
    events = []
    commands.run.return_value = CommandResult(
        (),
        0,
        "\n".join(
            json.dumps(x)
            for x in [{"type": "system", "subtype": "init", "session_id": "session-1"}, terminal()]
        ),
        "",
        0,
    )
    result = executor.execute(replace(agent_request, on_event=events.append))
    assert result.structured["session_id"] == "session-1"
    assert events[0]["session_id"] == "session-1"
    assert "--verbose" in commands.run.call_args.args[0]


def test_config_legacy_and_explicit_default_unknown_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    assert load_config(path).agents.default_engine == "codex-cli"
    path.write_text("agents: {default_engine: claude-cli}\nclaude: {max_turns: 2}")
    assert load_config(path).claude.max_turns == 2
    for text in [
        "agents: {default_engine: missing}",
        "claude: {max_turns: 0}",
        "claude: {timeout: .inf}",
        "claude: {permission_mode: missing}",
        "agents: {model_routes: {model: missing}}",
        "claude: {sdk_fallback: yes_please}",
    ]:
        path.write_text(text)
        with pytest.raises(ConfigurationError):
            load_config(path)


def test_router_deterministic_model_and_no_fallback(agent_request):
    claude, codex = Mock(), Mock()
    router = AgentRouter(
        AgentConfig(model_routes={"gpt-test": "codex-cli"}),
        {"claude-cli": claude, "codex-cli": codex},
    )
    router.execute(agent_request)
    claude.execute.assert_called_once()
    router.execute(replace(agent_request, model="gpt-test"))
    codex.execute.assert_called_once()
    with pytest.raises(ConfigurationError):
        router.resolve("unknown")


def test_sdk_stream_resume_and_explicit_missing_sdk_fallback(tmp_path, agent_request):
    seen = []

    async def query(*, prompt, options):
        seen.append((prompt, options))
        yield SimpleNamespace(subtype="init", data={"session_id": "session-1"})
        yield SimpleNamespace(**{k: v for k, v in terminal().items() if k != "type"})

    module = SimpleNamespace(ClaudeAgentOptions=lambda **kw: kw, query=query)
    executor = ClaudeSDKExecutor(ClaudeConfig(), str(tmp_path / "logs"), sdk_loader=lambda: module)
    events = []
    result = asyncio.run(
        executor.execute_async(
            replace(agent_request, session_id="session-1", on_event=events.append)
        )
    )
    assert result.structured["engine"] == "claude-sdk"
    assert seen[0][1]["resume"] == "session-1"
    assert events[0]["session_id"] == "session-1"

    def missing():
        raise ImportError("not installed")

    executor = ClaudeSDKExecutor(ClaudeConfig(), str(tmp_path / "logs"), sdk_loader=missing)
    with pytest.raises(CapabilityNotConfigured):
        asyncio.run(executor.execute_async(agent_request))
    fallback, _ = cli(tmp_path)
    executor = ClaudeSDKExecutor(
        ClaudeConfig(sdk_fallback=True),
        str(tmp_path / "logs"),
        sdk_loader=missing,
        fallback=fallback,
    )
    result = asyncio.run(executor.execute_async(agent_request))
    assert result.structured["engine"] == "claude-cli"
    assert result.structured["fallback_reason"] == "SDK_UNAVAILABLE"


def test_sdk_error_does_not_fallback(tmp_path, agent_request):
    async def query(**kwargs):
        yield SimpleNamespace(
            subtype="error_during_execution", is_error=True, session_id="session-1", result="bad"
        )

    fallback = Mock()
    executor = ClaudeSDKExecutor(
        ClaudeConfig(sdk_fallback=True),
        str(tmp_path / "logs"),
        sdk_loader=lambda: SimpleNamespace(ClaudeAgentOptions=lambda **kw: kw, query=query),
        fallback=fallback,
    )
    with pytest.raises(TechnicalError):
        asyncio.run(executor.execute_async(agent_request))
    fallback.execute.assert_not_called()


def test_generic_cli_real_file_and_timeout_cancel(tmp_path, agent_request):
    runner = CancellableCommandRunner()
    executor = GenericCLIExecutor(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; Path('out.txt').write_text(sys.stdin.read()); print('ok')",
        ],
        runner,
        str(tmp_path / "logs"),
    )
    result = executor.execute(agent_request)
    assert result.stdout.strip() == "ok"
    assert (tmp_path / "out.txt").read_text() == "写文件"
    sleeper = GenericCLIExecutor(
        [sys.executable, "-c", "import time; time.sleep(5)"], runner, str(tmp_path / "logs")
    )
    with pytest.raises(ExternalCommandTimeout):
        sleeper.execute(replace(agent_request, timeout=0.05))
    with pytest.raises(AgentCancelled):
        sleeper.execute(replace(agent_request, cancel_requested=lambda: True))


def test_manager_lifecycle_duplicate_cancel_timeout_and_failure(agent_request):
    class Agent:
        async def execute_async(self, req):
            if req.prompt == "bad":
                raise RuntimeError("token=secret")
            while not req.cancel_requested():
                await asyncio.sleep(0.005)
            raise AgentCancelled("已取消")

    manager = AgentManager()
    with pytest.raises(ConfigurationError):
        manager.submit(Agent(), agent_request)
    manager.start()
    future = manager.submit(Agent(), agent_request)
    with pytest.raises(ConfigurationError):
        manager.submit(Agent(), agent_request)
    manager.cancel(agent_request.run_id)
    assert future.result(timeout=2).status.value == "CANCELLED"
    assert (
        manager.submit(Agent(), replace(agent_request, attempt_number=2, timeout=0.02))
        .result(timeout=2)
        .status.value
        == "TIMED_OUT"
    )
    failed = manager.submit(Agent(), replace(agent_request, attempt_number=3, prompt="bad")).result(
        timeout=2
    )
    assert failed.status.value == "FAILED" and "secret" not in str(failed)
    manager.stop()
    assert manager.running is False


def test_sdk_cancel_and_timeout_close_stream(tmp_path, agent_request):
    closed = []

    async def query(**kwargs):
        try:
            yield SimpleNamespace(subtype="init", data={"session_id": "session-1"})
            await asyncio.sleep(5)
        finally:
            closed.append(True)

    module = SimpleNamespace(ClaudeAgentOptions=lambda **kw: kw, query=query)
    executor = ClaudeSDKExecutor(ClaudeConfig(), str(tmp_path / "logs"), sdk_loader=lambda: module)
    with pytest.raises(ExternalCommandTimeout):
        asyncio.run(executor.execute_async(replace(agent_request, timeout=0.01)))
    with pytest.raises(AgentCancelled):
        asyncio.run(executor.execute_async(replace(agent_request, cancel_requested=lambda: True)))
    assert closed


def test_cancellable_stream_session_arrives_before_process_finishes(tmp_path, agent_request):
    from threading import Event

    observed = Event()
    executor = ClaudeCLIExecutor(
        ClaudeConfig(binary=sys.executable, output_format="stream-json"),
        CancellableCommandRunner(),
        str(tmp_path / "logs"),
    )
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json,time\nprint(json.dumps({'type':'system','session_id':'session-1'}),flush=True)\ntime.sleep(5)\n"
    )
    executor.build_command = lambda req: [sys.executable, str(script)]
    with pytest.raises(AgentCancelled):
        executor.execute(
            replace(
                agent_request,
                on_event=lambda event: observed.set(),
                cancel_requested=observed.is_set,
            )
        )
    assert observed.is_set()


def test_codex_resume_parameters_and_invalid_session(tmp_path, agent_request):
    from dtcoder_agentic_dev.adapters.codex.adapter import CodexAdapter
    from dtcoder_agentic_dev.config import CodexConfig

    commands = Mock(spec=["run"])
    commands.run.return_value = CommandResult((), 0, '{"type":"turn.completed"}', "", 0)
    executor = CodexAdapter(CodexConfig(output_format="json"), commands, str(tmp_path / "logs"))
    result = executor.execute(replace(agent_request, session_id="session-1", model="gpt-test"))
    assert commands.run.call_args.args[0] == [
        "codex",
        "exec",
        "resume",
        "--json",
        "--model",
        "gpt-test",
        "--skip-git-repo-check",
        "session-1",
        "-",
    ]
    assert result.structured["session_id"] == "session-1"
    for session in ["--last", "../outside", "", "id with space"]:
        with pytest.raises(ConfigurationError):
            executor.execute(replace(agent_request, session_id=session))


def test_manager_stop_waits_for_cooperative_reclamation(agent_request):
    finished = []

    class Agent:
        async def execute_async(self, req):
            while not req.cancel_requested():
                await asyncio.sleep(0.005)
            finished.append(True)
            raise AgentCancelled("停止")

    manager = AgentManager()
    manager.start()
    future = manager.submit(Agent(), agent_request)
    manager.stop()
    assert future.result().status.value == "CANCELLED"
    assert not manager.thread.is_alive()


def test_claude_path_escape_rejected(tmp_path, agent_request):
    executor, commands = cli(tmp_path)
    with pytest.raises(TechnicalError):
        executor.execute(replace(agent_request, run_id="../outside"))
    # 日志路径检查应在启动模型前完成。
    commands.run.assert_not_called()


def test_sdk_terminal_abort_is_not_success(tmp_path, agent_request):
    async def query(**kwargs):
        yield SimpleNamespace(
            **{k: v for k, v in terminal(terminal_reason="aborted_tools").items() if k != "type"}
        )

    executor = ClaudeSDKExecutor(
        ClaudeConfig(),
        str(tmp_path / "logs"),
        sdk_loader=lambda: SimpleNamespace(ClaudeAgentOptions=lambda **kw: kw, query=query),
    )
    with pytest.raises(AgentCancelled):
        asyncio.run(executor.execute_async(agent_request))


def test_manager_failure_returncode_and_concurrency_are_real(agent_request):
    from threading import Event

    entered = Event()
    active, peak = 0, 0

    class Agent:
        async def execute_async(self, req):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            while not req.cancel_requested():
                await asyncio.sleep(0.005)
            active -= 1
            return AgentExecutionResult("失败", returncode=1)

    manager = AgentManager(1)
    manager.start()
    try:
        first = manager.submit(Agent(), agent_request)
        assert entered.wait(2)
        second = manager.submit(Agent(), replace(agent_request, attempt_number=2, timeout=0.01))
        manager.cancel(agent_request.run_id)
        assert first.result(timeout=2).status.value != "SUCCEEDED"
        assert second.result(timeout=2).status.value != "SUCCEEDED"
        assert peak == 1
    finally:
        manager.stop()


def test_prompt_templates_preserve_user_copy_and_reject_invalid_fields(tmp_path):
    from dtcoder_agentic_dev.application.services.initialization import initialize
    from dtcoder_agentic_dev.prompts.builder import TaskPromptBuilder

    initialize(tmp_path / "config.yaml")
    path = tmp_path / "prompts/agent_zh.txt"
    path.write_text("用户模板 {instructions}\n{context}")
    initialize(tmp_path / "config.yaml")
    assert path.read_text().startswith("用户模板")
    builder = TaskPromptBuilder(path.parent)
    assert "用户模板" in builder.build("需求", {}, builder.freeze("zh"))
    for text in [
        "{instructions.foo} {context}",
        "仅有 {instructions}",
        "{instructions!r} {context}",
    ]:
        path.write_text(text)
        with pytest.raises(ConfigurationError):
            builder.freeze("zh")
    with pytest.raises(ConfigurationError):
        builder.freeze("unknown")


def test_doctor_checks_selected_engine_without_model_requests(tmp_path):
    from dtcoder_agentic_dev.application.services.diagnostics import diagnose
    from dtcoder_agentic_dev.application.services.initialization import initialize
    from dtcoder_agentic_dev.cli.bootstrap import build_runtime

    path = initialize(tmp_path / "config.yaml")
    commands = Mock(spec=["run"])
    commands.run.return_value = CommandResult((), 0, "已认证", "", 0)
    runtime = build_runtime(path, commands=commands, console_logging=False)
    try:
        results = diagnose(runtime)
        calls = [call.args[0] for call in commands.run.call_args_list]
        assert ["claude", "auth", "status"] in calls
        assert not any("--print" in args or "exec" in args for args in calls)
        assert next(d for d in results if d.component == "Codex").status == "未启用"
    finally:
        runtime.close()


def test_cancel_kills_only_owned_process_group(tmp_path, agent_request):
    import os
    import subprocess

    from dtcoder_agentic_dev.infrastructure.process.ownership import process_is_alive

    if os.name != "posix":
        pytest.skip("进程组隔离测试仅适用于 POSIX")
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"], start_new_session=True
    )
    records = []
    script = tmp_path / "tree.py"
    script.write_text(
        "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'])\nprint('started',flush=True)\ntime.sleep(5)\n"
    )
    runner = CancellableCommandRunner()
    try:
        with pytest.raises(ExternalCommandTimeout):
            runner.run_cancellable(
                [sys.executable, str(script)], timeout=0.1, on_process=records.append
            )
        assert unrelated.poll() is None
        assert records[0]["pid"] == records[0]["process_group"]
        # 被回收的主进程不得继续存活；不对无归属进程使用模糊匹配。
        with pytest.raises(ProcessLookupError):
            os.kill(records[0]["pid"], 0)
        assert process_is_alive({"pid": 0}) is True
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=2)

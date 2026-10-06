import json
import subprocess
import sys

import pytest

from dtcoder_agentic_dev.adapters.codex.adapter import CodexAdapter
from dtcoder_agentic_dev.config import CodexConfig
from dtcoder_agentic_dev.domain.errors import (
    ExternalCommandError,
    ExternalCommandTimeout,
    TechnicalError,
)
from dtcoder_agentic_dev.engine.steps.development import parse_review_summary
from dtcoder_agentic_dev.infrastructure.process.command import SubprocessCommandRunner
from dtcoder_agentic_dev.ports.agent_executor import AgentExecutionRequest
from dtcoder_agentic_dev.ports.command_runner import CommandResult
from dtcoder_agentic_dev.prompts.renderer import StrictPromptRenderer


def test_command_success_stdin_env_and_no_shell():
    result = SubprocessCommandRunner().run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"], stdin="中文", timeout=5
    )
    assert result.returncode == 0 and result.stdout.strip() == "中文"
    assert result.duration_seconds >= 0


def test_command_errors_and_timeout(mocker):
    mocked = mocker.patch(
        "subprocess.run", return_value=subprocess.CompletedProcess(["x"], 3, "", "error")
    )
    runner = SubprocessCommandRunner()
    assert runner.run(["x"], check=False).returncode == 3
    with pytest.raises(ExternalCommandError):
        runner.run(["x"])
    assert mocked.call_args.kwargs["shell"] is False
    mocked.side_effect = subprocess.TimeoutExpired(["x"], 1)
    with pytest.raises(ExternalCommandTimeout):
        runner.run(["x"], timeout=1)
    with pytest.raises(ValueError):
        runner.run("unsafe")


def test_prompt_strict(tmp_path):
    renderer = StrictPromptRenderer(str(tmp_path))
    with pytest.raises(TechnicalError, match=str(tmp_path / "missing.txt")):
        renderer.render("missing", {})
    (tmp_path / "a.txt").write_text('{name} {{"json": true}}', encoding="utf-8")
    with pytest.raises(TechnicalError, match="name"):
        renderer.render("a", {})
    result = renderer.render("a", {"name": "测试", "extra": 1})
    assert result.text == '测试 {"json": true}' and len(result.sha256) == 64
    (tmp_path / "a.txt").write_text("{name.__class__}", encoding="utf-8")
    with pytest.raises(TechnicalError):
        renderer.render("a", {"name": "x"})


@pytest.mark.parametrize(
    "value",
    [
        "```json\n{}\n```",
        "[]",
        "{}",
        '{"passed":1,"round":1,"blockers":[],"majors":[],"minors":[]}',
        '{"passed":true,"round":true,"blockers":[],"majors":[],"minors":[]}',
        '{"passed":true,"round":2,"blockers":[],"majors":[],"minors":[]}',
        '{"passed":true,"round":1,"blockers":"bad","majors":[],"minors":[]}',
        '{"passed":true,"round":1,"blockers":[4],"majors":[],"minors":[]}',
    ],
)
def test_strict_summary_invalid(value):
    with pytest.raises(TechnicalError):
        parse_review_summary(value, 1)


def test_strict_summary_valid():
    data = {"passed": True, "round": 1, "blockers": ["阻断"], "majors": [], "minors": []}
    assert parse_review_summary(json.dumps(data), 1)["blockers"] == ["阻断"]


def test_codex_command_logs_and_parse(tmp_path, mocker):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: fixture", encoding="utf-8")
    commands = mocker.Mock()
    commands.run.return_value = CommandResult(("codex",), 0, '{"ok":true}', "诊断", 2)
    adapter = CodexAdapter(
        CodexConfig(
            binary="custom", extra_args=["--model", "test"], output_format="json", timeout=99
        ),
        commands,
        str(tmp_path / "logs"),
    )
    request = AgentExecutionRequest("run", "coding", 1, str(workspace), "提示词")
    result = adapter.execute(request)
    assert adapter.build_command() == ["custom", "exec", "--model", "test", "--json", "-"]
    assert result.structured == {"ok": True}
    assert commands.run.call_args.kwargs["stdin"] == "提示词"
    assert commands.run.call_args.kwargs["cwd"] == str(workspace)
    assert (tmp_path / "logs/run/coding/attempt-1/prompt.txt").read_text(
        encoding="utf-8"
    ) == "提示词"
    assert (
        json.loads(
            (tmp_path / "logs/run/coding/attempt-1/execution.json").read_text(encoding="utf-8")
        )["duration_seconds"]
        == 2
    )
    commands.run.return_value = CommandResult((), 1, "", "", 0)
    with pytest.raises(ExternalCommandError):
        adapter.execute(request)
    commands.run.return_value = CommandResult((), 0, "bad", "", 0)
    with pytest.raises(TechnicalError):
        adapter.execute(request)
    commands.run.return_value = CommandResult(
        (), 0, '{"type":"item.completed"}\n{"type":"turn.completed"}', "", 0
    )
    assert len(adapter.execute(request).structured["events"]) == 2
    commands.run.side_effect = ExternalCommandTimeout("超时")
    with pytest.raises(ExternalCommandTimeout):
        adapter.execute(request)
    assert "ExternalCommandTimeout" in (
        tmp_path / "logs/run/coding/attempt-1/execution.json"
    ).read_text(encoding="utf-8")


def test_summary_rejects_duplicate_keys_and_nonstandard_numbers():
    duplicate = '{"passed":true,"passed":false,"round":1,"blockers":[],"majors":[],"minors":[]}'
    nonfinite = '{"passed":true,"round":1,"blockers":[{"line":NaN}],"majors":[],"minors":[]}'
    for text in (duplicate, nonfinite):
        with pytest.raises(TechnicalError):
            parse_review_summary(text, 1)


def test_timeout_retains_partial_output(mocker):
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            ["fixture"], 1, output="部分输出".encode("utf-8"), stderr=b"partial-error"
        ),
    )
    ticks = iter([1, 3])
    with pytest.raises(ExternalCommandTimeout) as raised:
        SubprocessCommandRunner(lambda: next(ticks)).run(["fixture"])
    assert raised.value.stdout == "部分输出"
    assert raised.value.stderr == "partial-error"
    assert raised.value.duration_seconds == 2


def test_empty_template_rejected(tmp_path):
    (tmp_path / "empty.txt").write_text("  \n", encoding="utf-8")
    with pytest.raises(TechnicalError, match="模板为空"):
        StrictPromptRenderer(str(tmp_path)).render("empty", {})


def test_codex_yaml_request_no_repo_model_timeout_and_redacted_logs(tmp_path, mocker):
    workspace = tmp_path / "no-repo"
    workspace.mkdir()
    commands = mocker.Mock()
    commands.run.return_value = CommandResult(
        (),
        0,
        '{"type":"thread.started","thread_id":"session-id"}\n{"type":"turn.completed"}',
        "token=private-secret",
        1,
    )
    adapter = CodexAdapter(CodexConfig(output_format="json"), commands, str(tmp_path / "logs"))
    request = AgentExecutionRequest(
        "run",
        "write",
        1,
        str(workspace),
        "password=private-secret",
        model="selected",
        timeout=3,
        allow_no_repo=True,
    )
    result = adapter.execute(request)
    assert result.structured["session_id"] == "session-id"
    assert commands.run.call_args.args[0] == [
        "codex",
        "exec",
        "--json",
        "--model",
        "selected",
        "--skip-git-repo-check",
        "-",
    ]
    assert commands.run.call_args.kwargs["timeout"] == 3
    for path in (tmp_path / "logs").rglob("*.txt"):
        assert "private-secret" not in path.read_text()

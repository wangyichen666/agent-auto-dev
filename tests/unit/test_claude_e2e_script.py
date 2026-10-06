"""真实验收脚本的离线分支检查；不调用本地模型。"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dtcoder_agentic_dev.domain.models import AttemptStatus, RunStatus


@pytest.mark.parametrize("case", ["basic", "timeout"])
def test_cli_success_does_not_use_timeout_assertion(tmp_path, monkeypatch, case):
    spec = importlib.util.spec_from_file_location(
        "claude_e2e_script", Path(__file__).parents[2] / "scripts/e2e_claude.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    root = tmp_path / "验收"
    monkeypatch.setattr(sys, "argv", ["e2e_claude.py", "--case", case, "--directory", str(root)])
    monkeypatch.setattr(script.shutil, "which", lambda name: "fake-claude")
    status = RunStatus.SUCCEEDED if case == "basic" else RunStatus.FAILED
    run = SimpleNamespace(run_id="run", status=status, last_error=None, lease_owner=None)
    attempt = SimpleNamespace(
        attempt_number=1,
        step_name="write",
        status=AttemptStatus(status.value),
        error_code=None if case == "basic" else "TIMEOUT",
        metadata={},
    )
    runtime = Mock()
    runtime.store.list_runs.return_value = [run]
    runtime.store.load_run.return_value = run
    runtime.store.list_attempts.return_value = [attempt]
    runtime.declarative.submit.return_value = run
    runtime.dispatcher.dispatch_once.return_value = run
    monkeypatch.setattr(script, "build_runtime", lambda *args, **kwargs: runtime)
    invoked = Mock()
    invoked.invoke.return_value = SimpleNamespace(exit_code=0, output="成功")
    monkeypatch.setattr(script, "CliRunner", lambda: invoked)
    script.main()
    assert json.loads((root / "summary.json").read_text())["status"] == "PASSED"
    if case == "basic":
        invoked.invoke.assert_called_once()
        runtime.declarative.submit.assert_not_called()
    else:
        invoked.invoke.assert_not_called()
        runtime.declarative.submit.assert_called_once()
    runtime.close.assert_called_once()


def test_live_script_refuses_nonempty_user_directory(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "claude_e2e_script", Path(__file__).parents[2] / "scripts/e2e_claude.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    existing = tmp_path / "保留文件.txt"
    existing.write_text("用户内容")
    monkeypatch.setattr(sys, "argv", ["e2e_claude.py", "--directory", str(tmp_path)])
    initialized = Mock()
    monkeypatch.setattr(script, "initialize", initialized)
    with pytest.raises(AssertionError, match="空目录"):
        script.main()
    assert existing.read_text() == "用户内容"
    initialized.assert_not_called()

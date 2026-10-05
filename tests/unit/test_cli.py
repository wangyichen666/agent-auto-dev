import pytest
import yaml
from click.testing import CliRunner

from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.cli.app import app


@pytest.mark.parametrize("args", [["--help"], ["init", "--help"], ["--version"]])
def test_help_without_init(args, tmp_path):
    result = CliRunner().invoke(app, ["--config", str(tmp_path / "missing.yaml"), *args])
    assert result.exit_code == 0
    assert "Traceback" not in result.output


def test_missing_config_clear_error(tmp_path):
    result = CliRunner().invoke(app, ["--config", str(tmp_path / "missing.yaml"), "doctor"])
    assert result.exit_code != 0 and "请先执行 init" in result.output


def initialize(tmp_path):
    path = tmp_path / "config.yaml"
    result = CliRunner().invoke(app, ["--config", str(path), "init"])
    assert result.exit_code == 0, result.output
    return path


def test_init_idempotent_and_creates_database(tmp_path):
    path = initialize(tmp_path)
    config = path.read_text(encoding="utf-8") + "\n# 用户修改\n"
    path.write_text(config, encoding="utf-8")
    template = tmp_path / "prompts/requirements.txt"
    template.write_text("自定义模板", encoding="utf-8")
    result = CliRunner().invoke(app, ["--config", str(path), "init"])
    assert result.exit_code == 0
    assert template.read_text(encoding="utf-8") == "自定义模板"
    assert path.read_text(encoding="utf-8") == config
    assert (tmp_path / "state.db").exists() and (tmp_path / "logs/scheduler.log").exists()


def test_doctor_diagnosis_no_real_external_calls(tmp_path, mocker):
    path = initialize(tmp_path)
    mocker.patch("dtcoder_agentic_dev.infrastructure.process.command.SubprocessCommandRunner.run")
    result = CliRunner().invoke(app, ["--config", str(path), "doctor"])
    assert result.exit_code != 0
    assert "未配置真实 CodeHost" in result.output and "Traceback" not in result.output


def test_list_show_and_state_controls(tmp_path, run):
    path = initialize(tmp_path)
    store = SQLiteRunRepository(tmp_path / "state.db")
    store.create_run(run)
    store.close()
    cli = CliRunner()
    assert "run-1" in cli.invoke(app, ["--config", str(path), "list"]).output
    result = cli.invoke(app, ["--config", str(path), "show", "--run", run.run_id])
    assert result.exit_code == 0 and "attempts" in result.output
    assert cli.invoke(app, ["--config", str(path), "pause", "--run", run.run_id]).exit_code == 0
    assert cli.invoke(app, ["--config", str(path), "pause", "--run", run.run_id]).exit_code != 0
    assert cli.invoke(app, ["--config", str(path), "cleanup", "--run", run.run_id]).exit_code != 0
    assert cli.invoke(app, ["--config", str(path), "resume", "--run", run.run_id]).exit_code == 0
    assert cli.invoke(app, ["--config", str(path), "cancel", "--run", run.run_id]).exit_code == 0
    assert "run-1" not in cli.invoke(app, ["--config", str(path), "list"]).output
    assert "run-1" in cli.invoke(app, ["--config", str(path), "list", "--all"]).output
    assert cli.invoke(app, ["--config", str(path), "show", "--run", "missing"]).exit_code != 0


def test_dry_run_process_queue_no_git_or_codex(tmp_path, mocker):
    path = initialize(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["dry_run"] = True
    data["repositories"] = [{"name": "sample", "remote": "https://example.invalid/sample"}]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    external = mocker.patch(
        "dtcoder_agentic_dev.infrastructure.process.command.SubprocessCommandRunner.run"
    )
    result = CliRunner().invoke(
        app, ["--config", str(path), "process", "--repo", "sample", "--issue", "1"]
    )
    assert result.exit_code == 0 and "QUEUED" in result.output
    assert CliRunner().invoke(app, ["--config", str(path), "run-once"]).exit_code == 0
    external.assert_not_called()
    assert CliRunner().invoke(app, ["--config", str(path), "config", "show"]).exit_code == 0

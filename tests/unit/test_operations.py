import json
import signal
from dataclasses import asdict
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.application.services.initialization import initialize
from dtcoder_agentic_dev.application.services.rollback import RollbackService
from dtcoder_agentic_dev.application.services.runs import RunService
from dtcoder_agentic_dev.cli.app import app
from dtcoder_agentic_dev.cli.bootstrap import build_runtime
from dtcoder_agentic_dev.config import (
    AppConfig,
    RepoConfig,
    StateConfig,
    WorkspaceConfig,
    load_config,
    redacted_config,
)
from dtcoder_agentic_dev.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    TechnicalError,
)
from dtcoder_agentic_dev.domain.models import (
    Artifact,
    AttemptStatus,
    ExternalOperation,
    OperationStatus,
    RunStatus,
    StepAttempt,
)
from dtcoder_agentic_dev.infrastructure.locking.file import RunExecutionGuard
from dtcoder_agentic_dev.infrastructure.process.daemon import DaemonManager
from dtcoder_agentic_dev.ports.code_host import PullRequest
from dtcoder_agentic_dev.ports.command_runner import CommandResult
from dtcoder_agentic_dev.ports.pipeline import PipelineResult, PipelineStatus


def test_init_add_update_templates_and_backup(tmp_path):
    path = tmp_path / "config.yaml"
    cli = CliRunner()
    options = ["--remote", "https://example.invalid/r", "--project", "p", "--provider", "antcode"]
    first = cli.invoke(
        app, ["--config", str(path), "init", "--name", "first", *options, "--reviewer", "1"]
    )
    assert first.exit_code == 0, first.output
    (tmp_path / "comments/run_started.txt").write_text("自定义")
    update = cli.invoke(
        app, ["--config", str(path), "init", "--name", "renamed", *options, "--reviewer", "2"]
    )
    assert update.exit_code == 0, update.output
    config = load_config(path)
    assert len(config.repositories) == 1 and config.repositories[0].name == "renamed"
    assert config.repositories[0].reviewers == ["2"]
    assert (tmp_path / "comments/run_started.txt").read_text() == "自定义"
    initialize(path, update_templates=True)
    backups = list((tmp_path / "comments").glob("run_started.txt.backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == "自定义"
    result = cli.invoke(
        app,
        [
            "--config",
            str(path),
            "init",
            "--name",
            "second",
            "--remote",
            "https://example.invalid/second",
        ],
    )
    assert result.exit_code == 0 and len(load_config(path).repositories) == 2


def test_init_pipeline_parameters_and_invalid_update_atomic(tmp_path):
    path = tmp_path / "config.yaml"
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(path),
            "init",
            "--name",
            "r",
            "--remote",
            "https://example.invalid/r",
            "--project",
            "1",
            "--code-host-adapter",
            "antcode",
            "--pipeline-enabled",
            "--pipeline-adapter",
            "aci",
            "--pipeline-project",
            "2",
            "--yml-path",
            ".aci.yml",
            "--param",
            "env=pre",
        ],
    )
    assert result.exit_code == 0, result.output
    config = load_config(path)
    assert config.repositories[0].pipeline.params == {"env": "pre"}
    before = path.read_text()
    with pytest.raises(ConfigurationError):
        initialize(
            path, repository={"name": "bad", "remote": "https://token:secret@example.invalid/r"}
        )
    assert path.read_text() == before


@pytest.mark.parametrize(
    "ci",
    [
        {"project": "1"},
        {"project": "1", "yml_path": "a", "template_id": "t"},
        {"yml_path": "a"},
        {"project": "1", "yml_path": "../a"},
        {"project": "1", "yml_path": "a", "branch": "../x"},
        {"project": "1", "yml_path": "a", "params": {"bad=key": "value"}},
        {"project": "1", "yml_path": "a", "params": {"env": 3}},
        {"project": "1", "yml_path": "a", "extra": True},
    ],
)
def test_pipeline_config_validation(tmp_path, ci):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "pipeline": {"adapter": "aci"},
                "repositories": [
                    {"name": "r", "remote": "remote", "pipeline_enabled": True, "pipeline": ci}
                ],
            }
        )
    )
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_pipeline_paths_resolve_and_redact(tmp_path):
    for name in ["pipeline.yaml", "env"]:
        (tmp_path / name).write_text("value")
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "pipeline": {"adapter": "aci"},
                "notification": {"api_url": "https://example.invalid/api"},
                "code_host": {"profile": "private"},
                "repositories": [
                    {
                        "name": "r",
                        "remote": "remote",
                        "pipeline_enabled": True,
                        "reviewers": ["a", "", " a ", "b"],
                        "pipeline": {
                            "project": "1",
                            "yaml_file": "pipeline.yaml",
                            "env_file": "env",
                            "params": {"password": "secret"},
                        },
                    }
                ],
            }
        )
    )
    config = load_config(path)
    assert config.repositories[0].pipeline.yaml_file == str(tmp_path / "pipeline.yaml")
    assert config.repositories[0].reviewers == ["a", "b"]
    assert (
        "secret" not in str(redacted_config(config))
        and redacted_config(config)["code_host"]["profile"] == "<已隐藏>"
    )


def test_dry_run_real_adapters_never_call_external(tmp_path):
    path = initialize(tmp_path / "config.yaml")
    data = yaml.safe_load(path.read_text())
    data.update(
        dry_run=True,
        code_host={"adapter": "antcode"},
        pipeline={"adapter": "aci"},
        notification={
            "adapter": "dingtalk",
            "api_url": "https://example.invalid",
            "card_template_id": "card",
            "issue_comments": True,
        },
        repositories=[
            {
                "name": "r",
                "remote": "https://example.invalid/r",
                "project": "1",
                "pipeline_enabled": True,
                "pipeline": {"project": "2", "yml_path": ".aci.yml"},
            }
        ],
    )
    path.write_text(yaml.safe_dump(data))
    commands = Mock()
    runtime = build_runtime(path, commands=commands)
    runtime.runs.process("r", 1)
    commands.run.assert_not_called()
    assert not runtime.store.list_operations(runtime.store.list_runs()[0].run_id)
    runtime.close()


def daemon_fixture(tmp_path):
    config = AppConfig(state=StateConfig(directory=str(tmp_path)))
    commands = Mock()
    manager = DaemonManager(
        config, tmp_path / "config.yaml", commands, kill=Mock(), sleep=lambda _: None
    )
    commands.run.return_value = CommandResult(
        (),
        0,
        f"Mon Oct 5 12:00:00 2026 python -m dtcoder_agentic_dev.cli.app --config {tmp_path / 'config.yaml'} run --daemon-token token",
        "",
        0,
    )
    manager.pid_file.write_text(
        json.dumps(
            {
                "pid": 1234,
                "started": "Mon Oct 5 12:00:00 2026",
                "token": "token",
                "config": manager.config_path,
            }
        )
    )
    return manager, commands


def test_daemon_status_stale_and_pid_reuse_never_kills(tmp_path):
    manager, commands = daemon_fixture(tmp_path)
    assert manager.status() == {"status": "运行", "pid": 1234}
    commands.run.return_value = CommandResult(
        (), 0, "Mon Oct 5 12:00:00 2026 python other.py", "", 0
    )
    assert manager.status()["status"] == "stale PID"
    manager.stop()
    manager.kill.assert_not_called()
    assert manager.status()["status"] == "停止"


def test_daemon_start_atomic_pid_and_logs(tmp_path):
    config = AppConfig(state=StateConfig(directory=str(tmp_path)))
    commands, launch = Mock(), Mock()
    launch.return_value.pid = 4321
    launch.return_value.poll.return_value = None

    def inspect(args, **kwargs):
        command = launch.call_args.args[0]
        return CommandResult((), 0, "Mon Oct 5 12:00:00 2026 " + " ".join(command), "", 0)

    commands.run.side_effect = inspect
    manager = DaemonManager(
        config, tmp_path / "config.yaml", commands, launch=launch, sleep=lambda _: None
    )
    assert manager.start()["pid"] == 4321
    assert manager.status()["status"] == "运行"
    assert (
        launch.call_args.kwargs["shell"] is False and launch.call_args.kwargs["start_new_session"]
    )
    assert isinstance(launch.call_args.args[0], list)
    assert manager.pid_file.stat().st_mode & 0o077 == 0
    manager.log_file.write_text("第一行\n第二行\n第三行\n")
    assert list(manager.logs(2)) == ["第二行\n第三行\n"]
    with pytest.raises(ConfigurationError):
        manager.start()


def test_daemon_graceful_stop(tmp_path):
    manager, commands = daemon_fixture(tmp_path)
    original = commands.run.return_value
    commands.run.side_effect = [original, original, CommandResult((), 1, "", "", 0)]
    assert manager.stop()["status"] == "停止"
    manager.kill.assert_called_once_with(1234, signal.SIGTERM)


def test_daemon_timeout_requires_explicit_force(tmp_path):
    manager, _ = daemon_fixture(tmp_path)
    with pytest.raises(TechnicalError):
        manager.stop(0.001)
    manager.kill.assert_called_once_with(1234, signal.SIGTERM)
    manager.kill.reset_mock()
    manager.stop(0.001, force=True)
    assert manager.kill.call_args.args == (1234, signal.SIGKILL)


@pytest.fixture
def rollback(tmp_path, run, issue, clock, ids):
    repo = RepoConfig("r", "https://example.invalid/r")
    run.repository_id, issue.repository_id = repo.repository_id, repo.repository_id
    run.status, run.branch = RunStatus.FAILED, "agent/old"
    run.workspace_path = str(tmp_path / "workspaces" / repo.repository_id / run.run_id)
    run.context = {"base_revision": "a" * 40, "git_head": "c" * 40}
    store = SQLiteRunRepository(tmp_path / "state.db")
    store.create_run(run)
    store.save_issue(issue)
    attempt = StepAttempt(
        "old-attempt",
        run.run_id,
        "coding",
        1,
        status=AttemptStatus.SUCCEEDED,
        input_snapshot={
            "run": {
                **asdict(run),
                "context": {
                    "base_revision": "a" * 40,
                    "git_head": "b" * 40,
                    "outputs": {"requirements": {"type": "SUCCEEDED"}},
                },
            }
        },
        metadata={"git_head": "c" * 40},
    )
    store.save_attempt(attempt)
    store.save_artifact(
        Artifact("art", run.run_id, "requirements", "spec", "spec.md", "digest", "b" * 40)
    )
    git, workspace, host, pipeline = Mock(), Mock(), Mock(), Mock()
    git.head_sha.return_value = "c" * 40
    git.current_branch.return_value = run.branch
    git.is_ancestor.return_value = True
    git.is_repository.return_value = True
    workspace.prepare_at.side_effect = lambda new, repo, sha: str(
        tmp_path / "workspaces" / repo.repository_id / new.run_id
    )
    config = AppConfig(
        repositories=[repo],
        workspace=WorkspaceConfig(str(tmp_path / "mirrors"), str(tmp_path / "workspaces")),
    )
    runs = RunService(
        store,
        config,
        host,
        workspace,
        EventDispatcher(store, clock, ids),
        clock,
        ids,
        RunExecutionGuard(tmp_path / "locks"),
        git=git,
        pipeline=pipeline,
    )
    service = RollbackService(runs)
    yield service, store, git, workspace, host, pipeline, run
    store.close()


def test_rollback_plan_and_success_preserve_old_history(rollback):
    service, store, git, workspace, _, _, run = rollback
    before = store.load_run(run.run_id)
    plan = service.plan(run.run_id, "coding", "纠正实现")
    assert plan["after_revision"] == "b" * 40
    git.backup_ref.assert_not_called()
    new = service.execute(
        run.run_id, "coding", "纠正实现", expected_revision=plan["source_revision"]
    )
    assert new.current_step == "coding" and new.context["rollback_from"] == run.run_id
    assert new.status is RunStatus.QUEUED and new.branch != run.branch
    assert store.load_run(run.run_id) == before
    assert len(store.list_attempts(run.run_id)) == 1 and not store.list_attempts(new.run_id)
    args = git.backup_ref.call_args.args
    assert args[1].startswith("refs/dtcoder/backups/run-1/") and args[2] == "c" * 40
    workspace.prepare_at.assert_called_once()
    audit = store.list_operations(run.run_id)[0]
    assert audit.status is OperationStatus.SUCCEEDED and audit.request_snapshot["operator"]
    assert audit.response_snapshot["successor_id"] == new.run_id
    assert store.list_events(run.run_id)[0].type.value == "RunRolledBack"


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.WAITING])
def test_rollback_invalid_active_states(rollback, status):
    service, store, git, _, _, _, run = rollback
    run.status = status
    store.update_run(run)
    with pytest.raises(ConfigurationError):
        service.plan(run.run_id, "coding", "r")
    git.backup_ref.assert_not_called()


def test_rollback_missing_revision_and_wrong_ancestry(rollback):
    service, store, git, _, _, _, run = rollback
    attempt = store.list_attempts(run.run_id)[0]
    attempt.input_snapshot["run"]["context"].pop("git_head")
    attempt.input_snapshot["run"]["context"].pop("base_revision")
    store.save_attempt(attempt)
    with pytest.raises(ConfigurationError):
        service.plan(run.run_id, "coding", "r")
    attempt.input_snapshot["run"]["context"]["git_head"] = "b" * 40
    store.save_attempt(attempt)
    git.is_ancestor.return_value = False
    with pytest.raises(ConfigurationError):
        service.plan(run.run_id, "coding", "r")


def test_rollback_lease_attempt_and_lock_protection(rollback):
    service, store, _, _, _, _, run = rollback
    run.status = RunStatus.QUEUED
    store.update_run(run)
    claimed = store.claim_run("owner", 1000, 8999, run.run_id)
    claimed.status = RunStatus.FAILED
    store.update_run(claimed)
    with pytest.raises(ConcurrencyConflict):
        service.plan(run.run_id, "coding", "r")
    store.release_lease(run.run_id, "owner")
    with service.runs.execution_guard(run.run_id):
        with pytest.raises(ConcurrencyConflict):
            service.plan(run.run_id, "coding", "r")
    attempt = store.list_attempts(run.run_id)[0]
    attempt.status = AttemptStatus.RUNNING
    store.save_attempt(attempt)
    with pytest.raises(ConcurrencyConflict):
        service.plan(run.run_id, "coding", "r")


def add_remotes(store, run, host, pipeline):
    for name, external in [("create_pr", "pr"), ("pipeline", "ci")]:
        store.save_operation(
            ExternalOperation(
                name,
                run.run_id,
                name,
                run.run_id + ":" + name,
                OperationStatus.SUCCEEDED,
                external_id=external,
            )
        )
    host.get_pull_request.return_value = PullRequest("pr", "https://example.invalid/pr", "open")
    pipeline.get_status.return_value = PipelineResult("ci", PipelineStatus.RUNNING)


def test_rollback_merged_pr_even_keep_refused(rollback):
    service, store, _, _, host, pipeline, run = rollback
    add_remotes(store, run, host, pipeline)
    host.get_pull_request.return_value = PullRequest("pr", "url", "merged")
    with pytest.raises(ConfigurationError):
        service.plan(run.run_id, "coding", "r", keep_pr=True)


@pytest.mark.parametrize("keep", [False, True])
def test_rollback_keep_remote_options(rollback, keep):
    service, store, git, _, host, pipeline, run = rollback
    add_remotes(store, run, host, pipeline)
    service.execute(run.run_id, "coding", "r", keep_pr=keep, keep_pipeline=keep, backup=False)
    assert host.close_pull_request.call_count == (0 if keep else 1)
    assert pipeline.cancel.call_count == (0 if keep else 1)
    git.backup_ref.assert_not_called()


def test_rollback_remote_failure_partial_audit(rollback):
    service, store, _, workspace, host, pipeline, run = rollback
    add_remotes(store, run, host, pipeline)
    host.close_pull_request.side_effect = OSError("token-secret")
    with pytest.raises(TechnicalError):
        service.execute(run.run_id, "coding", "r")
    workspace.prepare_at.assert_not_called()
    audit = next(o for o in store.list_operations(run.run_id) if o.operation_type == "rollback")
    assert audit.status is OperationStatus.FAILED
    assert [a["status"] for a in audit.response_snapshot["remote_actions"]] == [
        "FAILED",
        "SUCCEEDED",
    ]
    assert "token-secret" not in str(asdict(audit))
    assert store.load_run(run.run_id).status is RunStatus.FAILED


def test_rollback_paused_superseded_and_concurrency_revision(rollback):
    service, store, _, _, _, _, run = rollback
    run.status = RunStatus.PAUSED
    store.update_run(run)
    plan = service.plan(run.run_id, "coding", "r")
    store.update_run(run)
    with pytest.raises(ConcurrencyConflict):
        service.execute(run.run_id, "coding", "r", expected_revision=plan["source_revision"])
    successor = service.execute(run.run_id, "coding", "r")
    assert store.load_run(run.run_id).context["superseded_by"] == successor.run_id
    assert store.load_run(run.run_id).status is RunStatus.CANCELLED


def test_pipeline_cancel_failure_isolated(run, issue, clock, ids):
    from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
    from dtcoder_agentic_dev.application.services.pipeline_control import (
        PipelineCancellationHandler,
    )
    from dtcoder_agentic_dev.domain.events import EventType

    store = InMemoryRunRepository()
    run.status = RunStatus.CANCELLED
    store.create_run(run)
    store.save_operation(ExternalOperation("op", run.run_id, "pipeline", "key", external_id="ci"))
    pipeline = Mock()
    pipeline.get_status.return_value = PipelineResult("ci", PipelineStatus.RUNNING)
    pipeline.cancel.side_effect = TechnicalError("failed")
    dispatcher = EventDispatcher(store, clock, ids, [PipelineCancellationHandler(store, pipeline)])
    dispatcher.publish([dispatcher.make(EventType.PIPELINE_STARTED, run.run_id)])
    assert store.load_run(run.run_id).status is RunStatus.CANCELLED
    assert store.list_events(run.run_id)[0].payload["handler_failures"]


def test_doctor_reads_auth_and_remote_without_mutation(tmp_path):
    from dtcoder_agentic_dev.application.services.diagnostics import diagnose

    path = initialize(tmp_path / "config.yaml")
    data = yaml.safe_load(path.read_text())
    data.update(
        code_host={"adapter": "antcode"},
        pipeline={"adapter": "aci"},
        repositories=[
            {
                "name": "r",
                "remote": "https://example.invalid/r",
                "project": "1",
                "pipeline_enabled": True,
                "pipeline": {"project": "2", "yml_path": ".aci.yml"},
            }
        ],
    )
    path.write_text(yaml.safe_dump(data))
    commands = Mock()
    commands.run.return_value = CommandResult((), 0, "readonly response", "", 0)
    runtime = build_runtime(path, commands=commands)
    results = diagnose(runtime)
    assert all(d.ok for d in results)
    for call in commands.run.call_args_list:
        args = call.args[0]
        assert not any(
            v in args for v in ["create", "comment", "trigger", "push", "clone", "fetch"]
        )
    assert any(call.args[0] == ["aci", "auth", "status"] for call in commands.run.call_args_list)
    runtime.close()


def test_rollback_pending_recovery_without_successor(rollback):
    service, store, _, _, _, _, run = rollback
    plan = service.plan(run.run_id, "coding", "r")
    pending = ExternalOperation(
        "interrupted",
        run.run_id,
        "rollback",
        "pending-rollback",
        request_snapshot={**plan, "successor_id": "not-created"},
    )
    store.save_operation(pending)
    with pytest.raises(ConcurrencyConflict):
        service.runs.control(run.run_id, "resume")
    with pytest.raises(ConcurrencyConflict):
        service.plan(run.run_id, "coding", "r")
    recovered = service.recover(run.run_id, "确认进程已中断")
    assert (
        recovered.status is OperationStatus.FAILED
        and recovered.response_snapshot["error_type"] == "ProcessInterrupted"
    )
    assert service.plan(run.run_id, "coding", "重新规划")["after_revision"] == "b" * 40


def test_rollback_workspace_failure_retains_paused_successor(rollback):
    service, store, _, workspace, _, _, run = rollback
    workspace.prepare_at.side_effect = TechnicalError("工作区创建失败")
    with pytest.raises(TechnicalError):
        service.execute(run.run_id, "coding", "r")
    successor = next(r for r in store.list_runs() if r.run_id != run.run_id)
    assert (
        successor.status is RunStatus.PAUSED and successor.context["rollback_revision"] == "b" * 40
    )
    audit = next(o for o in store.list_operations(run.run_id) if o.operation_type == "rollback")
    assert audit.status is OperationStatus.FAILED
    assert audit.request_snapshot["successor_id"] == successor.run_id
    # 工作区未准备完成时不能自动恢复成功，显式 resume 仍从记录修订准备。
    assert service.runs.control(successor.run_id, "resume").status is RunStatus.QUEUED


def test_rollback_pending_with_paused_successor_reconciles(rollback):
    service, store, _, workspace, _, _, run = rollback
    plan = service.plan(run.run_id, "coding", "r")
    pending = ExternalOperation(
        "pending-op",
        run.run_id,
        "rollback",
        "pending-key",
        request_snapshot={**plan, "successor_id": "successor"},
        response_snapshot={"remote_actions": []},
    )
    store.save_operation(pending)
    from dtcoder_agentic_dev.domain.models import WorkflowRun

    successor = WorkflowRun(
        "successor",
        run.workflow_name,
        run.workflow_version,
        run.repository_id,
        run.issue_external_id,
        run.issue_number,
        status=RunStatus.PAUSED,
        current_step="coding",
        branch="agent/successor",
        context={
            "rollback_from": run.run_id,
            "rollback_operation": "pending-op",
            "rollback_revision": "b" * 40,
        },
    )
    store.create_run(successor)
    with pytest.raises(ConcurrencyConflict):
        service.runs.control(successor.run_id, "resume")
    audit = service.recover(run.run_id, "恢复中断的工作区准备")
    assert audit.status is OperationStatus.SUCCEEDED
    assert store.load_run(successor.run_id).status is RunStatus.QUEUED
    workspace.prepare_at.assert_called_once()


def test_rollback_cli_plan_no_yes_and_explicit_backup_confirmation(tmp_path, rollback, mocker):
    service, _, git, _, _, _, run = rollback
    mocker.patch("dtcoder_agentic_dev.cli.app.runtime_for", return_value=Mock(runs=service.runs))
    cli = CliRunner()
    args = ["rollback", "--run", run.run_id, "--step", "coding", "--reason", "r"]
    result = cli.invoke(app, args + ["--dry-run"])
    assert result.exit_code == 0 and "仅展示计划" in result.output
    git.backup_ref.assert_not_called()
    result = cli.invoke(app, args + ["--yes", "--no-backup"])
    assert result.exit_code != 0 and "confirm-no-backup" in result.output
    git.backup_ref.assert_not_called()


def test_daemon_logs_follow_reads_appends(tmp_path):
    manager, _ = daemon_fixture(tmp_path)
    manager.log_file.parent.mkdir(parents=True)
    manager.log_file.write_text("历史\n")

    def append(_):
        with manager.log_file.open("a") as stream:
            stream.write("新内容\n")

    manager.sleep = append
    lines = manager.logs(1, follow=True)
    assert next(lines) == "历史\n"
    assert next(lines) == "新内容\n"
    lines.close()


def test_daemon_windows_clear_error(tmp_path, monkeypatch):
    manager, _ = daemon_fixture(tmp_path)
    monkeypatch.setattr("dtcoder_agentic_dev.infrastructure.process.daemon.os.name", "nt")
    with pytest.raises(ConfigurationError, match="Windows"):
        manager.stop()


def test_worktree_rejects_nested_symlink_escape(tmp_path, run):
    from dtcoder_agentic_dev.domain.errors import FatalError
    from dtcoder_agentic_dev.infrastructure.git.workspace import GitWorktreeWorkspaceManager

    root, outside = tmp_path / "managed", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / run.repository_id).symlink_to(outside, target_is_directory=True)
    manager = GitWorktreeWorkspaceManager(
        Mock(), WorkspaceConfig(str(tmp_path / "mirrors"), str(root))
    )
    with pytest.raises(FatalError):
        manager.cleanup(run)


def test_doctor_unknown_template_variable_is_configuration_error(tmp_path):
    from dtcoder_agentic_dev.application.services.diagnostics import diagnose

    path = initialize(tmp_path / "config.yaml")
    (tmp_path / "prompts/coding.txt").write_text("{unsupported_variable}")
    commands = Mock()
    runtime = build_runtime(path, commands=commands)
    results = diagnose(runtime)
    template = next(d for d in results if d.component == "模板")
    assert not template.ok and template.status == "配置错误"
    runtime.close()


def test_daemon_corrupt_pid_record_is_stale(tmp_path):
    manager, _ = daemon_fixture(tmp_path)
    manager.pid_file.write_text("[]")
    assert manager.status()["status"] == "stale PID"
    manager.stop()
    manager.kill.assert_not_called()


def test_library_dry_run_dispatch_preserves_queue_and_no_git(tmp_path):
    path = initialize(tmp_path / "config.yaml")
    data = yaml.safe_load(path.read_text())
    data.update(dry_run=True, repositories=[{"name": "r", "remote": "https://example.invalid/r"}])
    path.write_text(yaml.safe_dump(data))
    commands = Mock()
    runtime = build_runtime(path, commands=commands)
    run = runtime.runs.process("r", 1)
    assert runtime.dispatcher.dispatch_once(run.run_id) is None
    assert runtime.scheduler.run_once() == []
    assert runtime.store.load_run(run.run_id).status is RunStatus.QUEUED
    assert not runtime.store.list_attempts(run.run_id)
    with pytest.raises(ConfigurationError):
        runtime.runs.cleanup(run.run_id)
    commands.run.assert_not_called()
    runtime.close()

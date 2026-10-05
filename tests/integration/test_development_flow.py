"""真实默认步骤 + SQLite + 文件产物；所有外部调用均为测试替身。"""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.application.workflows import issue_development_workflow
from dtcoder_agentic_dev.config import RepoConfig
from dtcoder_agentic_dev.domain.errors import TechnicalError
from dtcoder_agentic_dev.domain.models import ExternalOperation, OperationStatus, RunStatus
from dtcoder_agentic_dev.domain.workflow import StepServices
from dtcoder_agentic_dev.engine.registry import StepRegistry
from dtcoder_agentic_dev.engine.retry import RetryPolicy
from dtcoder_agentic_dev.engine.runner import WorkflowRunner
from dtcoder_agentic_dev.engine.steps.development import (
    CodingStep,
    FixStep,
    RequirementsStep,
    ReviewStep,
)
from dtcoder_agentic_dev.engine.steps.external import CreatePRStep, PipelineStep
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import FileArtifactStore
from dtcoder_agentic_dev.ports.code_host import PullRequest
from dtcoder_agentic_dev.ports.pipeline import PipelineResult, PipelineStatus
from dtcoder_agentic_dev.prompts.renderer import StrictPromptRenderer

TEMPLATES = Path(__file__).parents[2] / "src/dtcoder_agentic_dev/prompts/templates"
BODY = "# Summary\n功能说明\n# Linked Issue\n#1\n# Changes\n实现和测试\n# Test Plan\npytest 通过\n"


class FakeGit:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.files = {}
        self.commits = 0
        self.pushes = 0

    def snapshot_untracked(self, path):
        return {
            str(p.relative_to(self.workspace))
            for p in self.workspace.rglob("*")
            if p.is_file() and str(p.relative_to(self.workspace)) not in self.files
        }

    def status_porcelain(self, path):
        return "".join(
            f" M {p.relative_to(self.workspace)}\n"
            for p in self.workspace.rglob("*")
            if p.is_file() and self.files.get(str(p.relative_to(self.workspace))) != p.read_bytes()
        )

    def commit_changes(self, path, message, *, artifact_paths, untracked_before):
        self.commits += 1
        self.files = {
            str(p.relative_to(self.workspace)): p.read_bytes()
            for p in self.workspace.rglob("*")
            if p.is_file()
        }
        return True

    def head_sha(self, path):
        return f"head-{self.commits}"

    def diff_name_only(self, path, base):
        return list(self.files)

    def push(self, path, branch):
        self.pushes += 1


class FakeAgent:
    def __init__(self, blocked_rounds=0, invalid_summary=False, no_code=False):
        self.calls = []
        self.blocked_rounds = blocked_rounds
        self.invalid_summary = invalid_summary
        self.no_code = no_code

    def execute(self, request):
        self.calls.append(request)
        change = Path(request.workspace) / ".agents/changes/issue-1"
        change.mkdir(parents=True, exist_ok=True)
        if request.step_name == "requirements":
            for name in ("spec.md", "plan.md", "tasks.md"):
                file = change / name
                if not file.exists():
                    file.write_text(f"需求 {name}", encoding="utf-8")
        elif request.step_name == "coding":
            if not self.no_code:
                (Path(request.workspace) / "feature.py").write_text(
                    "result = 1\n", encoding="utf-8"
                )
        elif request.step_name == "fix":
            (Path(request.workspace) / "feature.py").write_text(
                f"result = {request.attempt_number + 1}\n", encoding="utf-8"
            )
        elif request.step_name == "review":
            # Prompt 携带本轮路径，技术重试不会递增 round。
            import re

            n = int(re.search(r"codereview/round-(\d+)", request.prompt).group(1))
            folder = change / f"codereview/round-{n}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "report.md").write_text("评审报告", encoding="utf-8")
            summary = {
                "passed": True,
                "round": n,
                "blockers": ["必须修复"] if n <= self.blocked_rounds else [],
                "majors": [],
                "minors": [],
            }
            (folder / "summary.json").write_text(
                "非法 JSON" if self.invalid_summary else json.dumps(summary), encoding="utf-8"
            )
        elif request.step_name == "create_pr":
            (change / "pr_body.md").write_text(BODY, encoding="utf-8")


class FakeHost:
    def __init__(self):
        self.prs = {}
        self.created = 0

    def find_pull_request(self, repo, key):
        return self.prs.get(key)

    def create_pull_request(self, repo, **kwargs):
        key = kwargs["idempotency_key"]
        self.created += 1
        self.prs[key] = PullRequest("pr-1", "https://example.invalid/pr/1")
        return self.prs[key]


class FakePipeline:
    def __init__(self, status=PipelineStatus.SUCCEEDED):
        self.status = status
        self.calls = 0
        self.created = {}

    def trigger(self, repo, branch, key):
        self.calls += 1
        self.created.setdefault(
            key,
            PipelineResult(
                "pipeline-1", PipelineStatus.RUNNING, "https://example.invalid/pipeline/1"
            ),
        )
        return self.created[key]

    def get_status(self, repo, id):
        return PipelineResult(id, self.status, "https://example.invalid/pipeline/1")


class Harness:
    def __init__(
        self,
        tmp_path,
        run,
        issue,
        clock,
        ids,
        *,
        pipeline_enabled=False,
        blocked_rounds=0,
        max_fix=3,
    ):
        self.database = tmp_path / "state.db"
        self.store = SQLiteRunRepository(self.database)
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.run = replace(
            run,
            workspace_path=str(self.workspace),
            branch="agent/test",
            context={"pipeline_enabled": pipeline_enabled, "base_revision": "base-head"},
        )
        self.issue, self.clock, self.ids = issue, clock, ids
        self.repo = RepoConfig(
            "r", "local", pipeline_enabled=pipeline_enabled, validation_commands=[["verify"]]
        )
        self.git = FakeGit(self.workspace)
        self.agent = FakeAgent(blocked_rounds)
        self.commands = Mock()
        self.host, self.pipeline = FakeHost(), FakePipeline()
        self.max_fix = max_fix
        self.store.create_run(self.run)
        self.store.save_issue(issue)
        self.build()

    def build(self):
        renderer = StrictPromptRenderer(str(TEMPLATES))
        registry = StepRegistry()
        for step in [
            RequirementsStep(renderer, self.git),
            CodingStep(renderer, self.git, self.commands),
            ReviewStep(renderer, self.git),
            FixStep(renderer, self.git, self.commands, self.max_fix),
            PipelineStep(self.pipeline, self.store, self.ids, 30),
            CreatePRStep(renderer, self.git, self.host, self.store, self.ids),
        ]:
            registry.register(step)
        self.services = StepServices(
            self.agent, FileArtifactStore(self.clock, self.ids), self.clock
        )
        self.events = EventDispatcher(self.store, self.clock, self.ids)
        self.engine = WorkflowRunner(
            self.store,
            registry,
            issue_development_workflow(),
            self.services,
            self.events,
            self.ids,
            self.clock,
            RetryPolicy(2, 1),
            20,
            self.git.head_sha,
        )

    def execute(self):
        if not self.store.claim_run("worker", self.clock.now(), 1000, self.run.run_id):
            return None
        try:
            return self.engine.run(self.run.run_id, self.issue, self.repo, "worker")
        finally:
            self.store.release_lease(self.run.run_id, "worker")

    def close(self):
        self.store.close()


def test_default_real_steps_succeed(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids)
    result = h.execute()
    assert result.status is RunStatus.SUCCEEDED and result.context["pr_url"].endswith("/pr/1")
    assert h.host.created == h.git.pushes == 1
    assert len(h.store.list_artifacts(run.run_id)) == 6
    assert all(a.git_revision for a in h.store.list_artifacts(run.run_id))
    assert all(a.input_snapshot.get("template_sha256") for a in h.store.list_attempts(run.run_id))
    assert h.commands.run.call_args.kwargs["cwd"] == str(h.workspace)
    h.close()


def test_passed_true_with_blocker_still_fixes(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids, blocked_rounds=1)
    result = h.execute()
    assert result.status is RunStatus.SUCCEEDED
    assert result.context["fix_loops"] == 1 and result.context["review_round"] == 2
    assert [a.step_name for a in h.store.list_attempts(run.run_id)].count("review") == 2
    assert clock.sleeps == []
    h.close()


def test_max_fix_loop_fails(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids, blocked_rounds=20, max_fix=1)
    result = h.execute()
    assert result.status is RunStatus.FAILED and "上限 1" in result.last_error
    assert h.host.created == 0
    h.close()


def test_invalid_review_retries_technical(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids)
    h.agent.invalid_summary = True
    assert h.execute().status is RunStatus.FAILED
    reviews = [a for a in h.store.list_attempts(run.run_id) if a.step_name == "review"]
    assert len(reviews) == 3 and all(a.error_type == "TechnicalError" for a in reviews)
    assert clock.sleeps == [1, 2]
    h.close()


def test_no_code_is_explicit_failure(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids)
    h.agent.no_code = True
    result = h.execute()
    assert result.status is RunStatus.FAILED and "没有产生代码变化" in result.last_error
    h.close()


def test_waiting_across_store_reopen_no_duplicate_pipeline(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids, pipeline_enabled=True)
    assert h.execute().status is RunStatus.WAITING
    assert h.pipeline.calls == 1
    h.store.close()
    h.store = SQLiteRunRepository(h.database)
    h.build()
    assert h.execute() is None
    clock.value += 30
    assert h.execute().status is RunStatus.SUCCEEDED
    assert h.pipeline.calls == 1 and h.host.created == 1
    h.close()


@pytest.mark.parametrize("blocks", [True, False])
def test_pipeline_failure_policy(tmp_path, run, issue, clock, ids, blocks):
    h = Harness(tmp_path, run, issue, clock, ids, pipeline_enabled=True)
    h.repo.pipeline_failure_blocks = blocks
    h.pipeline.status = PipelineStatus.FAILED
    assert h.execute().status is RunStatus.WAITING
    clock.value += 30
    assert h.execute().status is (RunStatus.FAILED if blocks else RunStatus.SUCCEEDED)
    h.close()


def test_pr_crash_window_reconciles_remote(tmp_path, run, issue, clock, ids, mocker):
    h = Harness(tmp_path, run, issue, clock, ids)
    save = h.store.save_operation
    failed = False

    def crash_once(op):
        nonlocal failed
        if (
            op.operation_type == "create_pr"
            and op.status is OperationStatus.SUCCEEDED
            and not failed
        ):
            failed = True
            raise TechnicalError("模拟远端成功后保存失败")
        return save(op)

    mocker.patch.object(h.store, "save_operation", side_effect=crash_once)
    assert h.execute().status is RunStatus.SUCCEEDED
    assert h.host.created == 1 and h.git.pushes == 1
    assert len(h.store.list_operations(run.run_id)) == 1
    h.close()


def test_requirements_reused_and_template_hash(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids)
    folder = h.workspace / ".agents/changes/issue-1"
    folder.mkdir(parents=True)
    for name in ("spec.md", "plan.md", "tasks.md"):
        (folder / name).write_text("人工确认的合法产物", encoding="utf-8")
    assert h.execute().status is RunStatus.SUCCEEDED
    assert not any(c.step_name == "requirements" for c in h.agent.calls)
    assert (folder / "spec.md").read_text(encoding="utf-8") == "人工确认的合法产物"
    h.close()


def test_pending_pipeline_intent_uses_remote_key(tmp_path, run, issue, clock, ids):
    h = Harness(tmp_path, run, issue, clock, ids, pipeline_enabled=True)
    key = f"{run.run_id}:pipeline"
    h.store.save_operation(ExternalOperation("pending", run.run_id, "pipeline", key))
    h.pipeline.trigger("repo", "branch", key)  # 模拟远端已成功，本地尚无 external_id。
    assert h.execute().status is RunStatus.WAITING
    assert h.pipeline.calls == 2 and len(h.pipeline.created) == 1
    clock.value += 30
    assert h.execute().status is RunStatus.SUCCEEDED
    h.close()


def test_product_bootstrap_with_injected_external_ports(tmp_path, issue, clock, ids):
    import yaml

    from dtcoder_agentic_dev.application.services.initialization import initialize
    from dtcoder_agentic_dev.cli.bootstrap import build_runtime
    from dtcoder_agentic_dev.config import load_config

    path = tmp_path / "config.yaml"
    initialize(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["repositories"] = [{"name": "example", "remote": "https://example.invalid/example.git"}]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    repo = load_config(path).repositories[0]
    current_issue = replace(issue, repository_id=repo.repository_id)
    workspace_path = tmp_path / "isolated"
    workspace_path.mkdir()
    git = FakeGit(workspace_path)
    host = FakeHost()
    agent = FakeAgent()
    host.get_issue = Mock(return_value=current_issue)
    host.list_open_issues = Mock(return_value=[current_issue])
    workspace = Mock()

    def prepare(run, repository):
        run.context["base_revision"] = "base-fixture"
        return str(workspace_path)

    workspace.prepare.side_effect = prepare
    runtime = build_runtime(
        path,
        code_host=host,
        agent_executor=agent,
        pipeline=FakePipeline(),
        commands=Mock(),
        clock=clock,
        sleeper=clock,
        ids=ids,
        workspace=workspace,
        git=git,
    )
    try:
        results = runtime.scheduler.run_once()
        assert len(results) == 1 and results[0].status is RunStatus.SUCCEEDED
        assert results[0].context["pr_url"] == "https://example.invalid/pr/1"
        assert runtime.store.load_run(results[0].run_id).lease_owner is None
        assert (tmp_path / "logs/runs" / results[0].run_id / "run.log").is_file()
    finally:
        runtime.close()

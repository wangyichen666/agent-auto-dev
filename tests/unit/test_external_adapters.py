"""平台契约测试，命令执行与网络均为替身。"""

import json
from unittest.mock import Mock

import pytest

from dtcoder_agentic_dev.adapters.cli_json import items, marker
from dtcoder_agentic_dev.adapters.code_host.antcode import AntCodeAdapter
from dtcoder_agentic_dev.adapters.pipeline.aci import ACIAdapter
from dtcoder_agentic_dev.config import (
    CodeHostConfig,
    PipelineConfig,
    RepoConfig,
    RepoPipelineConfig,
)
from dtcoder_agentic_dev.domain.errors import (
    CapabilityNotConfigured,
    ExternalCommandError,
    ExternalCommandTimeout,
    TechnicalError,
)
from dtcoder_agentic_dev.ports.command_runner import CommandResult
from dtcoder_agentic_dev.ports.pipeline import PipelineStatus

HELP = "--json --all --idempotency-key --source-branch --target-branch --reviewer --remove-source-branch"


class CLI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.help = HELP

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if "--help" in args:
            return CommandResult(tuple(args), 0, self.help, "", 0)
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, CommandResult):
            return value
        return CommandResult(
            tuple(args), 0, value if isinstance(value, str) else json.dumps(value), "", 0
        )


@pytest.fixture
def repo():
    return RepoConfig(
        "repo",
        "https://example.invalid/repo",
        provider="antcode",
        project="123",
        pipeline=RepoPipelineConfig(project="456", yml_path=".aci.yml", params={"env": "pre"}),
    )


def host(repo, responses, profile=""):
    cli = CLI(responses)
    return AntCodeAdapter(CodeHostConfig(profile=profile), [repo], cli), cli


@pytest.mark.parametrize("wrapped", [False, True])
def test_antcode_list_mapping(repo, wrapped):
    record = {
        "iid": "7",
        "id": 70,
        "title": "标题",
        "description": "正文",
        "web_url": "https://example.invalid/7",
        "author": {"name": "展示名称", "id": "100"},
        "labels": [{"name": "ready"}, "x"],
        "assignee": {"user_id": "200"},
        "reviewers": [{"name": "无身份"}, {"username": "login"}],
    }
    adapter, commands = host(
        repo, [{"items": [record]} if wrapped else [record]], "private-profile"
    )
    issue = adapter.list_open_issues(repo.repository_id)[0]
    assert (issue.number, issue.external_id, issue.body, issue.author) == (7, "70", "正文", "100")
    assert issue.assignees == ["200"] and issue.raw == {"reviewers": ["login"]}
    assert issue.labels == ["ready", "x"]
    assert commands.calls[-1][0] == [
        "antcode",
        "--profile",
        "private-profile",
        "issue",
        "list",
        "--project",
        "123",
        "--state",
        "open",
        "--all",
        "--json",
    ]
    assert commands.calls[-1][1] == {"timeout": 60, "check": False}


def test_issue_aliases_and_missing_identity(repo):
    adapter, commands = host(
        repo,
        [
            {
                "issue_iid": 4,
                "title": "t",
                "body": "b",
                "url": "https://example.invalid/i",
                "author": {"name": "只是展示名称"},
                "assignees": ["u", "u", ""],
                "label": "ready",
            }
        ],
    )
    issue = adapter.get_issue(repo.repository_id, 4)
    assert issue.author == "" and issue.assignees == ["u"]
    assert issue.labels == ["ready"] and issue.body == "b"
    assert commands.calls[0][0][1:3] == ["issue", "view"]


@pytest.mark.parametrize(
    "response,error",
    [
        ("not json", TechnicalError),
        ({"title": "missing iid"}, TechnicalError),
        ({"iid": "x", "title": "t"}, TechnicalError),
        ({"iid": 1}, TechnicalError),
        (CommandResult((), 2, "token-secret", "cookie-secret", 0), ExternalCommandError),
        (ExternalCommandTimeout("token-secret", stdout="secret"), ExternalCommandTimeout),
        (ExternalCommandError("cookie-secret"), ExternalCommandError),
        (OSError("secret"), ExternalCommandError),
    ],
)
def test_error_mapping_safe(repo, response, error):
    adapter, _ = host(repo, [response])
    with pytest.raises(error) as caught:
        adapter.get_issue(repo.repository_id, 1)
    assert "secret" not in str(caught.value)
    assert not getattr(caught.value, "stdout", "")
    assert not getattr(caught.value, "stderr", "")


def test_comment_pending_reconciliation(repo):
    key = "run:comment:event"
    tag = marker(key)
    adapter, cli = host(repo, [[], {"id": "comment"}, [{"id": "comment", "body": "正文\n" + tag}]])
    assert adapter.create_issue_comment(repo.repository_id, "7", "正文", key) == "comment"
    assert adapter.create_issue_comment(repo.repository_id, "7", "正文", key) == "comment"
    creates = [args for args, _ in cli.calls if args[1:3] == ["issue", "comment"]]
    assert len(creates) == 1 and tag in creates[0][creates[0].index("--body") + 1]


def test_pr_parameters_and_remote_reconciliation(repo):
    key = "run:create_pr"
    pr = {
        "iid": 9,
        "url": "https://example.invalid/pr/9",
        "state": "open",
        "description": marker(key),
    }
    adapter, cli = host(repo, [[], pr, [pr], pr, {}])
    result = adapter.create_pull_request(
        repo.repository_id,
        branch="agent/1",
        base_branch="main",
        title="t",
        body="b",
        idempotency_key=key,
        reviewers=["1", "", " 1 ", "2"],
        remove_source_branch=True,
    )
    assert result.external_id == "9"
    assert adapter.find_pull_request(repo.repository_id, key) == result
    assert adapter.get_pull_request(repo.repository_id, "9") == result
    adapter.close_pull_request(repo.repository_id, "9")
    args = next(a for a, _ in cli.calls if a[1:3] == ["pr", "create"] and "--help" not in a)
    assert args.count("--reviewer") == 2 and "--remove-source-branch" in args


def test_capability_detection_refuses_mutation(repo):
    adapter, cli = host(repo, [])
    cli.help = "no supported options"
    with pytest.raises(CapabilityNotConfigured):
        adapter.create_issue_comment(repo.repository_id, "1", "b", "key")
    assert len(cli.calls) == 1 and "--help" in cli.calls[0][0]


def test_partial_pagination_refuses_false_negative():
    with pytest.raises(CapabilityNotConfigured):
        items({"items": [], "total": 1})


@pytest.mark.parametrize("status,normalized", list(ACIAdapter.STATUSES.items()))
def test_aci_statuses(repo, status, normalized):
    cli = CLI(
        [
            "欢迎使用 ACI\n"
            + json.dumps({"result": {"result": {"pipelineId": 5, "status": status.upper()}}})
        ]
    )
    adapter = ACIAdapter(PipelineConfig(), [repo], cli)
    assert adapter.get_status(repo.repository_id, "5").status == normalized


def test_aci_trigger_parameters_and_reconciliation(repo, tmp_path):
    repo.pipeline.template_id, repo.pipeline.yml_path = "template", None
    repo.pipeline.env_file = str(tmp_path / "env")
    repo.pipeline.yml_global_path = str(tmp_path / "global")
    repo.pipeline.branch = "configured-branch"
    pipeline = {"pipeline_id": "5", "status": "pending", "idempotency_key": "key"}
    cli = CLI([[], {"result": pipeline}, {"items": [pipeline]}, {}])
    adapter = ACIAdapter(PipelineConfig(), [repo], cli)
    assert (
        adapter.trigger(repo.repository_id, "branch", "key", commit="abc").status
        is PipelineStatus.PENDING
    )
    adapter.trigger(repo.repository_id, "branch", "key")
    adapter.cancel(repo.repository_id, "5")
    creates = [a for a, _ in cli.calls if a[1:3] == ["pipeline", "trigger"] and "--help" not in a]
    assert len(creates) == 1
    args = creates[0]
    for pair in [
        ("--template-id", "template"),
        ("--commit", "abc"),
        ("--branch", "configured-branch"),
        ("--param", "env=pre"),
        ("--source", "skill"),
        ("--idempotency-key", "key"),
    ]:
        assert args[args.index(pair[0]) + 1] == pair[1]
    assert "--env-file" in args and "--yml-global-path" in args


@pytest.mark.parametrize(
    "data",
    [
        {"id": 1, "status": "unknown"},
        {"status": "success"},
        {"id": 1},
        {"items": [{"id": 1, "status": "pending"}]},
    ],
)
def test_aci_invalid_returns(repo, data):
    adapter = ACIAdapter(PipelineConfig(), [repo], CLI([data]))
    with pytest.raises(TechnicalError):
        adapter.get_status(repo.repository_id, "1")


def test_aci_reconciliation_key_must_match(repo):
    adapter = ACIAdapter(
        PipelineConfig(),
        [repo],
        CLI([[{"id": 1, "status": "running", "idempotency_key": "other"}]]),
    )
    with pytest.raises(TechnicalError):
        adapter.trigger(repo.repository_id, "b", "key")


def test_no_direct_subprocess_in_platform_adapters():
    import ast
    from pathlib import Path

    for name in ["code_host/antcode.py", "pipeline/aci.py"]:
        tree = ast.parse(
            (Path(__file__).parents[2] / "src/dtcoder_agentic_dev/adapters" / name).read_text()
        )
        assert not any(
            isinstance(n, ast.Import) and any(a.name == "subprocess" for a in n.names)
            for n in ast.walk(tree)
        )


def test_pipeline_push_before_trigger_and_recovery(run, issue, clock, ids):
    from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
    from dtcoder_agentic_dev.domain.workflow import OutcomeType, StepRequest, StepServices
    from dtcoder_agentic_dev.engine.steps.external import PipelineStep
    from dtcoder_agentic_dev.ports.pipeline import PipelineResult

    store = InMemoryRunRepository()
    store.create_run(run)
    order = []
    git = Mock()
    git.push.side_effect = lambda *a: order.append("push")
    git.head_sha.return_value = "sha"
    pipeline = Mock()
    pipeline.trigger.side_effect = lambda *a, **kw: (
        order.append("trigger") or PipelineResult("1", PipelineStatus.SUCCEEDED)
    )
    pipeline.get_status.return_value = PipelineResult("1", PipelineStatus.SUCCEEDED)
    repo = RepoConfig("r", "remote")
    request = StepRequest(run, issue, repo, "/mock", {}, 1)
    step = PipelineStep(pipeline, store, ids, 30, git)
    assert step.execute(request, StepServices(None, None, clock)).type is OutcomeType.WAITING
    assert order == ["push", "trigger"]
    assert step.execute(request, StepServices(None, None, clock)).type is OutcomeType.SUCCEEDED
    assert pipeline.trigger.call_count == 1 and git.push.call_count == 1
    assert pipeline.trigger.call_args.kwargs == {"commit": "sha"}


def test_aci_trigger_without_status_is_pending(repo):
    adapter = ACIAdapter(PipelineConfig(), [repo], CLI([[], {"result": {"pipelineId": 1}}]))
    assert adapter.trigger(repo.repository_id, "branch", "key").status is PipelineStatus.PENDING


def test_sensitive_remote_urls_rejected(repo):
    adapter, _ = host(
        repo, [{"iid": 1, "title": "t", "url": "https://user:secret@example.invalid/i"}]
    )
    with pytest.raises(TechnicalError) as error:
        adapter.get_issue(repo.repository_id, 1)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "id": 1},
        {"errcode": 5, "id": 1},
        {"error": "sensitive-remote-message", "id": 1},
    ],
)
def test_platform_rejection_not_fake_success(repo, response):
    adapter, _ = host(repo, [response])
    with pytest.raises(TechnicalError) as error:
        adapter.close_pull_request(repo.repository_id, "1")
    assert "sensitive-remote-message" not in str(error.value)


def test_pr_missing_state_refuses_merge_guess(repo):
    adapter, _ = host(repo, [{"id": 1, "url": "https://example.invalid/pr"}])
    with pytest.raises(TechnicalError):
        adapter.get_pull_request(repo.repository_id, "1")


def test_create_pr_merges_valid_identities_stably(tmp_path, run, issue, clock, ids):
    from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
    from dtcoder_agentic_dev.domain.workflow import StepRequest, StepServices
    from dtcoder_agentic_dev.engine.steps.external import CreatePRStep
    from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import FileArtifactStore
    from dtcoder_agentic_dev.ports.code_host import PullRequest

    store = InMemoryRunRepository()
    store.create_run(run)
    repo = RepoConfig("r", "remote", reviewers=["1", "2", ""], remove_source_branch=True)
    issue.author, issue.assignees, issue.raw = "5", ["3", "4"], {"reviewers": ["2", "3"]}
    folder = tmp_path / ".agents/changes/issue-1"
    folder.mkdir(parents=True)
    (folder / "pr_body.md").write_text(
        "# Summary\n说明\n# Linked Issue\n#1\n# Changes\n实现\n# Test Plan\n验证\n"
    )
    git, host_adapter = Mock(), Mock()
    git.snapshot_untracked.return_value = set()
    host_adapter.find_pull_request.return_value = None
    host_adapter.create_pull_request.return_value = PullRequest("1", "https://example.invalid/pr/1")
    step = CreatePRStep(None, git, host_adapter, store, ids)
    step.execute(
        StepRequest(run, issue, repo, str(tmp_path), {}, 1),
        StepServices(None, FileArtifactStore(clock, ids), clock),
    )
    assert host_adapter.create_pull_request.call_args.kwargs["reviewers"] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert host_adapter.create_pull_request.call_args.kwargs["remove_source_branch"] is True


def test_git_backup_and_target_worktree_commands_only_arrays(tmp_path):
    from dtcoder_agentic_dev.config import GitConfig
    from dtcoder_agentic_dev.infrastructure.git.client import GitClient

    commands = Mock()
    commands.run.return_value = CommandResult((), 0, "", "", 0)
    git = GitClient(commands, GitConfig())
    git.backup_ref(str(tmp_path), "refs/dtcoder/backups/run/stamp", "a" * 40)
    assert commands.run.call_args.args[0] == [
        "git",
        "update-ref",
        "refs/dtcoder/backups/run/stamp",
        "a" * 40,
        "0" * 40,
    ]
    git.add_worktree_at(str(tmp_path), tmp_path / "next", "agent/next", "b" * 40)
    assert commands.run.call_args.args[0] == [
        "git",
        "worktree",
        "add",
        "-b",
        "agent/next",
        "--",
        str(tmp_path / "next"),
        "b" * 40,
    ]

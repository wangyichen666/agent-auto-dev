"""AntCode 命令适配器；评论和 PR 使用可查询的稳定远端标记。"""

from contextlib import nullcontext
from pathlib import Path

from dtcoder_agentic_dev.adapters.cli_json import JsonCLI, items, marker, required, safe_url, unwrap
from dtcoder_agentic_dev.domain.errors import ConfigurationError, TechnicalError
from dtcoder_agentic_dev.domain.models import Issue
from dtcoder_agentic_dev.infrastructure.locking.file import file_lock
from dtcoder_agentic_dev.ports.code_host import PullRequest


def identity(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # 展示名称不构成平台身份。
        for name in ("id", "user_id", "login", "username", "employee_id", "work_no"):
            if value.get(name) is not None and str(value[name]).strip():
                return str(value[name]).strip()
    return ""


def identities(values):
    if not isinstance(values, list):
        values = [values]
    return list(dict.fromkeys(v for v in map(identity, values) if v))


class AntCodeAdapter:
    def __init__(self, config, repositories, commands, lock_directory=None):
        self.cli = JsonCLI(commands, config.binary, config.timeout, config.profile)
        self.lock_directory = Path(lock_directory) if lock_directory else None
        self.repositories = {r.repository_id: r for r in repositories}

    def _project(self, repository_id):
        try:
            return self.repositories[repository_id].project
        except KeyError:
            raise ConfigurationError("未配置 AntCode 仓库") from None

    def _issue(self, repository_id, data):
        data = unwrap(data)
        number = required(data, "iid", "issue_iid", "number")
        try:
            number = int(number)
            if number <= 0:
                raise ValueError
        except ValueError:
            raise TechnicalError("Issue 编号无效") from None
        label_values = data.get("labels", data.get("label", []))
        if not isinstance(label_values, list):
            label_values = [label_values]
        labels = [
            v if isinstance(v, str) else v.get("name", "")
            for v in label_values
            if isinstance(v, (str, dict))
        ]
        author = identity(data.get("author", ""))
        assignees = identities(data.get("assignees", data.get("assignee", [])))
        reviewers = identities(data.get("reviewers", data.get("reviewer", [])))
        return Issue(
            "antcode",
            repository_id,
            str(data.get("id") or number),
            number,
            required(data, "title"),
            str(data.get("description") or data.get("body") or ""),
            author,
            safe_url(data.get("web_url") or data.get("url") or ""),
            str(data.get("state") or "open"),
            labels,
            assignees,
            raw={"reviewers": reviewers},
        )

    def list_open_issues(self, repository_id):
        self.cli.require(["issue", "list"], ["--all", "--json"])
        data = self.cli.call(
            ["issue", "list", "--project", self._project(repository_id), "--state", "open", "--all"]
        )
        return [self._issue(repository_id, v) for v in items(data)]

    def get_issue(self, repository_id, number):
        return self._issue(
            repository_id,
            self.cli.call(
                ["issue", "view", str(number), "--project", self._project(repository_id)]
            ),
        )

    def _lock(self, key):
        return (
            file_lock(self.lock_directory / (marker(key)[13:-4] + ".lock"))
            if self.lock_directory
            else nullcontext()
        )

    def create_issue_comment(self, repository_id, issue_external_id, body, idempotency_key):
        with self._lock(idempotency_key):
            return self._create_issue_comment(
                repository_id, issue_external_id, body, idempotency_key
            )

    def _create_issue_comment(self, repository_id, issue_external_id, body, idempotency_key):
        self.cli.require(["issue", "comments", "list"], ["--all", "--json"])
        project = self._project(repository_id)
        tag = marker(idempotency_key)
        comments = items(
            self.cli.call(
                ["issue", "comments", "list", str(issue_external_id), "--project", project, "--all"]
            )
        )
        for comment in comments:
            if tag in str(comment.get("body") or comment.get("description") or ""):
                return required(comment, "id", "comment_id")
        result = unwrap(
            self.cli.call(
                [
                    "issue",
                    "comment",
                    str(issue_external_id),
                    "--project",
                    project,
                    "--body",
                    body + "\n" + tag,
                ]
            )
        )
        return required(result, "id", "comment_id")

    def _pr(self, data):
        data = unwrap(data)
        return PullRequest(
            required(data, "iid", "id", "pr_iid"),
            safe_url(required(data, "web_url", "url")),
            (
                "merged"
                if data.get("merged") is True or data.get("merged_at")
                else {"opened": "open", "declined": "closed"}.get(
                    str(data.get("state") or data.get("status") or "unknown").lower(),
                    str(data.get("state") or data.get("status") or "unknown").lower(),
                )
            ),
        )

    def find_pull_request(self, repository_id, idempotency_key):
        self.cli.require(["pr", "list"], ["--all", "--json"])
        tag = marker(idempotency_key)
        for data in items(
            self.cli.call(
                ["pr", "list", "--project", self._project(repository_id), "--state", "all", "--all"]
            )
        ):
            if tag in str(data.get("description") or data.get("body") or ""):
                return self._pr(data)
        return None

    def create_pull_request(
        self,
        repository_id,
        *,
        branch,
        base_branch,
        title,
        body,
        idempotency_key,
        reviewers=(),
        remove_source_branch=False,
    ):
        with self._lock(idempotency_key):
            return self._create_pr(
                repository_id,
                branch=branch,
                base_branch=base_branch,
                title=title,
                body=body,
                idempotency_key=idempotency_key,
                reviewers=reviewers,
                remove_source_branch=remove_source_branch,
            )

    def _create_pr(
        self,
        repository_id,
        *,
        branch,
        base_branch,
        title,
        body,
        idempotency_key,
        reviewers,
        remove_source_branch,
    ):
        existing = self.find_pull_request(repository_id, idempotency_key)
        if existing:
            return existing
        args = [
            "pr",
            "create",
            "--project",
            self._project(repository_id),
            "--source-branch",
            branch,
            "--target-branch",
            base_branch,
            "--title",
            title,
            "--body",
            body + "\n" + marker(idempotency_key),
        ]
        for reviewer in identities(list(reviewers)):
            args += ["--reviewer", reviewer]
        if remove_source_branch:
            args += ["--remove-source-branch"]
        self.cli.require(
            ["pr", "create"],
            ["--json", "--source-branch", "--target-branch"]
            + (["--reviewer"] if reviewers else [])
            + (["--remove-source-branch"] if remove_source_branch else []),
        )
        return self._pr(self.cli.call(args))

    def get_pull_request(self, repository_id, external_id):
        pr = self._pr(
            self.cli.call(["pr", "view", external_id, "--project", self._project(repository_id)])
        )

        if pr.state not in {"open", "closed", "merged"}:
            raise TechnicalError("PR 查询缺少可核验状态，拒绝猜测合并情况")
        return pr

    def close_pull_request(self, repository_id, external_id):
        self.cli.call(["pr", "close", external_id, "--project", self._project(repository_id)])

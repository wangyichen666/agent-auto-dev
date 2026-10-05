"""本地能力诊断；仅显式 dry-run 可以返回带 dry-run 标识的模拟记录。"""

import logging

from dtcoder_agentic_dev.domain.errors import CapabilityNotConfigured
from dtcoder_agentic_dev.domain.models import Issue
from dtcoder_agentic_dev.ports.code_host import PullRequest


class LoggingCodeHostAdapter:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)
        self._prs = {}

    def _require(self):
        if not self.dry_run:
            raise CapabilityNotConfigured("代码托管能力未配置；请注入 CodeHostPort 适配器")

    def list_open_issues(self, repository_id):
        self._require()
        return []

    def get_issue(self, repository_id, number):
        self._require()
        return Issue(
            "dry-run", repository_id, f"dry-run-{number}", number, "演练需求", body="仅供本地演练"
        )

    def create_issue_comment(self, repository_id, issue_external_id, body, idempotency_key):
        self._require()
        self.logger.info("dry-run：模拟 Issue 评论")
        return f"dry-run:{idempotency_key}"

    def create_pull_request(
        self, repository_id, *, branch, base_branch, title, body, idempotency_key
    ):
        self._require()
        key = (repository_id, idempotency_key)
        self._prs.setdefault(
            key, PullRequest(f"dry-run:{idempotency_key}", f"dry-run://pr/{idempotency_key}")
        )
        return self._prs[key]

    def find_pull_request(self, repository_id, idempotency_key):
        self._require()
        return self._prs.get((repository_id, idempotency_key))

    def get_pull_request(self, repository_id, external_id):
        self._require()
        return next(
            pr
            for (repo, _), pr in self._prs.items()
            if repo == repository_id and pr.external_id == external_id
        )

    def close_pull_request(self, repository_id, external_id):
        pr = self.get_pull_request(repository_id, external_id)
        for key, candidate in list(self._prs.items()):
            if candidate == pr:
                self._prs[key] = PullRequest(pr.external_id, pr.url, "closed")

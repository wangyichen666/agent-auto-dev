from dataclasses import dataclass
from typing import Protocol

from dtcoder_agentic_dev.domain.models import Issue


@dataclass(frozen=True)
class PullRequest:
    external_id: str
    url: str
    state: str = "open"


class CodeHostPort(Protocol):
    def list_open_issues(self, repository_id: str) -> list[Issue]: ...
    def get_issue(self, repository_id: str, number: int) -> Issue: ...
    def create_issue_comment(
        self, repository_id: str, issue_external_id: str, body: str, idempotency_key: str
    ) -> str: ...
    def create_pull_request(
        self,
        repository_id: str,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        idempotency_key: str,
        reviewers: list[str] = (),
        remove_source_branch: bool = False,
    ) -> PullRequest: ...
    def find_pull_request(self, repository_id: str, idempotency_key: str) -> PullRequest | None: ...
    def get_pull_request(self, repository_id: str, external_id: str) -> PullRequest: ...
    def close_pull_request(self, repository_id: str, external_id: str) -> None: ...

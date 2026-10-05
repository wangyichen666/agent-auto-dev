import logging


class Poller:
    def __init__(self, config, code_host, run_service):
        self.config, self.host, self.runs = config, code_host, run_service
        self.logger = logging.getLogger(__name__)
        self.last_errors = []

    def poll(self):
        created = []
        self.last_errors = []
        for repo in self.config.repositories:
            try:
                for issue in self.host.list_open_issues(repo.repository_id):
                    if issue.repository_id != repo.repository_id or issue.state != "open":
                        continue
                    if repo.labels and not set(repo.labels).issubset(issue.labels):
                        continue
                    if repo.authors and issue.author not in repo.authors:
                        continue
                    run = self.runs.queue_issue(issue, repo)
                    if run:
                        created.append(run)
            except Exception as exc:
                self.last_errors.append({"repository": repo.name, "error_type": type(exc).__name__})
                self.logger.warning("仓库轮询失败：%s，错误类型=%s", repo.name, type(exc).__name__)
        return created

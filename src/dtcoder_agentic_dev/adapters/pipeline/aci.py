"""ACI CLI：要求可按幂等键查询，并支持服务器按键去重。"""

from dtcoder_agentic_dev.adapters.cli_json import JsonCLI, items, required, safe_url, unwrap
from dtcoder_agentic_dev.domain.errors import TechnicalError
from dtcoder_agentic_dev.ports.pipeline import PipelineResult, PipelineStatus


class ACIAdapter:
    STATUSES = {
        "pending": PipelineStatus.PENDING,
        "queued": PipelineStatus.PENDING,
        "created": PipelineStatus.PENDING,
        "waiting": PipelineStatus.PENDING,
        "running": PipelineStatus.RUNNING,
        "in_progress": PipelineStatus.RUNNING,
        "success": PipelineStatus.SUCCEEDED,
        "succeeded": PipelineStatus.SUCCEEDED,
        "passed": PipelineStatus.SUCCEEDED,
        "failed": PipelineStatus.FAILED,
        "failure": PipelineStatus.FAILED,
        "error": PipelineStatus.FAILED,
        "cancelled": PipelineStatus.CANCELLED,
        "canceled": PipelineStatus.CANCELLED,
    }

    def __init__(self, config, repositories, commands):
        self.cli = JsonCLI(commands, config.binary, config.timeout, banner=True)
        self.repositories = {r.repository_id: r.pipeline for r in repositories}

    def _result(self, data, *, triggered=False):
        data = unwrap(data)
        status = str(data.get("status", data.get("state", "pending" if triggered else ""))).lower()
        if status not in self.STATUSES:
            raise TechnicalError("ACI 返回未知流水线状态")
        return PipelineResult(
            required(data, "pipeline_id", "pipelineId", "id"),
            self.STATUSES[status],
            safe_url(data.get("web_url") or data.get("url") or ""),
        )

    def trigger(self, repository_id, branch, idempotency_key, *, commit=None):
        config = self.repositories[repository_id]
        self.cli.require(["pipeline", "list"], ["--idempotency-key", "--all", "--json"])
        self.cli.require(["pipeline", "trigger"], ["--idempotency-key", "--json"])
        found = items(
            self.cli.call(
                [
                    "pipeline",
                    "list",
                    "--project",
                    config.project,
                    "--idempotency-key",
                    idempotency_key,
                    "--all",
                ]
            )
        )
        matching = [
            v for v in found if v.get("idempotency_key", v.get("idempotencyKey")) == idempotency_key
        ]
        if found and len(matching) != len(found):
            raise TechnicalError("ACI 查询未返回可核验的幂等键，拒绝再次触发")
        if len(matching) > 1:
            raise TechnicalError("ACI 幂等键对应多条流水线，需要人工对账")
        if matching:
            return self._result(matching[0])
        args = [
            "pipeline",
            "trigger",
            "--project",
            config.project,
            "--branch",
            config.branch or branch,
            "--source",
            config.source,
            "--idempotency-key",
            idempotency_key,
        ]
        for name in ("yml_path", "template_id", "yaml_file", "yml_global_path", "env_file"):
            value = getattr(config, name)
            if value:
                args += ["--" + name.replace("_", "-"), value]
        if commit:
            args += ["--commit", commit]
        for key, value in config.params.items():
            args += ["--param", f"{key}={value}"]
        return self._result(self.cli.call(args), triggered=True)

    def get_status(self, repository_id, external_id):
        return self._result(
            self.cli.call(
                [
                    "pipeline",
                    "view",
                    external_id,
                    "--project",
                    self.repositories[repository_id].project,
                ]
            )
        )

    def cancel(self, repository_id, external_id):
        self.cli.call(
            [
                "pipeline",
                "cancel",
                external_id,
                "--project",
                self.repositories[repository_id].project,
            ]
        )

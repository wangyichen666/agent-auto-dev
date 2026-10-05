from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class PipelineStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class PipelineResult:
    external_id: str
    status: PipelineStatus
    url: str = ""


class PipelinePort(Protocol):
    # trigger 必须按此键在远端去重；仅本地记录不能关闭跨系统崩溃窗口。
    def trigger(self, repository_id: str, branch: str, idempotency_key: str) -> PipelineResult: ...
    def get_status(self, repository_id: str, external_id: str) -> PipelineResult: ...
    def cancel(self, repository_id: str, external_id: str) -> None: ...

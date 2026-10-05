"""可注入的通知 HTTP 传输端口。"""

from typing import Protocol


class HttpTransport(Protocol):
    def post(self, url: str, payload: dict, timeout: float) -> None: ...

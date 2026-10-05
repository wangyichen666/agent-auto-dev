"""同步事件分发。主事务完成后才调用处理器，失败详情单独审计。"""

import logging

from dtcoder_agentic_dev.domain.events import DomainEvent


class EventDispatcher:
    def __init__(self, repository, clock, ids, handlers=()):
        self.repository, self.clock, self.ids = repository, clock, ids
        self.handlers = list(handlers)
        self.logger = logging.getLogger(__name__)

    def make(self, type, run_id, step=None, payload=None):
        return DomainEvent(self.ids.new(), type, run_id, self.clock.now(), step, payload or {})

    def publish(self, events):
        for event in events:
            failures = []
            for handler in self.handlers:
                try:
                    handler(event)
                except Exception as exc:
                    failure = {"handler": type(handler).__name__, "error_type": type(exc).__name__}
                    failures.append(failure)
                    self.logger.warning(
                        "事件处理器失败：%s，错误类型=%s",
                        event.type.value,
                        type(exc).__name__,
                        extra={"run_id": event.run_id, "step": event.step or "-"},
                    )
            if failures:
                # 不打印异常字符串，避免通知 SDK 将认证信息放进异常消息。
                event.payload["handler_failures"] = failures
                try:
                    self.repository.save_event(event)
                except Exception:
                    self.logger.warning(
                        "无法持久化事件处理失败详情",
                        extra={"run_id": event.run_id, "step": event.step or "-"},
                    )

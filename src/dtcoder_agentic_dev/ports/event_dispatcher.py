from typing import Protocol

from dtcoder_agentic_dev.domain.events import DomainEvent, EventType


class EventDispatcherPort(Protocol):
    def make(
        self, type: EventType, run_id: str, step: str | None = None, payload: dict | None = None
    ) -> DomainEvent: ...
    def publish(self, events: list[DomainEvent]) -> None: ...

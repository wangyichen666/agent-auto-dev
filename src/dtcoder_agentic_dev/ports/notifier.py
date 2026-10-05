from typing import Protocol

from dtcoder_agentic_dev.domain.events import DomainEvent


class NotifierPort(Protocol):
    def notify(self, event: DomainEvent) -> None: ...

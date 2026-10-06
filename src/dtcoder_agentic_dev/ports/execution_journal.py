from typing import Protocol


class ExecutionJournal(Protocol):
    def save(self, run_id: str, job: str, attempt_number: int, execution: dict) -> dict: ...

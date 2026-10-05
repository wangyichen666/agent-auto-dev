from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    initial_delay: float = 1
    max_delay: float = 30

    def delay(self, retry_number: int) -> float:
        return min(self.max_delay, 30, self.initial_delay * 2 ** max(0, retry_number - 1))

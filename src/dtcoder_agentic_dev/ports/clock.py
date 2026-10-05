from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class IdGenerator(Protocol):
    def new(self) -> str: ...

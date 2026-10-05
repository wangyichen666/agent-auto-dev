import time
import uuid


class SystemClock:
    def now(self) -> float:
        return time.time()


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class UUIDGenerator:
    def new(self) -> str:
        return str(uuid.uuid4())

"""仓库镜像的跨进程文件锁，与单运行数据库租约互补。"""

try:
    import fcntl
except ImportError:
    fcntl = None
import re
from contextlib import contextmanager
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict, ConfigurationError, FatalError


@contextmanager
def file_lock(path: str | Path):
    if fcntl is None:
        raise ConfigurationError("执行锁仅支持 macOS/Linux；Windows 暂不支持后台执行")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class RunExecutionGuard:
    """过期但仍在执行外部命令的 worker 持锁至原子步骤结束。"""

    def __init__(self, directory):
        self.directory = Path(directory)

    def __call__(self, run_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise FatalError("运行 ID 不能用于安全的执行锁路径")
        return file_lock(self.directory / f"{run_id}.lock")

    @contextmanager
    def try_acquire(self, run_id):
        if fcntl is None:
            raise ConfigurationError("回退执行锁仅支持 macOS/Linux")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise FatalError("运行 ID 不能用于执行锁路径")
        target = self.directory / f"{run_id}.lock"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConcurrencyConflict("运行执行锁被占用，拒绝回退") from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

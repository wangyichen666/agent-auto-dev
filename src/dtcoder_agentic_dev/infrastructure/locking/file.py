"""仓库镜像的跨进程文件锁，与单运行数据库租约互补。"""

import fcntl
import re
from contextlib import contextmanager
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import FatalError


@contextmanager
def file_lock(path: str | Path):
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

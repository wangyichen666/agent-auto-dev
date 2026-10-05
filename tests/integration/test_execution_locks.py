import fcntl

import pytest

from dtcoder_agentic_dev.infrastructure.locking.file import RunExecutionGuard


def test_execution_guard_prevents_overlapping_old_worker(tmp_path):
    guard = RunExecutionGuard(tmp_path)
    with guard("run"):
        with (tmp_path / "run.lock").open("a", encoding="utf-8") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    with (tmp_path / "run.lock").open("a", encoding="utf-8") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)

import itertools

import pytest

from dtcoder_agentic_dev.domain.models import Issue, WorkflowRun


class FakeClock:
    def __init__(self, value=1000):
        self.value = value
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class SequenceIds:
    def __init__(self):
        self.sequence = itertools.count(1)

    def new(self):
        return f"id-{next(self.sequence)}"


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def ids():
    return SequenceIds()


@pytest.fixture
def issue():
    return Issue("test", "repo", "external-1", 1, "实现功能", body="需求描述")


@pytest.fixture
def run():
    return WorkflowRun(
        "run-1",
        "issue-development",
        "1",
        "repo",
        "external-1",
        1,
        current_step="requirements",
        created_at=1000,
        updated_at=1000,
    )


@pytest.fixture(autouse=True)
def forbid_real_external_services(monkeypatch):
    """测试默认禁止网络及真实 Codex、Git commit/push/merge/rebase。"""
    import socket
    import subprocess
    from pathlib import Path

    def deny_network(*args, **kwargs):
        raise AssertionError("测试禁止访问真实网络")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    original = subprocess.run

    def guarded_run(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)) and args:
            binary = Path(str(args[0])).name
            if binary == "codex":
                raise AssertionError("测试禁止调用真实 Codex")
            if binary == "git" and any(
                a in {"commit", "push", "merge", "rebase"} for a in args[1:]
            ):
                raise AssertionError("测试禁止执行 Git commit/push/merge/rebase")
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)

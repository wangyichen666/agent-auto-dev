from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict
from dtcoder_agentic_dev.domain.models import ExternalOperation, RunStatus, StepAttempt


@pytest.fixture(params=["memory", "sqlite"])
def repo(request, tmp_path):
    value = (
        InMemoryRunRepository()
        if request.param == "memory"
        else SQLiteRunRepository(tmp_path / "state.db")
    )
    yield value
    if hasattr(value, "close"):
        value.close()


def test_schema_and_roundtrip(repo, run, issue):
    repo.save_repository("repo", {"name": "仓库"})
    repo.save_issue(issue)
    repo.create_run(run)
    assert repo.load_run(run.run_id) == run
    assert repo.load_issue("repo", "external-1") == issue
    if hasattr(repo, "connection"):
        tables = {
            r[0]
            for r in repo.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "repositories",
            "issues",
            "workflow_runs",
            "step_attempts",
            "artifacts",
            "external_operations",
            "domain_events",
            "schema_version",
        } <= tables


def test_transaction_rollback(repo, run):
    repo.create_run(run)
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.save_attempt(StepAttempt("a", run.run_id, "s", 1))
            changed = repo.load_run(run.run_id)
            changed.status = RunStatus.FAILED
            repo.update_run(changed)
            raise RuntimeError("回滚")
    assert repo.list_attempts(run.run_id) == []
    assert repo.load_run(run.run_id).status is RunStatus.QUEUED


def test_lease_expiry_and_conflict(repo, run):
    repo.create_run(run)
    first = repo.claim_run("a", 10, 5)
    assert first.lease_owner == "a"
    assert repo.claim_run("b", 11, 5) is None
    with pytest.raises(ConcurrencyConflict):
        repo.renew_lease(run.run_id, "b", 11, 5)
    repo.renew_lease(run.run_id, "a", 12, 5)
    assert repo.claim_run("b", 16, 5) is None
    assert repo.claim_run("b", 18, 5).lease_owner == "b"
    repo.release_lease(run.run_id, "a")
    assert repo.load_run(run.run_id).lease_owner == "b"
    with pytest.raises(ConcurrencyConflict):
        repo.update_run(first)
    repo.release_lease(run.run_id, "b")
    assert repo.load_run(run.run_id).lease_owner is None


def test_active_issue_and_unique_key(repo, run):
    repo.create_run(run)
    with pytest.raises(ConcurrencyConflict):
        repo.create_run(replace(run, run_id="other"))
    repo.save_operation(ExternalOperation("op1", run.run_id, "pr", "key"))
    with pytest.raises(ConcurrencyConflict):
        repo.save_operation(ExternalOperation("op2", run.run_id, "pr", "key"))
    assert repo.find_operation("key").operation_id == "op1"


def test_cross_connection_atomic_claim(tmp_path, run):
    a = SQLiteRunRepository(tmp_path / "state.db")
    b = SQLiteRunRepository(tmp_path / "state.db")
    a.create_run(run)
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(store.claim_run, owner, 10, 100) for store, owner in [(a, "a"), (b, "b")]
        ]
        assert sum(f.result() is not None for f in futures) == 1
    a.close()
    b.close()


def test_sqlite_event_failure_details_saved(tmp_path, run, clock, ids):
    from dtcoder_agentic_dev.application.services.events import EventDispatcher
    from dtcoder_agentic_dev.domain.events import EventType

    store = SQLiteRunRepository(tmp_path / "events.db")
    store.create_run(run)

    def failed(event):
        raise RuntimeError("失败")

    dispatcher = EventDispatcher(store, clock, ids, [failed])
    event = dispatcher.make(EventType.RUN_STARTED, run.run_id)
    store.save_event(event)
    dispatcher.publish([event])
    assert (
        store.list_events(run.run_id)[0].payload["handler_failures"][0]["error_type"]
        == "RuntimeError"
    )
    store.close()

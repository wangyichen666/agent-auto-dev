"""无外部依赖的测试存储，具备与 SQLite 相同的事务及租约语义。"""

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict
from dtcoder_agentic_dev.domain.models import ACTIVE_STATUSES, RunStatus


class InMemoryRunRepository:
    def __init__(self):
        self._data = {
            key: {}
            for key in (
                "repositories",
                "issues",
                "runs",
                "attempts",
                "artifacts",
                "operations",
                "events",
            )
        }
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            snapshot = deepcopy(self._data)
            try:
                yield
            except BaseException:
                self._data = snapshot
                raise

    def save_repository(self, repository_id, data):
        with self._lock:
            self._data["repositories"][repository_id] = deepcopy(data)

    def save_issue(self, issue):
        with self._lock:
            self._data["issues"][(issue.repository_id, issue.external_id)] = deepcopy(issue)

    def load_issue(self, repository_id, external_id):
        with self._lock:
            return deepcopy(self._data["issues"][(repository_id, external_id)])

    def _check_active(self, run):
        if run.status in ACTIVE_STATUSES and any(
            r.run_id != run.run_id
            and r.repository_id == run.repository_id
            and r.issue_external_id == run.issue_external_id
            and r.status in ACTIVE_STATUSES
            for r in self._data["runs"].values()
        ):
            raise ConcurrencyConflict("此 Issue 已有活动运行")

    def create_run(self, run):
        with self.transaction():
            if run.run_id in self._data["runs"]:
                raise ConcurrencyConflict("运行已存在")
            self._check_active(run)
            self._data["runs"][run.run_id] = deepcopy(run)

    def load_run(self, run_id):
        with self._lock:
            return deepcopy(self._data["runs"][run_id])

    def update_run(self, run):
        with self.transaction():
            old = self._data["runs"][run.run_id]
            if old.revision != run.revision:
                raise ConcurrencyConflict("运行版本冲突")
            self._check_active(run)
            saved = deepcopy(run)
            saved.revision += 1
            saved.lease_owner, saved.lease_expires_at = old.lease_owner, old.lease_expires_at
            self._data["runs"][run.run_id] = saved
            run.revision = saved.revision

    def list_runs(self, repository_id=None):
        with self._lock:
            return deepcopy(
                sorted(
                    (
                        r
                        for r in self._data["runs"].values()
                        if repository_id is None or r.repository_id == repository_id
                    ),
                    key=lambda r: (r.created_at, r.run_id),
                )
            )

    def claim_run(self, owner, now, lease_seconds, run_id=None):
        with self.transaction():
            for run in self.list_runs():
                if run_id is not None and run.run_id != run_id:
                    continue
                if run.status not in {RunStatus.QUEUED, RunStatus.WAITING, RunStatus.RUNNING}:
                    continue
                if run.lease_owner and (run.lease_expires_at or 0) > now:
                    continue
                if (run.context.get("next_poll_at") or 0) > now:
                    continue
                run.lease_owner, run.lease_expires_at = owner, now + lease_seconds
                run.revision += 1
                self._data["runs"][run.run_id] = run
                return deepcopy(run)
            return None

    def assert_lease(self, run_id, owner, now):
        run = self.load_run(run_id)
        if run.lease_owner != owner or (run.lease_expires_at or 0) <= now:
            raise ConcurrencyConflict("运行租约已失效")

    def renew_lease(self, run_id, owner, now, lease_seconds):
        with self.transaction():
            self.assert_lease(run_id, owner, now)
            self._data["runs"][run_id].lease_expires_at = now + lease_seconds

    def release_lease(self, run_id, owner):
        with self.transaction():
            run = self._data["runs"][run_id]
            if run.lease_owner == owner:
                run.lease_owner = run.lease_expires_at = None

    def save_attempt(self, attempt):
        with self.transaction():
            if any(
                a.attempt_id != attempt.attempt_id
                and (a.run_id, a.step_name, a.attempt_number)
                == (attempt.run_id, attempt.step_name, attempt.attempt_number)
                for a in self._data["attempts"].values()
            ):
                raise ConcurrencyConflict("步骤尝试序号重复")
            self._data["attempts"][attempt.attempt_id] = deepcopy(attempt)

    def save_artifact(self, artifact):
        with self._lock:
            self._data["artifacts"][artifact.artifact_id] = deepcopy(artifact)

    def save_operation(self, operation):
        with self.transaction():
            existing = self.find_operation(operation.idempotency_key)
            if existing and existing.operation_id != operation.operation_id:
                raise ConcurrencyConflict("外部操作幂等键已存在")
            self._data["operations"][operation.operation_id] = deepcopy(operation)

    def find_operation(self, idempotency_key):
        with self._lock:
            return next(
                (
                    deepcopy(op)
                    for op in self._data["operations"].values()
                    if op.idempotency_key == idempotency_key
                ),
                None,
            )

    def save_event(self, event):
        with self._lock:
            self._data["events"][event.event_id] = deepcopy(event)

    def _list(self, kind, run_id):
        with self._lock:
            return deepcopy([x for x in self._data[kind].values() if x.run_id == run_id])

    def list_attempts(self, run_id):
        return self._list("attempts", run_id)

    def list_artifacts(self, run_id):
        return self._list("artifacts", run_id)

    def list_operations(self, run_id):
        return self._list("operations", run_id)

    def list_events(self, run_id):
        return self._list("events", run_id)

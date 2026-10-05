"""SQLite 工作流存储：集中转换、乐观版本、原子租约和嵌套事务。"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict, ConfigurationError
from dtcoder_agentic_dev.domain.events import DomainEvent, EventType
from dtcoder_agentic_dev.domain.models import (
    Artifact,
    ExternalOperation,
    Issue,
    StepAttempt,
    WorkflowRun,
    from_dict,
    to_dict,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS repositories(repository_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issues(
 repository_id TEXT NOT NULL, external_id TEXT NOT NULL, number INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(repository_id,external_id));
CREATE INDEX IF NOT EXISTS issue_number ON issues(repository_id,number);
CREATE TABLE IF NOT EXISTS workflow_runs(
 run_id TEXT PRIMARY KEY, repository_id TEXT NOT NULL, issue_external_id TEXT NOT NULL,
 status TEXT NOT NULL, revision INTEGER NOT NULL, next_poll_at REAL,
 lease_owner TEXT, lease_expires_at REAL, created_at REAL NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS run_dispatch ON workflow_runs(status,next_poll_at,lease_expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_issue ON workflow_runs(repository_id,issue_external_id)
 WHERE status IN ('QUEUED','RUNNING','WAITING','PAUSED');
CREATE TABLE IF NOT EXISTS step_attempts(
 attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
 step_name TEXT NOT NULL, attempt_number INTEGER NOT NULL, payload TEXT NOT NULL,
 UNIQUE(run_id,step_name,attempt_number));
CREATE TABLE IF NOT EXISTS artifacts(
 artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
 payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS external_operations(
 operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
 idempotency_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS operations_run ON external_operations(run_id);
CREATE TABLE IF NOT EXISTS domain_events(
 event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
 payload TEXT NOT NULL);
"""


def _encode(value) -> str:
    return json.dumps(
        asdict(value) if hasattr(value, "__dataclass_fields__") else value,
        ensure_ascii=False,
        sort_keys=True,
    )


def _decode(cls, row):
    data = json.loads(row["payload"])
    if cls is WorkflowRun:
        for key in ("status", "revision", "lease_owner", "lease_expires_at"):
            data[key] = row[key]
    if cls is DomainEvent:
        data["type"] = EventType(data["type"])
        return DomainEvent(**data)
    return from_dict(cls, data)


class SQLiteRunRepository:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(path), isolation_level=None, timeout=5, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._depth = 0
        self.connection.executescript(SCHEMA)
        with self.transaction():
            versions = self.connection.execute("SELECT version FROM schema_version").fetchall()
            if not versions:
                self.connection.execute("INSERT INTO schema_version VALUES(1)")
            elif len(versions) != 1 or versions[0][0] != 1:
                raise ConfigurationError("不支持此 SQLite schema 版本；需要显式升级")

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            level = self._depth
            self.connection.execute(
                "BEGIN IMMEDIATE" if level == 0 else f"SAVEPOINT nested_{level}"
            )
            self._depth += 1
            try:
                yield
            except BaseException:
                self.connection.execute("ROLLBACK" if level == 0 else f"ROLLBACK TO nested_{level}")
                if level:
                    self.connection.execute(f"RELEASE nested_{level}")
                raise
            else:
                self.connection.execute("COMMIT" if level == 0 else f"RELEASE nested_{level}")
            finally:
                self._depth -= 1

    def save_repository(self, repository_id: str, data: dict) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO repositories VALUES(?,?) ON CONFLICT(repository_id) "
                "DO UPDATE SET payload=excluded.payload",
                (repository_id, _encode(data)),
            )

    def save_issue(self, issue: Issue) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO issues VALUES(?,?,?,?) ON CONFLICT(repository_id,external_id) "
                "DO UPDATE SET number=excluded.number,payload=excluded.payload",
                (issue.repository_id, issue.external_id, issue.number, _encode(issue)),
            )

    def load_issue(self, repository_id: str, external_id: str) -> Issue:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM issues WHERE repository_id=? AND external_id=?",
                (repository_id, external_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Issue 不存在：{repository_id}/{external_id}")
            return _decode(Issue, row)

    def create_run(self, run: WorkflowRun) -> None:
        with self.transaction():
            try:
                self.connection.execute(
                    "INSERT INTO workflow_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        run.run_id,
                        run.repository_id,
                        run.issue_external_id,
                        run.status.value,
                        run.revision,
                        run.context.get("next_poll_at"),
                        run.lease_owner,
                        run.lease_expires_at,
                        run.created_at,
                        _encode(run),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyConflict("运行已存在或此 Issue 已有活动运行") from exc

    def load_run(self, run_id: str) -> WorkflowRun:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"运行不存在：{run_id}")
            return _decode(WorkflowRun, row)

    def update_run(self, run: WorkflowRun) -> None:
        with self.transaction():
            payload = to_dict(run)
            payload["revision"] = run.revision + 1
            try:
                count = self.connection.execute(
                    "UPDATE workflow_runs SET status=?,revision=revision+1,next_poll_at=?,payload=? "
                    "WHERE run_id=? AND revision=?",
                    (
                        run.status.value,
                        run.context.get("next_poll_at"),
                        _encode(payload),
                        run.run_id,
                        run.revision,
                    ),
                ).rowcount
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyConflict("此 Issue 已有其他活动运行") from exc
            if count != 1:
                raise ConcurrencyConflict(f"运行版本冲突：{run.run_id}")
            run.revision += 1

    def list_runs(self, repository_id: str | None = None) -> list[WorkflowRun]:
        with self._lock:
            query = "SELECT * FROM workflow_runs"
            params = ()
            if repository_id is not None:
                query += " WHERE repository_id=?"
                params = (repository_id,)
            rows = self.connection.execute(query + " ORDER BY created_at,run_id", params).fetchall()
            return [_decode(WorkflowRun, r) for r in rows]

    def claim_run(
        self, owner: str, now: float, lease_seconds: float, run_id: str | None = None
    ) -> WorkflowRun | None:
        with self.transaction():
            query = (
                "SELECT run_id FROM workflow_runs WHERE status IN ('QUEUED','WAITING','RUNNING') "
                "AND (lease_owner IS NULL OR lease_expires_at<=?) "
                "AND (next_poll_at IS NULL OR next_poll_at<=?)"
            )
            params: list = [now, now]
            if run_id is not None:
                query += " AND run_id=?"
                params.append(run_id)
            row = self.connection.execute(
                query + " ORDER BY created_at,run_id LIMIT 1", params
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE workflow_runs SET lease_owner=?,lease_expires_at=?,"
                "revision=revision+1 WHERE run_id=?",
                (owner, now + lease_seconds, row[0]),
            )
            return self.load_run(row[0])

    def assert_lease(self, run_id: str, owner: str, now: float) -> None:
        run = self.load_run(run_id)
        if run.lease_owner != owner or run.lease_expires_at is None or run.lease_expires_at <= now:
            raise ConcurrencyConflict(f"运行租约已失效：{run_id}")

    def renew_lease(self, run_id: str, owner: str, now: float, lease_seconds: float) -> None:
        with self.transaction():
            count = self.connection.execute(
                "UPDATE workflow_runs SET lease_expires_at=? "
                "WHERE run_id=? AND lease_owner=? AND lease_expires_at>?",
                (now + lease_seconds, run_id, owner, now),
            ).rowcount
            if count != 1:
                raise ConcurrencyConflict("不能续租：租约已失效或不属于当前 worker")

    def release_lease(self, run_id: str, owner: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE workflow_runs SET lease_owner=NULL,lease_expires_at=NULL "
                "WHERE run_id=? AND lease_owner=?",
                (run_id, owner),
            )

    def save_attempt(self, attempt: StepAttempt) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO step_attempts VALUES(?,?,?,?,?) ON CONFLICT(attempt_id) "
                "DO UPDATE SET payload=excluded.payload",
                (
                    attempt.attempt_id,
                    attempt.run_id,
                    attempt.step_name,
                    attempt.attempt_number,
                    _encode(attempt),
                ),
            )

    def save_artifact(self, artifact: Artifact) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?) ON CONFLICT(artifact_id) "
                "DO UPDATE SET payload=excluded.payload",
                (artifact.artifact_id, artifact.run_id, _encode(artifact)),
            )

    def save_operation(self, operation: ExternalOperation) -> None:
        with self.transaction():
            try:
                self.connection.execute(
                    "INSERT INTO external_operations VALUES(?,?,?,?) ON CONFLICT(operation_id) "
                    "DO UPDATE SET payload=excluded.payload",
                    (
                        operation.operation_id,
                        operation.run_id,
                        operation.idempotency_key,
                        _encode(operation),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyConflict("外部操作幂等键已存在") from exc

    def find_operation(self, idempotency_key: str) -> ExternalOperation | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM external_operations WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return _decode(ExternalOperation, row) if row else None

    def save_event(self, event: DomainEvent) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO domain_events VALUES(?,?,?) ON CONFLICT(event_id) "
                "DO UPDATE SET payload=excluded.payload",
                (event.event_id, event.run_id, _encode(event)),
            )

    def _list(self, table: str, cls, run_id: str):
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
            return [_decode(cls, row) for row in rows]

    def list_attempts(self, run_id):
        return self._list("step_attempts", StepAttempt, run_id)

    def list_artifacts(self, run_id):
        return self._list("artifacts", Artifact, run_id)

    def list_operations(self, run_id):
        return self._list("external_operations", ExternalOperation, run_id)

    def list_events(self, run_id):
        return self._list("domain_events", DomainEvent, run_id)

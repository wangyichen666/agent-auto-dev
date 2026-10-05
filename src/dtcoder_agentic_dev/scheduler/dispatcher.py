import logging
from contextlib import nullcontext
from threading import Event, Thread

from dtcoder_agentic_dev.domain.errors import ConcurrencyConflict
from dtcoder_agentic_dev.domain.events import EventType
from dtcoder_agentic_dev.domain.models import TERMINAL_STATUSES, RunStatus


class LeaseHeartbeat:
    """长命令执行期间续租；停止等待可中断，线程不决定业务状态。"""

    def __init__(self, store, clock, run_id, owner, lease_seconds, interval):
        self.store, self.clock, self.run_id, self.owner = store, clock, run_id, owner
        self.lease_seconds, self.interval = lease_seconds, interval
        self.stop_event = Event()
        self.error = None
        self.thread = Thread(target=self._loop, daemon=True, name="dtcoder-lease")

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.store.renew_lease(
                    self.run_id, self.owner, self.clock.now(), self.lease_seconds
                )
            except Exception as exc:
                self.error = exc
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join()


class Dispatcher:
    def __init__(
        self,
        store,
        config,
        workspace,
        runner,
        events,
        clock,
        owner,
        heartbeat_factory=LeaseHeartbeat,
        execution_guard=None,
    ):
        self.store, self.config, self.workspace, self.runner = store, config, workspace, runner
        self.events, self.clock, self.owner = events, clock, owner
        self.heartbeat_factory = heartbeat_factory
        self.execution_guard = execution_guard or (lambda run_id: nullcontext())
        self.logger = logging.getLogger(__name__)

    def dispatch_once(self, run_id=None):
        if getattr(self.config, "dry_run", False):
            return None
        run = self.store.claim_run(
            self.owner, self.clock.now(), self.config.scheduler.lease_seconds, run_id
        )
        if run is None:
            return None
        try:
            with (
                self.heartbeat_factory(
                    self.store,
                    self.clock,
                    run.run_id,
                    self.owner,
                    self.config.scheduler.lease_seconds,
                    self.config.scheduler.heartbeat_interval,
                ) as heartbeat,
                self.execution_guard(run.run_id),
            ):
                self.store.assert_lease(run.run_id, self.owner, self.clock.now())
                repo = next(
                    r for r in self.config.repositories if r.repository_id == run.repository_id
                )
                path = (
                    self.workspace.recover(run, repo)
                    if run.workspace_path
                    else self.workspace.prepare(run, repo)
                )
                with self.store.transaction():
                    self.store.assert_lease(run.run_id, self.owner, self.clock.now())
                    latest = self.store.load_run(run.run_id)
                    latest.workspace_path = path
                    if not latest.context.get("base_revision") and run.context.get("base_revision"):
                        latest.context["base_revision"] = run.context["base_revision"]
                    latest.updated_at = self.clock.now()
                    self.store.update_run(latest)
                if heartbeat.error:
                    raise ConcurrencyConflict("准备工作区时丢失租约")
                return self.runner.run(
                    run.run_id,
                    self.store.load_issue(run.repository_id, run.issue_external_id),
                    repo,
                    self.owner,
                )
        except ConcurrencyConflict:
            self.logger.warning(
                "运行租约或版本冲突，当前 worker 停止执行", extra={"run_id": run.run_id}
            )
            return self.store.load_run(run.run_id)
        except Exception as exc:
            event = None
            with self.store.transaction():
                latest = self.store.load_run(run.run_id)
                # 过期 worker 不允许覆盖其他 worker 的结果。
                try:
                    self.store.assert_lease(run.run_id, self.owner, self.clock.now())
                except ConcurrencyConflict:
                    return latest
                if latest.status not in TERMINAL_STATUSES and latest.status is not RunStatus.PAUSED:
                    latest.status, latest.finished_at = RunStatus.FAILED, self.clock.now()
                    latest.last_error = f"{type(exc).__name__}：{exc}"
                    latest.updated_at = self.clock.now()
                    self.store.update_run(latest)
                    event = self.events.make(
                        EventType.RUN_FAILED,
                        latest.run_id,
                        payload={"error_type": type(exc).__name__},
                    )
                    self.store.save_event(event)
            if event:
                self.events.publish([event])
            self.logger.warning(
                "单个运行执行失败：%s", type(exc).__name__, extra={"run_id": run.run_id}
            )
            return self.store.load_run(run.run_id)
        finally:
            self.store.release_lease(run.run_id, self.owner)

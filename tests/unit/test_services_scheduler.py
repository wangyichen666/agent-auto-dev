from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from dtcoder_agentic_dev.adapters.code_host.logging import LoggingCodeHostAdapter
from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
from dtcoder_agentic_dev.adapters.pipeline.disabled import DisabledPipelineAdapter
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.application.services.runs import RunService
from dtcoder_agentic_dev.config import AppConfig, RepoConfig
from dtcoder_agentic_dev.domain.errors import CapabilityNotConfigured, ConfigurationError
from dtcoder_agentic_dev.domain.models import RunStatus
from dtcoder_agentic_dev.scheduler.dispatcher import Dispatcher, LeaseHeartbeat
from dtcoder_agentic_dev.scheduler.poller import Poller
from dtcoder_agentic_dev.scheduler.runner import SchedulerRunner


def setup(issue, clock, ids):
    repo = RepoConfig("sample", "local", labels=["ready"], authors=["alice"])
    repo.repository_id = issue.repository_id
    config = AppConfig(repositories=[repo])
    store = InMemoryRunRepository()
    host = Mock()
    host.list_open_issues.return_value = [issue]
    events = EventDispatcher(store, clock, ids)
    service = RunService(store, config, host, Mock(), events, clock, ids)
    return config, store, host, events, service


def test_filter_dedup_and_explicit_retry(issue, clock, ids):
    config, store, host, events, service = setup(issue, clock, ids)
    poller = Poller(config, host, service)
    assert poller.poll() == []
    issue.labels = ["ready"]
    issue.author = "alice"
    assert len(poller.poll()) == 1
    assert poller.poll() == []
    first = store.list_runs()[0]
    service.control(first.run_id, "cancel")
    assert poller.poll() == []
    second = service.retry(first.run_id)
    assert second.run_id != first.run_id and len(store.list_runs()) == 2
    assert store.load_run(first.run_id).status is RunStatus.CANCELLED


def test_legal_control_and_cleanup(issue, clock, ids):
    config, store, host, events, service = setup(issue, clock, ids)
    run = service.queue_issue(issue, config.repositories[0], explicit=True)
    assert service.control(run.run_id, "pause").status is RunStatus.PAUSED
    with pytest.raises(ConfigurationError):
        service.cleanup(run.run_id)
    assert service.control(run.run_id, "resume").status is RunStatus.QUEUED
    service.control(run.run_id, "cancel")
    service.cleanup(run.run_id)
    service.workspace.cleanup.assert_called_once()
    assert store.load_run(run.run_id).context["workspace_cleaned"]


def test_dispatcher_failure_isolated_and_releases_lease(issue, clock, ids):
    config, store, host, events, service = setup(issue, clock, ids)
    first = service.queue_issue(issue, config.repositories[0], explicit=True)
    workspace = Mock()
    workspace.prepare.side_effect = RuntimeError("工作区异常")
    dispatcher = Dispatcher(
        store,
        config,
        workspace,
        Mock(),
        events,
        clock,
        "worker",
        heartbeat_factory=lambda *args: nullcontext(Mock(error=None)),
    )
    assert dispatcher.dispatch_once().status is RunStatus.FAILED
    assert store.load_run(first.run_id).lease_owner is None
    second = service.retry(first.run_id)
    assert dispatcher.dispatch_once().run_id == second.run_id


def test_local_adapters_do_not_fake_success():
    host = LoggingCodeHostAdapter()
    with pytest.raises(CapabilityNotConfigured):
        host.list_open_issues("r")
    with pytest.raises(CapabilityNotConfigured):
        host.create_pull_request(
            "r", branch="a", base_branch="m", title="a", body="b", idempotency_key="k"
        )
    with pytest.raises(CapabilityNotConfigured):
        DisabledPipelineAdapter().trigger("r", "b", "k")
    dry = LoggingCodeHostAdapter(True)
    assert dry.get_issue("r", 1).external_id == "dry-run-1"


def test_scheduler_signal_and_run_once_no_repeated_task():
    poller = Mock()
    dispatcher = Mock()
    sleeper = Mock()
    dispatcher.store.list_runs.return_value = []
    scheduler = SchedulerRunner(poller, dispatcher, sleeper, 30)
    scheduler.run_once()
    poller.poll.assert_called_once()
    scheduler.stop()
    scheduler.run_forever()
    sleeper.sleep.assert_not_called()


def test_heartbeat_renew_call_without_real_wait(issue, clock, ids, mocker):
    config, store, host, events, service = setup(issue, clock, ids)
    run = service.queue_issue(issue, config.repositories[0], explicit=True)
    store.claim_run("worker", clock.now(), 120)
    heartbeat = LeaseHeartbeat(store, clock, run.run_id, "worker", 120, 1)
    mocker.patch.object(heartbeat.stop_event, "wait", side_effect=[False, True])
    clock.value += 10
    heartbeat._loop()
    assert store.load_run(run.run_id).lease_expires_at == 1130


def test_comment_handler_idempotent_and_failure_warning(issue, clock, ids):
    from dtcoder_agentic_dev.application.services.comments import IssueCommentHandler
    from dtcoder_agentic_dev.domain.events import EventType

    config, store, host, events, service = setup(issue, clock, ids)
    run = service.queue_issue(issue, config.repositories[0], explicit=True)
    host.create_issue_comment.return_value = "comment-1"
    handler = IssueCommentHandler(host, store, clock, ids)
    event = events.make(EventType.RUN_STARTED, run.run_id)
    handler(event)
    handler(event)
    host.create_issue_comment.assert_called_once()
    assert len(store.list_operations(run.run_id)) == 1
    host.create_issue_comment.side_effect = RuntimeError("通知错误")
    failed = events.make(EventType.RUN_FAILED, run.run_id)
    store.save_event(failed)
    events.handlers = [handler]
    events.publish([failed])
    assert store.list_events(run.run_id)[-1].payload["handler_failures"]
    assert store.load_run(run.run_id).status is RunStatus.QUEUED


def test_resume_pipeline_waiting_keeps_poll_time(issue, clock, ids):
    config, store, host, events, service = setup(issue, clock, ids)
    run = service.queue_issue(issue, config.repositories[0], explicit=True)
    changed = store.load_run(run.run_id)
    changed.status = RunStatus.WAITING
    changed.context["next_poll_at"] = 1030
    store.update_run(changed)
    service.control(run.run_id, "pause")
    assert service.control(run.run_id, "resume").status is RunStatus.WAITING
    assert store.claim_run("worker", 1000, 100) is None
    assert store.claim_run("worker", 1030, 100) is not None

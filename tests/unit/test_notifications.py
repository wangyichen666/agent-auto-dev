from unittest.mock import Mock

import pytest

from dtcoder_agentic_dev.adapters.notification.dingtalk import DingTalkNotifier, UrllibHttpTransport
from dtcoder_agentic_dev.adapters.persistence.memory import InMemoryRunRepository
from dtcoder_agentic_dev.application.services.comments import IssueCommentHandler
from dtcoder_agentic_dev.application.services.event_views import EventViewBuilder
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.application.services.initialization import initialize
from dtcoder_agentic_dev.config import NotificationConfig
from dtcoder_agentic_dev.domain.errors import TechnicalError
from dtcoder_agentic_dev.domain.events import DomainEvent, EventType


def context(run, issue, clock, ids):
    store = InMemoryRunRepository()
    store.create_run(run)
    store.save_issue(issue)
    event = DomainEvent(ids.new(), EventType.RUN_STARTED, run.run_id, clock.now())
    return store, event


def test_shared_template_and_idempotent_comments(tmp_path, run, issue, clock, ids):
    initialize(tmp_path / "config.yaml")
    template = tmp_path / "comments/run_started.txt"
    template.write_text("运行 {run_id} 在 {branch} 已开始", encoding="utf-8")
    store, event = context(run, issue, clock, ids)
    views = EventViewBuilder(store, tmp_path / "comments")
    host, http = Mock(), Mock()
    host.create_issue_comment.return_value = "comment"
    issue.assignees = ["123"]
    store.save_issue(issue)
    handler = IssueCommentHandler(host, store, clock, ids, views)
    handler(event)
    handler(event)
    DingTalkNotifier(
        NotificationConfig(api_url="https://example.invalid", card_template_id="card"), views, http
    ).notify(event)
    assert host.create_issue_comment.call_count == 1
    assert (
        host.create_issue_comment.call_args.args[2]
        == http.post.call_args.args[1]["cardData"]["cardParamMap"]["content"]
    )
    assert http.post.call_args.args[1]["receiverUserIdList"] == ["000123"]


def test_missing_variable_is_audited_without_run_failure(tmp_path, run, issue, clock, ids):
    initialize(tmp_path / "config.yaml")
    (tmp_path / "comments/run_started.txt").write_text("{missing}")
    store, event = context(run, issue, clock, ids)
    host = Mock()
    dispatcher = EventDispatcher(
        store,
        clock,
        ids,
        [
            IssueCommentHandler(
                host, store, clock, ids, EventViewBuilder(store, tmp_path / "comments")
            )
        ],
    )
    before = store.load_run(run.run_id)
    dispatcher.publish([event])
    assert store.load_run(run.run_id) == before
    assert (
        store.list_events(run.run_id)[0].payload["handler_failures"][0]["error_type"]
        == "TechnicalError"
    )
    host.create_issue_comment.assert_not_called()


@pytest.mark.parametrize(
    "receivers,policy,configured,expected",
    [
        ([], "assignees", [], []),
        (["1", "", "000001", "user"], "assignees", [], ["000001", "user"]),
        (["1"], "configured", ["2"], ["000002"]),
        (["1"], "both", ["1", "2"], ["000001", "000002"]),
    ],
)
def test_receivers(run, issue, clock, ids, receivers, policy, configured, expected):
    issue.assignees = receivers
    store, event = context(run, issue, clock, ids)
    http = Mock()
    config = NotificationConfig(recipients=policy, receiver_ids=configured)
    DingTalkNotifier(config, EventViewBuilder(store), http).notify(event)
    if expected:
        assert http.post.call_args.args[1]["receiverUserIdList"] == expected
    else:
        http.post.assert_not_called()


@pytest.mark.parametrize("failure_count", [1, 2])
def test_retry_safe_and_bounded(run, issue, clock, ids, failure_count, caplog):
    issue.assignees = ["1"]
    store, event = context(run, issue, clock, ids)
    http = Mock()
    http.post.side_effect = [OSError("Authorization private token cookie")] * failure_count + [None]
    notifier = DingTalkNotifier(NotificationConfig(), EventViewBuilder(store), http)
    dispatcher = EventDispatcher(store, clock, ids, [notifier.notify])
    dispatcher.publish([event])
    assert http.post.call_count == 2
    assert "private token" not in caplog.text
    assert store.load_run(run.run_id).status == run.status
    if failure_count == 2:
        assert store.list_events(run.run_id)[0].payload["handler_failures"]


def test_http_offline_fails_safely():
    with pytest.raises(TechnicalError) as caught:
        UrllibHttpTransport().post("https://example.invalid", {"private": "secret"}, 0.01)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "status,fragment", [("SUCCEEDED", "已通过"), ("FAILED", "未通过"), ("CANCELLED", "未通过")]
)
def test_pipeline_message_facts(run, issue, clock, ids, status, fragment):
    store, event = context(run, issue, clock, ids)
    event.type = EventType.PIPELINE_FINISHED
    event.payload["pipeline_status"] = status
    assert fragment in EventViewBuilder(store).build(event)["text"]


def test_dingtalk_direct_card_and_auth_not_persisted(run, issue, clock, ids, monkeypatch):
    issue.assignees = ["123"]
    store, event = context(run, issue, clock, ids)
    config = NotificationConfig(
        api_mode="direct",
        access_token_env="TEST_DING_TOKEN",
        api_url="https://api.dingtalk.com/v1.0/card/instances/createAndDeliver",
        card_template_id="card",
    )
    http = Mock()
    notifier = DingTalkNotifier(config, EventViewBuilder(store), http)
    notifier.notify(event)
    payload = http.post.call_args.args[1]
    assert payload["openSpaceId"] == "dtv1.card//IM_ROBOT.000123"
    assert "receiverUserIdList" not in payload
    monkeypatch.setenv("TEST_DING_TOKEN", "secret-value")
    response = Mock(status=200)
    response.read.return_value = b'{"result": {"success": true}}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    send = Mock(return_value=response)
    monkeypatch.setattr("dtcoder_agentic_dev.adapters.notification.dingtalk.urlopen", send)
    UrllibHttpTransport(config.access_token_env).post(config.api_url, payload, 10)
    request = send.call_args.args[0]
    assert request.get_header("X-acs-dingtalk-access-token") == "secret-value"
    assert "secret-value" not in str(payload)


def test_http_empty_success_not_fabricated(monkeypatch):
    response = Mock(status=200)
    response.read.return_value = b"{}"
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        "dtcoder_agentic_dev.adapters.notification.dingtalk.urlopen", Mock(return_value=response)
    )
    with pytest.raises(TechnicalError):
        UrllibHttpTransport().post("https://example.invalid", {}, 1)

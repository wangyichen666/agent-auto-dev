"""产品组合根：所有具体依赖集中装配，CLI 命令只调用用例。"""

from dataclasses import dataclass
from pathlib import Path

from dtcoder_agentic_dev.adapters.code_host.antcode import AntCodeAdapter
from dtcoder_agentic_dev.adapters.code_host.logging import LoggingCodeHostAdapter
from dtcoder_agentic_dev.adapters.codex.adapter import CodexAdapter
from dtcoder_agentic_dev.adapters.notification.dingtalk import DingTalkNotifier
from dtcoder_agentic_dev.adapters.notification.null import NullNotifier
from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.adapters.pipeline.aci import ACIAdapter
from dtcoder_agentic_dev.adapters.pipeline.disabled import DisabledPipelineAdapter
from dtcoder_agentic_dev.application.services.comments import IssueCommentHandler
from dtcoder_agentic_dev.application.services.event_views import EventViewBuilder
from dtcoder_agentic_dev.application.services.events import EventDispatcher
from dtcoder_agentic_dev.application.services.pipeline_control import PipelineCancellationHandler
from dtcoder_agentic_dev.application.services.runs import RunService
from dtcoder_agentic_dev.application.workflows import issue_development_workflow
from dtcoder_agentic_dev.config import load_config
from dtcoder_agentic_dev.domain.errors import CapabilityNotConfigured
from dtcoder_agentic_dev.domain.workflow import StepServices
from dtcoder_agentic_dev.engine.registry import StepRegistry
from dtcoder_agentic_dev.engine.retry import RetryPolicy
from dtcoder_agentic_dev.engine.runner import WorkflowRunner
from dtcoder_agentic_dev.engine.steps.development import (
    CodingStep,
    FixStep,
    RequirementsStep,
    ReviewStep,
)
from dtcoder_agentic_dev.engine.steps.external import CreatePRStep, PipelineStep
from dtcoder_agentic_dev.infrastructure.filesystem.artifacts import FileArtifactStore
from dtcoder_agentic_dev.infrastructure.git.client import GitClient
from dtcoder_agentic_dev.infrastructure.git.workspace import GitWorktreeWorkspaceManager
from dtcoder_agentic_dev.infrastructure.locking.file import RunExecutionGuard
from dtcoder_agentic_dev.infrastructure.logging.setup import RunAuditHandler, configure_logging
from dtcoder_agentic_dev.infrastructure.process.command import SubprocessCommandRunner
from dtcoder_agentic_dev.infrastructure.process.runtime import (
    SystemClock,
    SystemSleeper,
    UUIDGenerator,
)
from dtcoder_agentic_dev.prompts.renderer import StrictPromptRenderer
from dtcoder_agentic_dev.scheduler.dispatcher import Dispatcher
from dtcoder_agentic_dev.scheduler.poller import Poller
from dtcoder_agentic_dev.scheduler.runner import SchedulerRunner


@dataclass
class Runtime:
    config: object
    store: object
    runs: object
    scheduler: object
    dispatcher: object
    commands: object
    renderer: object
    git: object

    def close(self):
        closer = getattr(self.store, "close", None)
        if closer is not None:
            closer()


def build_runtime(
    config_path,
    *,
    code_host=None,
    agent_executor=None,
    pipeline=None,
    notifier=None,
    commands=None,
    clock=None,
    sleeper=None,
    ids=None,
    store=None,
    workspace=None,
    git=None,
    console_logging=True,
):
    config = load_config(config_path)
    for injected, name, built_in in [
        (code_host, config.code_host.adapter, {"logging", "antcode"}),
        (pipeline, config.pipeline.adapter, {"disabled", "aci"}),
        (notifier, config.notification.adapter, {"null", "dingtalk"}),
    ]:
        if injected is None and name not in built_in and not config.dry_run:
            raise CapabilityNotConfigured(f"适配器 {name} 尚未装配，请在产品组合根注入对应 Port")
    clock, sleeper, ids = clock or SystemClock(), sleeper or SystemSleeper(), ids or UUIDGenerator()
    commands = commands or SubprocessCommandRunner()
    logs = Path(config.state.directory) / "logs"
    configure_logging(str(logs), console=console_logging)
    store = store or SQLiteRunRepository(config.state.database)
    git = git or GitClient(commands, config.git)
    workspace = workspace or GitWorktreeWorkspaceManager(git, config.workspace)
    views = EventViewBuilder(store, config.notification.templates_directory)
    host = (
        LoggingCodeHostAdapter(True)
        if config.dry_run
        else code_host
        or (
            AntCodeAdapter(
                config.code_host,
                config.repositories,
                commands,
                Path(config.state.directory) / "locks/external",
            )
            if config.code_host.adapter == "antcode"
            else LoggingCodeHostAdapter()
        )
    )
    notifier = (
        NullNotifier()
        if config.dry_run
        else notifier
        or (
            DingTalkNotifier(config.notification, views)
            if config.notification.adapter == "dingtalk"
            else NullNotifier()
        )
    )
    pipeline = (
        DisabledPipelineAdapter()
        if config.dry_run
        else pipeline
        or (
            ACIAdapter(config.pipeline, config.repositories, commands)
            if config.pipeline.adapter == "aci"
            else DisabledPipelineAdapter()
        )
    )
    executor = agent_executor or CodexAdapter(config.codex, commands, str(logs / "runs"))
    handlers = [
        notifier.notify,
        RunAuditHandler(logs),
        PipelineCancellationHandler(store, pipeline),
    ]
    if config.notification.issue_comments and not config.dry_run:
        handlers.append(IssueCommentHandler(host, store, clock, ids, views))
    events = EventDispatcher(store, clock, ids, handlers)
    renderer = StrictPromptRenderer(config.prompts.directory)
    registry = StepRegistry()
    for step in (
        RequirementsStep(renderer, git),
        CodingStep(renderer, git, commands),
        ReviewStep(renderer, git),
        FixStep(renderer, git, commands, config.workflow.max_fix_loops),
        PipelineStep(pipeline, store, ids, config.pipeline.poll_interval, git),
        CreatePRStep(renderer, git, host, store, ids),
    ):
        registry.register(step)
    services = StepServices(executor, FileArtifactStore(clock, ids), clock)
    engine = WorkflowRunner(
        store,
        registry,
        issue_development_workflow(),
        services,
        events,
        ids,
        sleeper,
        RetryPolicy(config.workflow.max_retries, config.workflow.retry_delay),
        config.scheduler.max_nodes_per_tick,
        git.head_sha,
        stop_requested=lambda: scheduler.stopping,
    )
    guard = RunExecutionGuard(Path(config.state.directory) / "locks/runs")
    runs = RunService(
        store, config, host, workspace, events, clock, ids, guard, pipeline=pipeline, git=git
    )
    dispatcher = Dispatcher(
        store, config, workspace, engine, events, clock, ids.new(), execution_guard=guard
    )
    scheduler = SchedulerRunner(
        Poller(config, host, runs), dispatcher, sleeper, config.scheduler.poll_interval
    )
    return Runtime(config, store, runs, scheduler, dispatcher, commands, renderer, git)

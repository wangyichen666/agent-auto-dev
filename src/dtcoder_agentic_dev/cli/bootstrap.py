"""产品组合根：所有具体依赖集中装配，CLI 命令只调用用例。"""

from dataclasses import dataclass
from pathlib import Path

from dtcoder_agentic_dev.adapters.code_host.logging import LoggingCodeHostAdapter
from dtcoder_agentic_dev.adapters.codex.adapter import CodexAdapter
from dtcoder_agentic_dev.adapters.notification.null import NullNotifier
from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.adapters.pipeline.disabled import DisabledPipelineAdapter
from dtcoder_agentic_dev.application.services.comments import IssueCommentHandler
from dtcoder_agentic_dev.application.services.events import EventDispatcher
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
):
    config = load_config(config_path)
    for injected, name, built_in in [
        (code_host, config.code_host.adapter, "logging"),
        (pipeline, config.pipeline.adapter, "disabled"),
        (notifier, config.notification.adapter, "null"),
    ]:
        if injected is None and name != built_in:
            raise CapabilityNotConfigured(f"适配器 {name} 尚未装配，请在产品组合根注入对应 Port")
    clock, sleeper, ids = clock or SystemClock(), sleeper or SystemSleeper(), ids or UUIDGenerator()
    commands = commands or SubprocessCommandRunner()
    logs = Path(config.state.directory) / "logs"
    configure_logging(str(logs))
    store = store or SQLiteRunRepository(config.state.database)
    git = git or GitClient(commands, config.git)
    workspace = workspace or GitWorktreeWorkspaceManager(git, config.workspace)
    host = code_host or LoggingCodeHostAdapter(config.dry_run)
    notifier = notifier or NullNotifier()
    pipeline = pipeline or DisabledPipelineAdapter()
    executor = agent_executor or CodexAdapter(config.codex, commands, str(logs / "runs"))
    handlers = [notifier.notify, RunAuditHandler(logs)]
    if config.notification.issue_comments:
        handlers.append(IssueCommentHandler(host, store, clock, ids))
    events = EventDispatcher(store, clock, ids, handlers)
    renderer = StrictPromptRenderer(config.prompts.directory)
    registry = StepRegistry()
    for step in (
        RequirementsStep(renderer, git),
        CodingStep(renderer, git, commands),
        ReviewStep(renderer, git),
        FixStep(renderer, git, commands, config.workflow.max_fix_loops),
        PipelineStep(pipeline, store, ids, config.pipeline.poll_interval),
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
    runs = RunService(store, config, host, workspace, events, clock, ids, guard)
    dispatcher = Dispatcher(
        store, config, workspace, engine, events, clock, ids.new(), execution_guard=guard
    )
    scheduler = SchedulerRunner(
        Poller(config, host, runs), dispatcher, sleeper, config.scheduler.poll_interval
    )
    return Runtime(config, store, runs, scheduler, dispatcher, commands, renderer, git)

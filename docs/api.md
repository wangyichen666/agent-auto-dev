# 应用服务边界

当前批次提供 Python application service 与 CLI，尚未提供 HTTP/FastAPI 服务，没有可调用的 HTTP 路由或 WebSocket。后续 API 应复用以下现有用例，不能另写一套任务状态规则。

| 用例 | 当前调用方 | 规则归属 |
| --- | --- | --- |
| DeclarativeRunService.submit | submit CLI | YAML 校验、skill 冻结、定义快照、任务入队、事件 |
| DeclarativeRunService.runner_for | Dispatcher | 核验历史定义，编译到统一 WorkflowRunner |
| DeclarativeRunService.prepare_workspace | Dispatcher | 无仓/单仓归属、租约与 CAS |
| DeclarativeRunService.control | RunService / CLI | pause/resume/retry/cancel/skip、反馈与合法状态 |
| RunService.details | show CLI | run、attempt、artifact、operation |
| RunRepository.list_runs | list CLI / Scheduler | 持久化查询 |

组合根 `build_runtime` 返回 declarative 服务，且允许 agent_executor、CommandRunner、RunRepository、clock 等 Port 注入。核心不导入 FastAPI、HTTP 或 UI。执行器失败不能映射为成功；新错误码包括 CONFIGURATION_INVALID、TIMEOUT、COMMAND_FAILED、BUSINESS_ERROR、CONCURRENCY_CONFLICT 和 PROCESS_INTERRUPTED。

实际 CLI、YAML 和持久化协议见 [workflows.md](workflows.md)。HTTP 鉴权、namespace、分页、大小限制、文件安全接口及面板资源会在后续独立闭环中补齐，当前不支持对外提供多用户 HTTP 服务。

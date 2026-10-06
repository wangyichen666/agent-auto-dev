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


## 第二批新增的可复用服务

AgentRouter 负责配置/模型路由；ManagedAgentExecutor 负责当前 RUNNING attempt 的模型执行和 session 事件写入。DeclarativeRunService.control 已支持 resume/retry 的 continue_conversation 与 revise；两种方式均保留反馈和历史。缺少会话/引擎匹配、有效租约或历史模型进程组仍存活时明确拒绝。

AgentManager 的 submit/status/cancel/stop 是进程内执行句柄接口，不能代替 API 的持久化任务查询。后续 HTTP 控制仍需先调用应用服务写持久化意图，再由运行时观察，不得仅取消内存 future。新增错误码 SESSION_LOST/CANCELLED；超时保持 TIMEOUT，未知引擎使用 CONFIGURATION_INVALID。

尚未新增 HTTP 路由、鉴权、文件 API 或面板；此文档记录应用服务边界，不能据此调用不存在的端点。

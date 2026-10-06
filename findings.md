# 证据与发现

- 初始 git status --short 为空。
- README/architecture 描述现有 WorkflowRunner、SQLite v1、CAS/租约/文件锁与单仓 worktree。
- src 尚无 YAML 工作流、Claude、API 或前端模块。
- pyproject 依赖 Click/PyYAML/tabulate；仅 dtcoder-agentic-dev 命令。
- 将继续通过实际实现及测试核实边界。

- 复用现有 WorkflowRunner 编译图；新增 PAUSED attempt outcome，旧枚举与公开命令不删除。
- 持久化扩展采用 SQLite v1 JSON payload 的可选 schema_version 字段与上下文记录，不更改旧表或索引；应补旧数据重开测试。
- 循环条件只比较循环内已声明 JSON 参数，最大轮数 1..1000；不执行表达式。
- 原始与 resolved YAML 保存独立文件并校验 SHA-256；skill 在提交时冻结为 prompt。
- 多仓、Claude SDK/CLI、API 和前端仍为后续批次，当前不宣称实现。

## 本批证据矩阵

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| YAML schema/顺序/安全条件 | 本批实现 | application/yaml_workflows.py、test_yaml_workflow.py |
| 统一 agent/tool 生命周期与持久化 | 本批实现 | engine/declarative.py、WorkflowRunner、test_declarative_runs.py |
| 定义冻结/skill 固化/SHA 恢复 | 本批实现 | filesystem/definitions.py、SQLite 重开/修改拒绝/超大模板测试 |
| pause/feedback/revise/retry/cancel/skip | 本批实现基础闭环 | application/services/declarative.py、竞态/失败点/人工测试 |
| 无仓受管目录/单仓 worktree 复用 | 本批实现 | filesystem/workspace.py、单仓 adapter fake 测试及 CLI 离线测试 |
| Codex model/timeout/无仓/session 提取 | 本批补充 | codex/adapter.py、命令数组与脱敏日志测试 |
| Claude/异步取消/session resume | 未实施 | 后续第二批；当前显式拒绝 continue_conversation |
| 多仓/reset/handoff/API/前端/备份 | 未实施 | docs/workflows.md 的明确边界 |

快照保存现在在租约校验事务内，防止过期 worker 覆盖恢复状态。恢复和控制仍依赖既有文件锁/heartbeat；未引入内存状态作为唯一事实源。文件与数据库不宣称跨资源原子事务：提交失败可能留下未引用定义目录，自动清理留待后续。

## 第二批阅读证据
- 当前工作区干净，第一批代码与文档已存在；测试重新运行 291 passed、1 skipped。
- AgentExecutorPort 只有同步 execute，无 session 输入；CodexAdapter 提取 session 但不支持 resume。
- 默认注册 default/codex 到同一 executor；没有 Claude 模块。
- DeclarativeRunService.control 显式拒绝 continue_conversation；pause/cancel 仅阻止后续节点。
- WorkflowRunner 在外部调用前保存 RUNNING attempt，并复用 SQLite CAS、跨进程执行锁和 heartbeat，可用来作为异步运行事实源。
- 新批次沿用原状态表/payload，避免新增孤立的内存任务状态。旧配置无 agents 节时保留 Codex 路由；新初始化示例采用 Claude。

## 第二批实现证据
- Claude CLI/SDK、GenericCLI、AgentRouter、AgentManager 与 ManagedAgentExecutor 已接入真实 Dispatcher，执行事实仍存 SQLite attempt。
- 新初始化 Claude；旧配置无 agents 继续 Codex并告警；resolved 定义固定引擎、已配置模型与双语模板。
- pause/cancel 协作停止受管 agent；session/进程组事件持久化，续聊失败保留历史和反馈；过期事件拒绝写入。
- CLI 自动恢复要求 session/引擎/冻结定义匹配且旧进程组退出。孤儿组存活则暂停，禁止人工操作启动另一 writer。
- SDK 公共接口没有可验证 PID，因此中断 SDK attempt 自动恢复保守暂停；正常 SDK 首次/流式/resume/取消/超时已实现和 mock 验证。
- 通用 CLI 的第三方同步 Port 必须协作取消；未合作时只能自然完成，不提前释放租约。

## 本地 Claude Code 真实验收证据
- 本地 CLI 2.1.63 与现有登录态可用；真实事件上报 deepseek-v4-flash，不能据此宣称 Anthropic 原生模型服务验证通过。
- 真实首次任务、Click submit、暂停后的同 session 续聊、取消、启动阶段超时均通过，现场留在独立临时目录。
- 本轮无需修改产品运行时代码；新增显式验收脚本、离线脚本检查和结果文档。SDK 未安装，真实 SDK 验收仍未执行。

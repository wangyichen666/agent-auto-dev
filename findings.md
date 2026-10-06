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

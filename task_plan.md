# 实施计划

目标：按用户优先级扩展研发代理，逐批形成可验证闭环，保留公开接口与旧数据。

## 第一批
1. 已完成：阅读仓库、确认干净工作区与测试基线（209 通过、1 跳过）。
2. 已完成：测试先行，实现严格 YAML schema、声明式模型、显式 agent/tool 注册表。
3. 已完成：复用 WorkflowRunner/CAS/租约/锁，实现循环、stage/attempt、冻结定义、产物、人工暂停/反馈/跳过/失败重试、无仓与单仓任务入口。
4. 已完成：修复快照 stale writer、取消重放、人工产物覆盖、后置动作校验、路径和凭据脱敏缺陷。
5. 已完成：291 passed、1 skipped；编译、Ruff、格式、Git diff、全部 CLI 帮助、隔离构建与 wheel 安装后离线闭环全部通过。

## 第一批结束时的后续路线（历史记录）
Claude CLI/SDK 与统一异步运行时 → session 续聊/结构化 handoff/reset → 多仓提交/回退 → 扩展事件 → HTTP API/安全文件接口 → React 面板 → 资源、清理、备份与运维。

## 错误与处理
- PATH 没有 pytest：使用仓库已有 .venv，不安装到用户全局环境。
- 新行为测试的初始失败：对应实现后聚焦验证通过，记录见 progress.md。
- 首个产物测试遗漏注册假 agent：补齐 fixture，编译继续拒绝未知 agent。
- 格式化后补丁上下文不一致：重新定位实际代码后应用，无部分修改。
- Ruff 删除兼容 re-export：显式保留 contained_path 同名导出，完整测试验证调用方。
- 系统 Git 为 2.39.5，真实 orphan worktree 单项跳过；未安装或替换系统 Git。

## 第二批（已完成当前纵向闭环）
1. 已完成：工作区干净；重新读取代码与文档，基线 291 passed、1 skipped。
2. 已完成：Claude CLI/SDK、通用 CLI、严格引擎路由与兼容配置；先写行为测试。
3. 已完成：异步 manager、任务专属进程取消、持久化 session、续聊及 CLI 恢复安全策略。SDK 缺少进程归属证明时不自动恢复。
4. 已完成：包资源、诊断、文档同步；330 passed、1 skipped，编译、Ruff、格式、帮助与构建通过；wheel 安装核验见 progress.md。

本批仍按用户允许的分批方式交付。多仓、handoff、API/面板、备份等后续能力不作完成声明。

## 本地 Claude Code 真实端到端验收（已完成）
- 用户已明确授权使用本地 Claude Code 及其登录态。
- 已确认本地 CLI 2.1.63、登录有效；项目虚拟环境未安装 SDK。
- 使用独立临时目录/配置/SQLite，无仓模型任务，不修改源仓库或全局 Claude 配置。
- 验证顺序：首次任务/真实文件校验 → 持久化暂停与 session 续聊 → 取消进程组 → 超时映射 → 回归与文档。
- scripts/e2e_claude.py 为显式真实模型验收入口，不进入离线 pytest。
- 首次任务及 submit CLI、暂停后同 session 续聊、取消、启动超时全部通过；实际模型 deepseek-v4-flash。
- 新增三个离线脚本检查，全量回归 333 passed、1 skipped；最终打包检查见 progress.md。

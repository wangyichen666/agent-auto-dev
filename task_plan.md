# 实施计划

目标：按用户优先级扩展研发代理，逐批形成可验证闭环，保留公开接口与旧数据。

## 第一批
1. 已完成：阅读仓库、确认干净工作区与测试基线（209 通过、1 跳过）。
2. 已完成：测试先行，实现严格 YAML schema、声明式模型、显式 agent/tool 注册表。
3. 已完成：复用 WorkflowRunner/CAS/租约/锁，实现循环、stage/attempt、冻结定义、产物、人工暂停/反馈/跳过/失败重试、无仓与单仓任务入口。
4. 已完成：修复快照 stale writer、取消重放、人工产物覆盖、后置动作校验、路径和凭据脱敏缺陷。
5. 已完成：291 passed、1 skipped；编译、Ruff、格式、Git diff、全部 CLI 帮助、隔离构建与 wheel 安装后离线闭环全部通过。

## 后续批次（未实施）
Claude CLI/SDK 与统一异步运行时 → session 续聊/结构化 handoff/reset → 多仓提交/回退 → 扩展事件 → HTTP API/安全文件接口 → React 面板 → 资源、清理、备份与运维。

## 错误与处理
- PATH 没有 pytest：使用仓库已有 .venv，不安装到用户全局环境。
- 新行为测试的初始失败：对应实现后聚焦验证通过，记录见 progress.md。
- 首个产物测试遗漏注册假 agent：补齐 fixture，编译继续拒绝未知 agent。
- 格式化后补丁上下文不一致：重新定位实际代码后应用，无部分修改。
- Ruff 删除兼容 re-export：显式保留 contained_path 同名导出，完整测试验证调用方。
- 系统 Git 为 2.39.5，真实 orphan worktree 单项跳过；未安装或替换系统 Git。

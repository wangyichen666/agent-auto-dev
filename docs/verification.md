# 开发验收记录

日期：2026-10-05。实施前工作区无未提交修改，基线为 111 项通过、1 项因系统 Git 版本跳过。保留原工作流、SQLite、租约、锁、独立 worktree、事件隔离与第二产品扩展测试，并增加真实外部契约、通知、运维及回退测试。

## 环境与实际执行

Python 3.14.4，项目声明 Python 3.10+；额外按 Python 3.10 语法解析全部源码和测试，但未在 Python 3.10 解释器上执行。使用项目 .venv 中的 pytest、Ruff 和构建工具。系统 Apple Git 不支持 orphan 测试所需选项，最终通过 DTCODER_TEST_GIT 指定捆绑 Git 2.53.0，所有测试都执行，没有跳过。

开发环境最初缺少 `python -m build` 对应构建模块，已安装 build 和 pyproject_hooks，且将 build 加入 dev 依赖。AntCode/ACI CLI 未安装，相关代码通过注入命令替身进行验证，不以工具缺失跳过实现。

| 验证命令 | 实际结果 |
| --- | --- |
| `pytest -q`（.venv，DTCODER_TEST_GIT 指定较新 Git） | **210 passed**，0 failed、0 skipped |
| `python -m compileall -q src tests` | 退出码 0 |
| `ruff check src tests` | 全部通过 |
| `ruff format --check src tests` | 104 个 Python 文件格式通过 |
| `python -m build` | 构建 sdist 与 wheel 成功 |
| `dtcoder-agentic-dev --help` | 退出码 0 |
| `dtcoder-agentic-dev init --help` | 退出码 0 |
| `dtcoder-agentic-dev doctor --help` | 退出码 0 |
| `dtcoder-agentic-dev rollback --help` | 退出码 0 |
| `git diff --check` | 退出码 0 |
| Python 3.10 AST 解析 | 全部通过，不能替代该解释器运行验证 |
| wheel / sdist 内容检查 | 新适配器、包内 YAML、源码发行运维文档和服务示例完整 |
| root / 包内 / wheel 示例 YAML 一致性检查 | 完全一致 |
| launchd plist XML 解析 | 通过；示例占位符未替换，未加载真实服务 |

## 新增验证范围

- AntCode 参数数组、profile、Issue 字段别名及对象身份、PR reviewer 去重/合并和源分支选项、CLI 启动/超时/非零/JSON/字段错误映射与脱敏。
- 远端评论和 PR 标记重复调用对账；缺少查询能力、返回未完成分页或 PR 状态不明时拒绝猜测；远端带敏感 URL 拒绝持久化，响应拒绝不能伪造成功。
- ACI YAML 来源互斥、项目/参数/路径严格校验、重复 param 参数、commit、env/source、banner 与嵌套 JSON、状态别名、未知状态、按键对账及取消。
- 流水线触发前推送、首次 WAITING、恢复只查 ID、失败阻断或放行，取消运行后的迟到触发及取消失败隔离；原完整 SQLite 重开恢复测试继续通过。
- 共享事件模板、未知变量失败审计、评论重复投递、工号补零、接收人策略、空接收人、最多一次重试、断网和认证头不进入负载/日志。
- 钉钉网关与官方 IM_ROBOT 直连卡片结构，空 HTTP 确认响应不得当作成功。
- init 结构化新增/更新仓库、稳定身份去重、用户模板保留、显式更新前备份、非法更新保持原 YAML。
- doctor 只读登录和仓库查询、未知模板变量分类；不执行创建/推送命令。
- daemon 原子 PID 文件权限、启动身份核验、PID 复用及损坏记录保护、SIGTERM、显式强制超时策略、日志行数/跟随、Windows 明确拒绝。
- rollback 计划、节点输入修订归属与祖先校验、非法状态、租约/原子步骤/执行锁、时间戳备份参数、PR 合并、keep 选项、部分远端失败与逐项审计。
- 原历史不覆盖、独立后继、暂停原运行真实终止、计划 revision 过期、工作区失败保留后继，以及中断 PENDING 意图的显式恢复。
- 工作区子目录符号链接逃逸拒绝；CLI 和直接调度器调用的 dry_run 均不执行 Git/Codex/平台/CI/通知，也不制造成功产物。

## 实际外部边界

测试 autouse 守卫禁止真实网络、Codex 和 Git commit/push/merge/rebase。Git 写入语义由 Mock 验证；隔离 worktree 使用本地临时仓库和 orphan，不制造提交，也不接触用户仓库。没有执行真实 commit、push、PR、评论、流水线或通知，没有启动真实后台调度服务。

AntCode/ACI 内部 CLI 的真实命令版本、完整查询、登录接口和服务器按键原子去重尚需在服务测试环境核验。适配器通过 help 能力检测拒绝缺失能力，不能把测试替身通过当作真实平台联调通过。AntCode 标记对账仍受远端一致性与分页完整性影响。

钉钉直连依据官方 SDK 的卡片投放结构，但未向真实服务发送；令牌续期、用户身份映射和应用权限由外部环境/网关提供。同步事件仍没有 outbox，存在提交后、投递前崩溃的漏发边界。POSIX 单机进程与锁不代表跨主机 fencing，Windows 后台与回退未支持。

迁移不改变 SQLite schema，旧配置通过新增字段默认值兼容；原路径执行 init 安装缺失评论模板，用户文件不覆盖。完整接入和运维要求见 [operations.md](operations.md)。

## 2026-10-06：声明式工作流第一批

初始 git status 干净；基线 209 passed、1 skipped。最终新增 82 项行为验证，总计 **291 passed、1 skipped**。唯一跳过项是系统 Apple Git 2.39.5 不支持测试所用 orphan worktree；提交、推送和模型契约仍由 fake/mock 验证。未执行真实模型请求、真实网络业务调用或用户仓库的 Git 写操作。

在项目 .venv（Python 3.14）中运行并通过：

- pytest -q
- python -m compileall -q src tests
- ruff check src tests
- ruff format --check src tests
- git diff --check
- python -m build（隔离构建 wheel 与 sdist）
- agent-auto-dev --help、doctor --help、process --help、rollback --help
- 新增 submit --help、workflow --help

新测试覆盖：严格 YAML/非法类型/重复键/大小限制、顺序与注册表、循环精确参数比较与轮数上限、六类产物校验、前后置动作失败、路径逃逸与工作区内受管路径符号链接、审批与人工产物、脱敏反馈、超时/取消竞态、优雅停止、失败节点重试、旧 payload 重开、模板冻结、单仓 Port 复用、无仓身份保护、失败日志文件与凭据脱敏、过期 snapshot writer。

包内分发原有配置与两份新 YAML 模板。安装后验收使用临时目录中的 wheel 副本执行 init → workflow validate → submit local-files，并核验真实产物、双命令入口、模板资源与无 .git 工作区。仓库没有前端，因此没有前端安装、类型检查、测试或构建项。

这是一批可用的 YAML/CLI 闭环，不是整个平台完成：Claude CLI/SDK 与异步运行时、session resume、handoff、多仓、reset、HTTP API、面板与自动备份/清理仍未实施。详见 [workflows.md](workflows.md)。

## 第二批验收（2026-10-06）

- 起始工作区干净；原基线 291 passed、1 skipped。
- 新增 39 个行为测试，最终 pytest -q：330 passed、1 skipped。
- 覆盖 Claude CLI/SDK 首次/流式/resume、错误终态、会话丢失、取消/超时、显式回退、命令数组、进程组隔离、持久化反馈、stale writer、SQLite 重开、CLI 恢复/孤儿保护、引擎/模型/双语模板冻结。
- compileall、ruff check、ruff format --check、git diff --check 均通过。
- agent-auto-dev 与 dtcoder-agentic-dev 的总帮助和 doctor/process/rollback 帮助均通过。
- python -m build 隔离构建 wheel/sdist 成功；发现并修复 package-data 资源声明错误后，重新构建并以 --no-deps --no-index 安装到临时目录，独立导入、新默认配置、双语模板、init/validate/submit 离线文件任务闭环通过。
- 唯一跳过仍为系统 Git 2.39.5 不支持 orphan worktree 测试；未修改用户源仓库、调用真实模型或访问业务网络。
- 当前仓库没有前端工程/锁文件，因此本批没有前端安装、测试或生产构建。
- SDK 契约使用注入替身验证，未做真实服务调用。中断 SDK 的自动恢复因缺少公开进程归属证明保持人工暂停；结构化 handoff、多仓、HTTP/面板、备份和资源限制仍未实现。

## 本地 Claude Code 真实验收（2026-10-06）

用户授权后，已使用本地 Claude Code 2.1.63 与现有登录态验证无仓首次任务、submit CLI、暂停后的同 session 续聊、取消及启动阶段超时。运行事件实际上报模型 deepseek-v4-flash。每个用例使用独立临时目录、SQLite 和真实文件产物；测试现场保留。

scripts/e2e_claude.py 为显式真实模型入口，普通 pytest 保持离线；新增两个脚本分支测试防止成功用例错误使用超时断言，以及非空目录保护测试。全量回归为 **333 passed、1 skipped**。详情与复现方式见 [claude-e2e.md](claude-e2e.md)。SDK 未安装，未执行真实 SDK 测试。

compileall、Ruff 检查及格式、git diff --check、验收脚本帮助均通过；wheel/sdist 隔离构建成功，确认源码包分发验收脚本与文档。

# 验证进度

- 已读取用户完整任务、README、architecture、示例配置与 pyproject。
- 尚未修改业务代码；正在确认测试基线与可复用接口。

- 基线：.venv/bin/pytest -q → 209 passed, 1 skipped；PATH 无 pytest，采用项目虚拟环境。
- 行为测试首次按预期因缺少 YAML 模块失败；实现后首轮 23 项聚焦测试全部通过。
- 包含新测试的全量：232 passed, 1 skipped；尚待 CLI 装配、资源与扩大安全覆盖。

- CLI submit/workflow validate、人工审批/反馈/重试/清理闭环通过。
- 新测试发现并修复：人工节点覆盖手工产物、取消后重复原子动作、循环重试突破轮数上限、post_actions 使已校验产物失效、非法类型 TypeError、重复参数与 JSON 凭据日志遗漏。
- 目前聚焦 YAML/CLI/恢复：66 项通过；Codex 参数/日志与脱敏：24 项通过。
- 新增测试均使用临时目录、SQLite 与 fake/mocked executor，无真实模型请求或用户仓库操作。

## 第一批最终验收

- pytest -q：291 passed、1 skipped；相对基线新增 82 项通过测试。
- compileall、ruff check、ruff format --check、git diff --check：全部通过。
- python -m build：隔离构建 wheel 与 sdist 成功。
- agent-auto-dev 的总帮助、doctor/process/rollback/submit/workflow 帮助及旧命令总帮助：全部通过。
- 最终 wheel 以 --no-deps --no-index 安装到临时目录，验证确实从 wheel 导入；init/validate/submit 离线执行及真实文件产物通过，模板/配置资源与双入口通过。
- 唯一跳过：系统 Git 2.39.5 的 orphan worktree 测试；未访问真实模型/业务网络或修改用户仓库。
- 第一批实现与验收完成。下一批尚未实施，顺序与边界见 task_plan.md、docs/workflows.md。

## 第二批进度
- 新执行器行为测试先行，初次因模块缺失失败；随后发现 pytest 保留 fixture 名 request，已重命名。
- Claude CLI/SDK、严格配置、通用 CLI 与 manager 首轮 14 项契约测试通过。
- 正在接入原调度、持久化 session 事件、取消和恢复；继续运行兼容测试。
- 续聊、持久化取消、SQLite 重开/恢复、过期 writer、模板冻结和进程组隔离聚焦测试通过。
- 测试揭示并修复：tuple/list 快照比较不一致、日志路径先后顺序、非法 session 参数、SDK aborted 伪成功、续聊失败 session 丢失、孤儿进程恢复冲突。
- 当前多执行器聚焦 36 项通过，正在执行最终全量、编译、Ruff、打包与 wheel 安装验证。
- 最终全量：330 passed、1 skipped；编译、Ruff、格式、双 CLI 帮助与隔离构建通过。
- wheel 安装核验发现本次新增 SDK 依赖的字符串替换误改 package-data 包名，新增双语模板未入包；已修复资源键，重新构建并重复安装核验，不以构建成功代替发布可用性。

## 第二批最终验收
- 330 passed、1 skipped；新增 39 个行为测试。编译、Ruff、格式、diff 检查和双入口要求的帮助均通过。
- 修复资源包名后，重新隔离构建成功；最终 wheel 无依赖/无索引安装到临时目录，验证来自 wheel 的导入、Claude 默认配置、双语模板及离线文件任务闭环通过。
- 真实 Claude/Codex 模型调用未执行；SDK 流式接口使用 fake 契约验证。SDK 崩溃自动恢复明确保留人工边界。
- 无前端工程；唯一 Git orphan skip 的原因保持不变。

## 本地 Claude Code 真实验收
- CLI 2.1.63，auth status 已登录；stream-json 实际上报模型 deepseek-v4-flash；沿用用户本地配置。
- 首次任务：9.462 秒，真实 report.md 校验成功，SUCCEEDED，session 持久化。
- 暂停续聊：38.652 秒，PAUSED 后复用 session，新 attempt 成功，反馈写入产物，旧记录保留。
- 取消：6.120 秒，任务/attempt CANCELLED，进程组退出、租约释放。
- 启动阶段超时：0.235 秒，FAILED/TIMEOUT，进程回收。
- 后补 submit CLI 时脚本误留 TIMEOUT 断言；CLI 实际成功，但脚本记录 FAILED。已修复并补两个离线分支测试，重新跑真实 basic CLI。
- SDK 未安装；本次不宣称真实 SDK 或真实 Bash 后代进程中断验证。
- 修复后的 submit CLI 真实验收通过：退出码 0，SUCCEEDED，耗时 9.277 秒；原失败报告保留。
- 两个脚本分支测试及非空目录保护测试通过，全量回归 333 passed、1 skipped。
- compileall、Ruff 检查及格式、diff 检查、验收脚本帮助通过；隔离构建 wheel/sdist 成功，确认源码包包含真实验收脚本和结果文档。源码包沿用原配置，不分发 tests 目录。

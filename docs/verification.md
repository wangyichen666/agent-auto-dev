# 开发验收记录

日期：2026-10-05。目标目录从空项目创建；未复用其他项目实现。

## 环境

- Python 3.14.4，项目声明 Python 3.10+，全部源码与测试通过 Python 3.10 语法解析检查。
- 测试指定 Git 2.53.0；系统默认 Apple Git 2.39.5 缺少 `--orphan`。
- Click、PyYAML、tabulate、pytest、pytest-mock、setuptools 与 Ruff 已安装到项目 `.venv`。

## 实际执行及结果

| 验证 | 结果 |
| --- | --- |
| `pip install -e '.[dev]' --no-build-isolation --no-index` | 可编辑安装成功，指定依赖齐全 |
| `pip wheel . --no-deps --no-build-isolation --no-index` | wheel 构建成功 |
| wheel ZIP 资源/入口检查 | 包含五个模板、配置示例、CLI 入口和中文 README |
| `dtcoder-agentic-dev --help` | 退出码 0，无须初始化 |
| `dtcoder-agentic-dev init --help` | 退出码 0 |
| 初始化前 `doctor` | 退出码 1，中文提示先执行 init，无堆栈 |
| 临时配置目录连续两次 `init` | 两次退出码 0；数据库、模板和日志齐全 |
| 初始化后 `doctor`，缺少外部工具/认证 | 退出码 1，逐项中文诊断，明确未配置真实 CodeHost，无堆栈 |
| 临时目录 `list` / `config show` | 退出码 0 |
| `DTCODER_TEST_GIT=<Git 2.53 路径> pytest -q` | **112 passed**，无跳过、无失败 |
| `ruff check src tests` | 全部通过 |
| `ruff format --check src tests` | 90 个 Python 文件格式符合要求 |
| `python -m compileall -q src tests` | 通过 |
| 对项目文件执行 Python 3.10 AST 解析 | 通过；未在 Python 3.10 解释器上实际运行 |
| 空目录基线 `git diff --no-index --check` | 无空白错误；全新增差异返回码 1 属正常情况 |
| 项目文本的常见凭据模式扫描 | 未发现常见 Token/认证头模式；示例仅使用占位地址与虚构数据 |

上述 `<Git 2.53 路径>` 是本次执行环境的捆绑 Git，可通过 DTCODER_TEST_GIT 选定；不把机器特定路径写入项目代码。较旧 Git 默认会跳过单个真实 orphan worktree 测试，其他测试仍可执行。

## 覆盖的关键行为

- 配置默认值、路径展开、严格字段/类型、重复仓库、非有限时间和能力配置。
- 领域模型序列化、图校验、注册表和第二个产品接入。
- SQLite schema、嵌套事务回滚、乐观冲突、唯一外部操作键、两个连接竞争领取、续租与过期恢复。
- 默认六节点真实步骤配合测试替身完整到达 SUCCEEDED；产品组合根实际注入后也完成完整调度。
- `passed=true` 且有 Blocker 仍走 fix，修复次数超限失败，业务阻断不计技术重试。
- 技术错误指数退避、重试耗尽、Fatal、无代码变化、严格 JSON、缺失/空模板和模板摘要。
- WAITING 状态重开 SQLite 和重新装配引擎后恢复；已有流水线 ID 不重复触发。
- PR 远端成功但本地保存失败时查询对账，不重复创建；流水线 PENDING 意图重放由远端同键去重。
- 暂停/取消期间原子步骤完成仍保留控制状态；停止信号在节点边界让出运行，不开始下一步骤。
- 真实的两个 orphan worktree 相互隔离、恢复复用和局部清理；提交/推送仅验证 Mock 命令参数。
- 运行文件执行锁阻止过期 worker 的长命令与接替 worker 重叠执行。
- 通知、评论事件处理失败只记录 warning 与详情，不覆盖主流程结果。
- 全部 CLI 基本行为与幂等初始化；演练入队不执行 Git/Codex。

## 操作边界

测试通过 autouse 守卫禁止真实网络、真实 Codex 与 Git commit/push/merge/rebase。整个开发过程中未执行这些 Git 操作，未写入真实认证信息或业务服务地址。公开开发依赖在获准的环境安装步骤中下载，业务系统未调用任何真实代码托管、Codex 任务、流水线或通知服务。

本阶段未接入真实平台/CI/通知；默认适配器会明确诊断缺少能力。POSIX 文件锁尚不支持 Windows。同步事件没有 outbox 自动重发，跨系统幂等必须由远端适配器落实。更详细边界与下一步见 README 和 architecture.md。

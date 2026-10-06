# 声明式任务工作流（第一、二批）

本批实现严格 YAML 定义、agent/tool 分发、阶段与尝试记录、有限循环、产物验证和 CLI 提交闭环。新任务使用 `submit`，编译后复用既有 `WorkflowRunner`、SQLite CAS、数据库租约、heartbeat 和运行文件锁。原有 `process --repo --issue` 产品与公开命令继续可用。

## 立即运行

```bash
agent-auto-dev --config ./runtime/config.yaml init
agent-auto-dev --config ./runtime/config.yaml workflow list
agent-auto-dev --config ./runtime/config.yaml workflow validate ./runtime/workflows/local-files.yaml
agent-auto-dev --config ./runtime/config.yaml submit \
  --workflow ./runtime/workflows/local-files.yaml --task '验证本地工具生命周期'
agent-auto-dev --config ./runtime/config.yaml list --all
```

`local-files.yaml` 是不调用模型的文件处理示例；它只演示 YAML 生命周期，不声称理解自然语言或完成调研。默认 `document.yaml` 则真实调用 agent，根据 `--task` 生成 `report.md` 并校验非空。新初始化的 `agent: default` 路由到 Claude CLI；未声明 agents 的旧配置保持 Codex，并给出迁移提示。Claude CLI/SDK 和 Codex 共用异步 manager 与既有持久化生命周期。

```bash
agent-auto-dev --config ./runtime/config.yaml submit --task '整理项目的运维流程'
# 在已配置仓库的隔离 worktree 中执行：
agent-auto-dev --config ./runtime/config.yaml submit --repo sample --task '编写项目接口说明'
```

每次 submit 创建独立 run；`--queue-only` 只入队，`run`/`run-once` 调度。dry_run 只入队，不执行工具、模型或 Git。一次调度受 `scheduler.max_nodes_per_tick` 限制，达到限制仍是可恢复的活动运行。

## YAML 契约

```yaml
name: review-doc
version: '1'
description: 有限的文档检查循环
context:
  language: zh
metadata:
  owner: local
stages: [write, review]
jobs:
  write:
    stage: write
    agent: default
    prompt: 根据任务写 report.md。
    outputs:
      report: {path: report.md, kind: markdown}
    artifact_check:
      - {type: nonempty, path: report.md}
    config:
      execute: {humanAgentType: auto, timeout: 600, retries: 1, allow_skip: false}
  review:
    stage: review
    agent: codex
    prompt: 检查 report.md，把是否通过写入 result.json 的 passed 布尔字段。
    inputs:
      report: {job: write, output: report}
    outputs:
      result: {path: result.json, kind: json, parameters: [passed]}
    artifact_check:
      - {type: json, path: result.json, fields: [passed]}
loops:
  - name: review
    jobs: [review]
    max_rounds: 3
    until: {job: review, parameter: passed, equals: true}
    on_exhausted: pause
```

- 顶层只接受 name/version/description/context/stages/jobs/loops/metadata。name/version 是非空字符串，version 应带引号。
- stages 为有序且不重复的标识符列表；jobs 为有序映射。先按 stage 顺序分组，再按 job 的 YAML 顺序执行。内部 `__complete` 是无副作用的终止节点。
- agent job 的 skill 与 prompt 必须且只能有一项，agent 名称必须已注册。model 按参数传给执行器，未知 agent 不回退。skill 是配置目录 `declarative.skills_directory` 内的相对文件，提交时冻结为 prompt；后续修改不会改变历史 run。
- tool job 必须有 type: tool 和 toolName，不允许 agent/prompt/skill/model。第三方执行器从组合根注册，不加载 YAML 中的 Python 类或任意表达式。
- inputs 引用此前已声明的 producer/output；运行时 producer 必须成功。outputs 可以是相对路径字符串或 path/kind/parameters 映射。parameters 从 JSON 对象的顶层字段读取，同一 job 不允许重名。
- loops 的 jobs 必须连续、有序且互不重叠；当前不支持嵌套。max_rounds 为 1..1000 整数。until 只能比较循环内已声明参数与一个 JSON 标量，按类型精确比较（true 不等于 1）。不执行 Python/shell 条件。
- YAML 上限 1 MiB，最多 256 个 stage、2048 个 job；拒绝重复键、anchor/alias、日期对象、非有限数、未知字段和非法类型。

## 工具与产物

内置通用工具：

| toolName | config | 行为 |
| --- | --- | --- |
| file.write | path、content | UTF-8 文本原子写入 |
| file.copy | source、target | 工作区内 UTF-8 文本复制 |
| command | command: 参数数组 | 白名单命令，shell=False，当前工作区 cwd |
| test | command: 参数数组 | 与 command 相同的受控验证入口 |

命令默认关闭。运维人员可以在主配置的 `tools.allowed_commands` 中允许受信任的参数前缀，例如 `[[python, -m, pytest]]`。允许 `python -c` 等广泛前缀相当于授予相应代码执行能力；当前工具白名单与 Prompt 范围规则不是操作系统沙箱。

post_actions 为 `[{toolName: ..., config: ...}]`，在主动作产物首次校验后执行，共用 job 的超时预算。完成后再次校验并捕获最终文件摘要。post_action 不另设 execute 策略，失败保留失败事实，不返回虚假成功。

artifact_check 支持：

| type | 附加字段 | 校验 |
| --- | --- | --- |
| exists | 无 | 普通文件存在，可为空 |
| nonempty | 无 | UTF-8 内容去空白后非空 |
| sections | sections: 列表 | Markdown 标题匹配 |
| json | fields: 列表、values: 映射 | 顶层字段存在、值和类型精确匹配 |
| regex | pattern | 正则匹配 |
| script | command: 参数数组 | 文件存在，并经 command 白名单执行验证 |

所有规则要求 path；所有文件路径均为工作区内相对路径，拒绝绝对路径、`..`、反斜线、`.git`/`.workflow` 受管目录和符号链接逃逸。正则与已放行脚本来自受信任工作流；当前不提供不可信脚本或正则的独立沙箱。无仓提交在编译阶段拒绝声明 requires_repository 的工具及识别出的 Git 写命令；本地 command/test 在单仓模式也拒绝识别出的 Git 写操作，提交/回退只能经专用受管 Git Port 执行。

## 人工操作与重试

`humanAgentType`：auto 自动执行；notify 自动执行并向现有事件消费者发节点开始事件；approval 在动作前暂停，resume 后执行；human 暂停等待人工填写产物，resume 只验证产物并执行声明的后置动作，不覆盖人工文件。

```bash
agent-auto-dev --config ./runtime/config.yaml pause --run RUN_ID
agent-auto-dev --config ./runtime/config.yaml resume --run RUN_ID --feedback '已批准，补充检查结论' --mode revise
agent-auto-dev --config ./runtime/config.yaml retry --run RUN_ID
agent-auto-dev --config ./runtime/config.yaml cancel --run RUN_ID
agent-auto-dev --config ./runtime/config.yaml skip --run RUN_ID
```

暂停点与反馈带时间、revision、节点、轮次和可用 session_id，反馈持久化前脱敏，历史不删除。当前 revise 从暂停边界继续；循环耗尽的 revise 重启该有限循环并保留上一轮记录。continue_conversation 支持受管 Claude CLI/SDK 与 Codex CLI 的 session resume；当前节点必须有匹配的持久化 session 和引擎，否则显式拒绝。retry --mode continue_conversation 保留失败节点会话，默认 revise 开始新执行，不清除反馈。人工节点验证失败可以 retry，已成功 attempt 与 artifact 不被改写。

YAML retry 只允许 FAILED/CANCELLED，使用原 run 的失败节点并创建新的 attempt；已完成节点不重放。循环耗尽后的 retry 新建循环执行代次，重新遵守原轮数上限。旧 Issue 产品 retry 仍保留创建后继 run 的历史语义。skip 只接受 PAUSED 且声明 allow_skip 的节点，记录真实 SKIPPED attempt；跳过的 producer 没有成功产物，下游不能将其作为成功输入。

pause/cancel 写持久化意图，在每个原子动作前再次检查。受管模型动作协作取消并回收自身进程组；其他同步动作自然完成并保存真实结果，然后停止新节点。取消错误单独映射为 PAUSED/CANCELLED，不能伪造成功。有效租约下禁止 resume/retry/skip。PAUSED/CANCELLED 不自动执行；RUNNING 的中断 attempt 保留为 PROCESS_INTERRUPTED。受管 CLI 仅在冻结定义、引擎与 session 匹配且历史进程组已经退出时创建续聊 attempt，否则暂停。SDK 归属证明缺失时暂停，要求 revise。工具继续沿用原恢复语义。

## 持久化与兼容

```text
runtime/
├── workflow-definitions/<run-id>/{workflow.yaml,resolved_workflow.yaml}
├── workspaces/<run-id>/.workflow/owner.json          # 无仓，无 .git
├── workspaces/<repository-id>/<run-id>/             # 单仓 worktree
└── logs/runs/<run-id>/<job>/attempt-<n>/{stdout.txt,stderr.txt,execution.json}
```

运行只保存定义的相对路径、schema_version 和 SHA-256，恢复时核验身份与摘要，不读取后来修改的用户模板。定义原样保存，不应含认证凭据；认证继续交给执行器自身登录态。新初始化保留用户 workflow/skill 副本，显式更新模板先备份。

stage execution 按 run/stage/loop/执行代次/轮次确定 UUID；job attempt 按 run/job/attempt_number 确定 UUID。stage 状态、时间、暂停/失败点保存于 run.context，由同一 CAS 事务更新。attempt 增加可选 error_code，保存输入、实际引擎、模型、session、日志引用、产物、HEAD、耗时及错误。大型 stdout/stderr 在文件中，SQLite 保存有界摘要。日志落盘失败单独审计，不掩盖节点事实。

SQLite 沿用 schema v1 的 JSON payload 可选扩展，不删除旧列、表或索引。旧 payload 缺少新字段时使用默认值，有旧格式重开测试。run 的 QUEUED/SUCCEEDED 保持兼容，语义分别对应 PENDING/COMPLETED；stage 使用 RUNNING/PAUSED/COMPLETED/FAILED/CANCELLED。

无仓工作区必须有 run 归属标记；既不收养未知目录，也不在无仓任务中创建伪 Git 仓库。cleanup 仍要求终态、无有效租约和同一执行锁，保留定义与历史。单仓仍使用现有 worktree manager；本批不新增 Git 提交工具，旧研发产品仍保留原有受管提交行为。

## 后续边界

结构化 handoff、reset、多仓两阶段提交/回退、HTTP API、前端、资源监控与备份/定时清理尚未实现。当前 YAML 核心执行器注册表、工具 Port、文件定义 Port 和日志 Port 可供后续批次复用，不宣称这些后续能力已可用。


## 执行器与恢复边界（第二批）

注册名称：default、claude/claude-cli、claude-sdk、codex/codex-cli。模型精确路由配置在 agents.model_routes；没有匹配路由时采用显式默认引擎，未知引擎不回退。新 submit 的 resolved 定义保存选定引擎及已配置的默认模型；未显式配置模型时由执行器自身默认值决定，记录的 model 可以为空，不虚构具体模型名称。

Claude CLI 使用参数数组 `claude --print --output-format json|stream-json --max-turns N --permission-mode MODE`，提示词从 stdin 输入；model、allowedTools、resume 按结构化配置追加。stream-json 添加 verbose，session 可以在执行结束前落盘。工具自动许可不等于移除其他工具或 OS 沙箱。权限、Git 和文件范围仍需宿主环境提供适当隔离。

AgentExecutionRequest 新增可选 session_id、cancel_requested 与 on_event，旧构造方式兼容；结果新增四种终态，旧同步 Port 仍可注入。GenericCLIExecutor 接受可信命令数组，用于自定义协议/替身；不猜测其 resume 参数。Claude CLI/SDK 必须返回合法终态，is_error、异常、轮次耗尽、缺失结果或 aborted 不能当作成功。SESSION_LOST 属于需人工处理的错误，连接故障仍按技术策略重试。

manager 句柄只表示本进程的执行情况。真实查询和重启恢复使用 SQLite；每次调用前保存 RUNNING attempt，流式事件在同一租约下更新 session/进程归属，stale writer 拒绝写入。heartbeat、调度、CLI 控制与跨进程文件锁保持原边界。恢复遇到历史进程组仍存活时持久化 orphan_process_active 暂停；保守检查只探测已记录的进程组，不杀进程，不匹配其他会话。进程组 ID 被复用时也可能保守阻断，不能据此执行模糊清理。

SDK 通过公开 query/ClaudeAgentOptions 流读取消息，resume 指定 session，取消/超时关闭当前迭代器与 transport；只有导入失败允许显式 CLI 回退。SDK 没有向平台暴露可验证 PID，因此崩溃后的 SDK 自动接管暂不支持。首次调用、人工暂停后的续聊和失败重试已覆盖替身契约，未向真实模型发送请求。

Prompt 构建器使用 agent_zh.txt/agent_en.txt，context.language 为 zh/en。模板只允许 instructions/context 两个简单变量，提交时把文本、schema 和摘要冻结在 resolved_workflow.yaml。init 保留用户副本，显式 update_templates 先备份。注入 task、repository、workspace、声明输入/输出、参数上下文和本节点反馈；knowledge 目前作为显式上下文引用保留，结构化 handoff 和知识读取 Port 仍待下一批。

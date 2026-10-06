# 真实接入与长期运维

## 从已有安装迁移

现有 SQLite schema 版本仍为 1，新增字段使用 dataclass 默认值，旧运行、attempt、artifact 和操作键保持可读。不重新建立数据库。先停止原调度器，备份配置和 SQLite（使用 SQLite backup 或停止后完整备份），升级包，再执行原配置路径的 `init`：新增评论模板仅在不存在时安装，已有配置和 Prompt 不覆盖。

```bash
dtcoder-agentic-dev --config ./runtime/config.yaml init
dtcoder-agentic-dev --config ./runtime/config.yaml doctor
```

更新内置模板使用 `init --update-templates`，每个覆盖文件旁保存 `.backup-<时间戳>`。该选项同时更新 Prompt 和评论模板。未要求更新时，重复 init 不改模板或配置文本。

仓库新增或更新示例：

```bash
dtcoder-agentic-dev --config ./runtime/config.yaml init \
  --name sample --provider antcode --project sample/project \
  --path ./source --remote https://example.invalid/sample/project.git \
  --base-branch main --label agent-ready --reviewer reviewer-login \
  --code-host-adapter antcode --pipeline-adapter aci \
  --pipeline-enabled --pipeline-project 200100125 --yml-path .aci.yml \
  --param env=pre
```

占位 URL 不可真实使用。按 `(provider, project 或 remote)` 的稳定身份更新显示名称，重复执行不会新增相同仓库。仅传已有 `--name` 可更新该条目的非身份字段。部分 CI 配置更新合并已有字段；切换 YAML 来源时应编辑结构化 YAML，将旧来源设为 null，保持恰好一个来源。`--label`、`--author`、`--reviewer` 可重复。

`--prepare-mirror` 可预先克隆受管镜像；本地 path 只是源，origin 随后设为配置 remote。Git 写操作、网络同步仅在明确使用此选项且不是 dry_run 时发生。源目录与工作区/镜像目录重叠会被拒绝。

## AntCode CLI 契约与幂等边界

选择 `code_host.adapter: antcode`，按需配置 binary、profile、timeout。每条命令使用现有 CommandRunner 参数数组，认证来自 CLI 已有登录状态，不读取 token_env 值、不保存完整平台响应。profile 仅作为全局 `--profile` 参数；配置展示隐藏其内容。

适配器使用以下命令协议；这是一份明确的兼容性要求，当前环境没有安装该内部 CLI，**尚未以真实 CLI 核验具体版本的命令拼写**。不同版本应集中调整适配器，不能绕过对账而伪造成功：

```text
antcode [--profile PROFILE] issue list --project PROJECT --state open --all --json
antcode [--profile PROFILE] issue view NUMBER --project PROJECT --json
antcode [--profile PROFILE] issue comments list ISSUE_ID --project PROJECT --all --json
antcode [--profile PROFILE] issue comment ISSUE_ID --project PROJECT --body BODY --json
antcode [--profile PROFILE] pr list --project PROJECT --state all --all --json
antcode [--profile PROFILE] pr create --project PROJECT --source-branch BRANCH --target-branch BASE --title TITLE --body BODY [--reviewer ID] [--remove-source-branch] --json
antcode [--profile PROFILE] pr view PR_ID --project PROJECT --json
antcode [--profile PROFILE] pr close PR_ID --project PROJECT --json
```

列表必须完整返回数组或 `{items: [...]}`，PR 列表必须包含 description/body，包含关闭/合并的 PR。评论列表包含 id 和正文。接口返回更多分页、total 超过 items 或下一页游标时拒绝判断资源不存在。必须支持 `--all`；关键查询和 PR 创建选项先通过 `--help` 检测，不支持时抛 CapabilityNotConfigured 并要求升级/调整 CLI。

评论和 PR 正文附加 `<!-- dtcoder:<幂等键 SHA-256> -->`。首次调用和恢复 PENDING 意图时扫描远端标记，已存在则返回远端身份。生产组合根对相同键加同机文件锁，避免同数据库多个投递者同时查询后创建。PR 配置 reviewer 与 Issue reviewer、assignee、author 按稳定顺序合并去重；对象只提取平台 id/user_id/login/username/employee_id/work_no，忽略 name。字符串被视为 CLI 支持的平台身份字符串，实际身份有效性由平台校验。

**剩余边界**：标记查询依赖远端读取足够及时、正文保留标记和列表完整；服务器没有按键原子创建时，不保证跨主机或最终一致列表下的恰好一次。不能删除标记，不能给多个独立部署使用不同状态目录却共享同一运行键。平台响应不应返回带凭据或签名查询参数的 URL，否则拒绝持久化。

## ACI CLI 契约

选择 `pipeline.adapter: aci`。每个启用仓库必须提供 pipeline.project，并在 yml_path/template_id/yaml_file 中恰好选择一种。yml_path 为仓库内相对路径；yaml_file、yml_global_path、env_file 为相对于配置文件目录的本地文件，支持 `~` 与环境变量展开。params 是字符串映射，重复生成 `--param key=value`，值不写入配置展示或仓库快照。

```text
aci pipeline list --project PROJECT --idempotency-key KEY --all --json
aci pipeline trigger --project PROJECT --branch BRANCH --source SOURCE --idempotency-key KEY (--yml-path PATH | --template-id ID | --yaml-file FILE) [--yml-global-path FILE] [--commit SHA] [--param KEY=VALUE] [--env-file FILE] --json
aci pipeline view PIPELINE_ID --project PROJECT --json
aci pipeline cancel PIPELINE_ID --project PROJECT --json
```

内部 CLI 的具体版本同样未经真实核验。触发前要求 help 提供 `--idempotency-key`，查询也必须提供按键完整列表。查询结果必须返回相同 idempotency_key/idempotencyKey，多个结果或不可核验的键会拒绝再次触发。服务器必须真正按键去重；**帮助中存在参数并不能证明服务器提供原子幂等**，部署前需要在服务测试环境核验该契约。不支持时升级 CLI 或提供满足协议的受控包装器，不能降级到仅 SQLite 去重。

允许 JSON 前置 banner 与嵌套 result，接受 pipeline_id/pipelineId/id。触发响应缺少状态只记 PENDING；查询缺少/未知状态报 TechnicalError。pending/queued/created/waiting 归为 PENDING，running/in_progress 归为 RUNNING，success/succeeded/passed 归为 SUCCEEDED，failed/failure/error 归为 FAILED，cancelled/canceled 归为 CANCELLED。

首次触发先推送运行分支，并传入工作区 HEAD；请求意图已经保存。首次结果始终 WAITING，下次到 next_poll_at 后只查询已保存 ID。失败默认阻断，可配置 pipeline_failure_blocks: false。运行取消后查询仍在运行的 CI 并尽力取消；迟到的 PipelineStarted 事件再次检查取消状态。取消失败审计到事件处理失败，不撤销主运行的取消结果。

## 评论与钉钉

开启 `notification.issue_comments` 后使用 comments 目录中的 13 个模板，覆盖运行开始、需求、编码、评审通过/阻断、流水线开始/通过/失败、PR、完成、失败、取消和回退。仅接受简单变量：run_id、branch、step、round、artifacts、pipeline_url、pr_url、error_type、result。未知变量、字段表达式或空模板均明确失败，事件分发器审计失败并保留主结果。实际没发生的阶段不会产生成功评论。

钉钉使用相同 EventViewBuilder 和同一模板文本。`notification.adapter: dingtalk`，配置 api_url、card_template_id、timeout 和 recipients；assignees 为默认策略，configured 使用 receiver_ids，both 合并。数字工号不足六位补零，空接收人跳过并记录不含身份的日志。平台 ID/登录名与钉钉工号的对应关系必须由使用方保证；不同身份体系请显式选择 configured，避免用展示名称猜测工号。

支持两种 API：

- `gateway`（默认）：向企业 HTTPS 卡片网关发送 cardTemplateId、outTrackId、receiverUserIdList 和 cardData.cardParamMap；网关负责平台身份映射、认证续期与按 outTrackId 去重，响应需明确确认成功（success: true 或 errcode: 0）。可用 access_token_env 加钉钉认证头，也可注入自己的 HttpTransport。
- `direct`：api_url 使用 `https://api.dingtalk.com/v1.0/card/instances/createAndDeliver`，access_token_env 指定运行环境中的钉钉 access token 变量名。逐接收人投放 IM_ROBOT 卡片，每人的 outTrackId 是事件 ID 和工号的稳定摘要。认证头为 x-acs-dingtalk-access-token，卡片结构参考[钉钉官方 SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python/blob/main/dingtalk_stream/card_replier.py)。应用必须有已发布模板、单聊机器人投放权限和有效用户 ID。此模式不获取/自动刷新 token；需要外部凭据管理或改用网关管理续期。

每张卡片网络失败最多重试一次，超时有界。URL、响应、令牌与接收人不进入日志/异常。失败交给事件分发器审计，不阻断工作流。仍采用同步提交后事件分发；没有持久化 outbox，提交后、投递前崩溃可能漏发事件，不能声称通知必达。

## 后台命令

```bash
dtcoder-agentic-dev --config ./runtime/config.yaml start
dtcoder-agentic-dev --config ./runtime/config.yaml status
dtcoder-agentic-dev --config ./runtime/config.yaml logs --lines 100 --follow
dtcoder-agentic-dev --config ./runtime/config.yaml stop --timeout 60
dtcoder-agentic-dev --config ./runtime/config.yaml restart --timeout 60
```

后台使用当前 Python 的模块入口，不依赖 shell。PID 记录以权限 0600 原子写入，含 PID、ps 启动时间、配置路径和随机进程标识。status 区分运行、停止和 stale PID；后者不会收到任何信号。信号发送前再次核验身份。默认 SIGTERM 等待当前原子步骤结束；超时保留进程，只有 stop --force 明确允许 SIGKILL。Windows 明确拒绝后台命令。

日志为状态目录 logs/scheduler.log；logs 支持行数、追加跟随和截断。当前不自带日志轮转，可用外部 copytruncate 或定期归档，避免运行中直接替换日志 inode。PID 和同机锁针对 POSIX 单机部署；不要将其作为跨主机进程 fencing。start/stop 管理本工具启动的进程，launchd/systemd 管理的服务应使用对应服务管理器，不混用。

doctor 运行版本、登录状态（codex login status、antcode/aci auth status）、Git ls-remote 及本地 remote 查询；只读远端，不触发创建或推送。区分缺失、未认证、配置错误、未启用和通过。查询会访问实际远端；dry_run 诊断跳过外部命令。

## 回退语义与恢复

```bash
dtcoder-agentic-dev --config ./runtime/config.yaml rollback --run RUN_ID --step coding --reason '重新实现' --dry-run
dtcoder-agentic-dev --config ./runtime/config.yaml rollback --run RUN_ID --step coding --reason '重新实现' --yes
```

语义是**重新执行目标节点**，使用该节点最新 attempt 输入快照中的 git_head，首次节点用已记录 base_revision；必须属于原运行的 artifact/attempt/base 修订，并是其分支祖先。缺失时拒绝猜测。只有终止/暂停状态可计划与执行，有有效租约、RUNNING attempt、执行锁或同 Issue 其他活动运行则拒绝。

计划展示前后 SHA、远端动作和保留选项，执行需要 --yes。默认先创建 refs/dtcoder/backups/RUN_ID/时间戳-操作ID，指向原工作区 HEAD；--no-backup 还需 --confirm-no-backup。原分支和工作区不 reset，后继独立分支/worktree 建于目标 SHA，原 attempts/artifacts 不改写。原终止运行保持状态；暂停运行明确记为 CANCELLED 并记录 superseded_by，腾出同 Issue 活动唯一性约束。后继保存 rollback_from 和 rollback_operation。

默认关闭未合并 PR、取消跨越的运行中流水线。PR 已合并即使 --keep-pr 也拒绝。--keep-pr/--keep-pipeline 仅保留原远端资源，不把它们冒充为后继新代码的产物；后继有自己的幂等键和外部操作。跨回 create_pr 时，前序已通过 CI 的上下文可保留，但不会自动复用原 PR。

回退意图先写 PENDING，记录操作者、原因、前后 revision、备份引用、逐项远端结果和后继 ID。部分远端失败继续审计其他动作，然后停止创建后继，操作记 FAILED，发失败回退事件。工作区准备失败时已有后继保留 PAUSED；检查后可显式 resume，工作区仍从 rollback_revision 准备，不改为当前 base。

进程崩溃留下 PENDING 时，运行控制拒绝跨过该意图。先 show 查看记录，再：

```bash
dtcoder-agentic-dev --config ./runtime/config.yaml rollback-recover --run RUN_ID --reason '已确认原进程退出' --yes
```

存在符合身份的 PAUSED 后继且所有远端动作有成功审计时，按原目标 SHA 准备工作区并入队；无后继则将旧意图明确记为 ProcessInterrupted/FAILED，之后重新生成计划查询远端。不会把无法证实的部分动作记成成功。所有恢复发出同样的审计事件。

## Linux systemd

[dtcoder-agentic-dev.service](deployment/dtcoder-agentic-dev.service) 使用 %h 相对用户 home 的固定部署约定，不包含开发机路径。按实际虚拟环境与配置位置修改示例，然后放入 ~/.config/systemd/user/：

```bash
systemctl --user daemon-reload
systemctl --user enable --now dtcoder-agentic-dev.service
journalctl --user -u dtcoder-agentic-dev.service -f
systemctl --user stop dtcoder-agentic-dev.service
```

是否开启 linger 由主机管理员决定。服务直接运行前台 run，由 systemd 提供重启和进程生命周期；不要在 ExecStart 调用 start。

## macOS launchd

[launchd 模板](deployment/com.dtcoder.agentic-dev.plist) 使用 __HOME__ 部署占位符，安装时必须替换为当前用户 home，XML 必须正确转义。按虚拟环境位置调整 ProgramArguments，保存到 ~/Library/LaunchAgents/com.dtcoder.agentic-dev.plist 后用 launchctl bootstrap gui/$(id -u) 管理。不要原样加载含占位符的模板。启用前创建 logs 目录并先 init/doctor。停服务使用 launchctl bootout；服务直接运行前台 run，避免与 PID 管理命令混用。

## 声明式任务运维

`agent-auto-dev` 是 `dtcoder-agentic-dev` 的兼容命令别名。init 新增 workflows/document.yaml 与 workflows/local-files.yaml；重复初始化保留用户副本。declarative.directory 和 skills_directory 相对配置目录展开；workflow-definitions 相对 state.directory 固定保存运行快照。

首次验证可使用 local-files.yaml，完全离线执行文件处理与校验。真实 document 默认使用现有 Codex 登录态；本批尚不提供 Claude 登录诊断或 Claude 运行时。doctor 的原有诊断语义保持兼容，不执行模型请求。

命令/脚本默认关闭，通过 tools.allowed_commands 显式放行受信任参数前缀。外部命令以参数数组运行，不使用 shell。节点 timeout 下发给同步命令并在验证/后置动作边界检查，当前没有异步 manager 的进程树取消。停止调度器后，未完 RUNNING 可恢复；PAUSED 不自动执行。

备份人工流程应同时保存 state.db（SQLite 一致性备份）、workflow-definitions、logs 和任务产物；本批不提供自动对象存储备份。恢复定义摘要错误时明确失败，不能用最新模板覆盖历史记录。无仓 cleanup 校验 owner.json、终态、租约和执行锁，拒绝未知目录，定义与 SQLite 历史保留。此机制不代替后续保留期清理、备份互斥或 manifest 服务。

详细任务操作与 YAML 格式见 [workflows.md](workflows.md)。


## Claude 与模型运行时（第二批）

新初始化默认 Claude CLI；安装 Claude Code 后使用其自身 auth login 完成登录。平台只调用 `--version` 和 `auth status` 诊断，不读取凭据或发模型请求。Codex 按 default_engine/model_routes 启用，未启用时 doctor 标记“未启用”；工作流显式选择的其他引擎需要运维人员确保其二进制与登录态可用。

旧配置缺少 agents 时保留 Codex 并输出迁移提示；手工添加 agents.default_engine 即可迁移，init 不覆盖用户配置。包内示例与根示例一致。SDK 为可选依赖 `pip install ".[claude]"`，sdk_fallback 默认关闭；导入缺失以外的错误均不回退。CLI 参数契约依据 [Claude CLI 文档](https://code.claude.com/docs/en/cli-reference)，SDK 契约依据 [SDK Python 文档](https://code.claude.com/docs/en/agent-sdk/python)；真实服务版本/认证兼容仍应在受控环境核验。

pause/cancel 写持久化状态，模型执行轮询状态/租约并回收专属进程组，其他受信任同步 Port 在安全边界完成。取消不使用 pkill、killall 或命令行模糊匹配。CLI 输出限 16 MiB；SDK 限 10,000 个事件及 16 MiB。日志原子落盘并脱敏，不归档登录态。

重启后 run/run-once 继续扫描既有可领取状态；PAUSED/CANCELLED 不自动执行。持久化的历史进程组仍存活时先暂停并拒绝 resume/retry/skip，不尝试杀未知 PID。确认旧运行自然结束后再恢复。缺少 session 或 SDK 归属证明时自动续跑暂停；continue_conversation 不绕过定义/引擎检查。会话丢失应使用 revise 新建尝试，临时故障可用 retry --mode continue_conversation 复用 session。

SDK 崩溃自动接管、结构化 handoff、多仓、Web 服务/面板、资源水位及备份仍未实现。当前上线范围是本机受管工作区和现有单机调度，不宣称多用户云端平台已经完成。

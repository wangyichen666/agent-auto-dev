# 本地 Claude Code 真实端到端验收

2026-10-06，经用户授权使用已安装的 Claude Code 与现有登录态。CLI 版本为 2.1.63，auth status 表示已登录。真实 stream-json 初始化事件上报模型为 deepseek-v4-flash；本次验收针对这套本地 Claude Code 配置，不据此宣称已验证 Anthropic 原生模型服务。

## 验证方法

使用 scripts/e2e_claude.py 显式发起真实调用。每个用例创建独立临时配置、SQLite、无仓工作区和日志目录；使用真实执行器、异步 manager、Dispatcher、文件产物校验、租约、执行锁及反馈持久化。首次调用用例还通过真实 Click submit 命令入口创建并执行任务。验收不读取或更改全局 Claude 配置，不对用户源码仓库执行 Git 写操作。

暂停与取消在模型真实写入 draft.md 后触发，证明 session 与任务进度已产生。随后检查模型进程组退出和租约释放；续聊通过 continue_conversation，核验两次 attempt 的 session 相同、旧 attempt 保留、产物含人工反馈。用例允许窄范围的 sleep 命令，但本次实际工具事件仅出现 Write，暂停发生在等待命令开始前；因此没有把本次记录作为真实 Bash 后代进程中断的证据。

超时用例将节点预算设为 0.2 秒，验证真实 Claude CLI 启动阶段的超时映射和回收；它不是模型推理阶段超时或网络超时的验证。

## 结果与证据

| 用例 | 结果 | 核验 |
| --- | --- | --- |
| 首次无仓任务 | 通过 | 模型生成 report.md，非空/标记校验，SUCCEEDED，session 持久化 |
| submit CLI | 通过 | CLI 返回 0，SQLite 保存成功任务、真实产物与 session |
| 暂停后续聊 | 通过 | PAUSED → 同 session 的独立 attempt → SUCCEEDED；反馈体现在产物中 |
| 取消 | 通过 | run 与 attempt 均 CANCELLED；所属进程组退出，租约释放 |
| 超时 | 通过 | FAILED/TIMEOUT，未伪装为成功 |

每次执行会把 summary.json 保存在打印的验收目录中；该目录还包含 state.db、workflow-definitions、workspaces 和 logs。保留原始运行现场，不自动删除。

开发中有一次脚本误将 TIMEOUT 断言放入 basic 分支；当时 CLI 实际返回 0，任务为 SUCCEEDED，但验收脚本如实记录 FAILED。已修正并新增两个离线分支测试，重新执行 basic CLI 验收；不修改旧失败报告来伪造成功。

最终 basic CLI 验收耗时 9.277 秒，暂停续聊 38.652 秒，取消 6.120 秒，启动超时 0.235 秒。另增加非空目录保护测试，确保拒绝覆盖用户文件；全量离线回归为 333 passed、1 skipped。

## 复现

从仓库根目录执行，使用已安装项目依赖的 Python 环境：

```bash
.venv/bin/python scripts/e2e_claude.py --case basic
.venv/bin/python scripts/e2e_claude.py --case pause-resume
.venv/bin/python scripts/e2e_claude.py --case cancel
.venv/bin/python scripts/e2e_claude.py --case timeout
```

这些命令会调用真实模型并产生用量。可选 --model 指定已可用模型，默认沿用本地 Claude 默认选择；可选 --directory 指定新的验收目录，不允许覆盖已有配置。脚本只接受本地已登录的 claude 二进制，不内置凭据或自动登录。

普通 pytest 继续禁止真实模型和网络；不要通过关闭测试守卫来运行真实验收。Claude SDK 未安装，未做真实 SDK 验收；SDK 的离线契约测试继续保留。当前没有新增多仓、API 或前端的真实验收声明。

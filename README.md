# Codex + Laya for LingYi

面向 LingYi 本地推演的双层智能体扩展。Codex 根据本方态势制定任务计划，Laya 从合法候选中选择具体行动，Python 桥接层通过 LingYi SDK 下达任务并检查执行结果。

本仓库只包含本项目编写的扩展代码、提示词、模板、测试与启动脚本。**不包含 LingYi 本体、厂商 SDK、想定、数据库、地图、Python 运行环境、Laya 源码与模型、账户信息或历史对局。** 使用者需自行取得兼容的 LingYi 发行包及其使用授权。

## 功能

- 每个阵营独立的 Codex 会话；只向其提供本方单位与可见目标。
- 七类任务：巡逻、打击、支援、转场、运输、布雷、扫雷。结构化计划经过校验，再生成合法候选。
- 飞机投入数量与编组选择、挂载与起飞观察、任务归属确认、单位损失记录。
- 按“想定 + 阵营”共用持久 workspace，读取人类指导和历史经验。
- 对局结束后生成战略、战术和游戏操作反思，保存 Markdown，供后续对局读取。
- 从对应房间的引擎日志与回放读取双方分数、来源及逐次加扣分，并核对差额。
- Codex 子进程超时清理与控制台日志。

## 仓库结构

```text
Install.ps1                         # 校验、备份并覆盖安装本模块
SOURCE_MANIFEST.json                # 安装文件及 SHA256
overlay/
  run_codex_laya.ps1                 # 一键启动与结束比分展示
  start_laya_local.ps1
  LingYi/intelligent/ai/
    headless_scenario_run.py         # 推演生命周期与多阵营接入
    codex_mission_planner.py         # Codex 规划与反思
    codex_process.py                 # CLI 子进程管理
    codex_knowledge.py               # skills、workspace、经验管理
    laya_mission_protocol.py         # 计划校验、候选生成、Laya 请求
    laya_command_agent.py            # 白名单任务执行
    scenario_brief.py                # 想定说明与编码兼容
    unit_loss_monitor.py
    score_report.py
    codex_*_output_schema.json
    codex_knowledge/
      reflection_template.md
      skills/lingyi-simulation-guidance/SKILL.md
    test_*.py
docs/MISSION_PROTOCOL.md             # 任务协议与字段说明
```

## 环境与兼容范围

当前装配方式面向 Windows PowerShell、LingYi 单机模式（mode=4）及随发行包提供的 Python 3.9 环境。需要 NVIDIA GPU 与可用的 CUDA PyTorch 来运行默认 Laya 启动配置；脚本默认使用 `cuda`。其他操作系统及 SDK 版本未验证。

SDK 必须包含 `agent.config.LingYiConfig`、`engine.LingYi.entitys.agent_base`、`deduce_RL.Deduction`、`global_util` 和 `gameenvs.common.Struct_pb2`。计分明细读取针对当前 SDK 的回放结构（`THScoreChangeReason`、类型 136）；不同版本可能需要适配。

Codex CLI 需要在 PATH 中可调用，并事先完成登录；本模块直接调用 CLI，无需向 LingYi Python 安装 OpenAI SDK。账号、登录缓存和模型访问权限由使用者自行配置。

## 装配与复现

### 1. 准备 LingYi 目录

以下 `D:\simulation` 指**包含 LingYi 子目录的外层目录**，结构需为：

```text
D:\simulation\
  LingYi\
    Data\lyengine\Plat.exe
    Data\Scenarios\*.scen
    intelligent\ai\agent\config.py
    intelligent\ai\engine\...
    intelligent\new_env\ray210\python.exe
  laya\                              # 下一步单独安装
```

先确认原版 LingYi 能在本机正常加载想定。不要将引擎或 SDK 上传到本仓库。

### 2. 安装扩展

在本仓库目录执行：

```powershell
.\Install.ps1 -TargetRoot 'D:\simulation' -CheckOnly
.\Install.ps1 -TargetRoot 'D:\simulation'
```

脚本只复制清单中的 23 个文件；若目标文件已存在，先备份到目标目录的 `codex_laya_backups`。重复安装会覆盖本模块的同名代码和初始指导文件，先保存个人修改。不会复制或修改厂商 `agent/config.py`，也不会删除已有 workspace 和经验。

手动修改原版 `LingYi\intelligent\ai\agent\config.py`：`get_server_mode_config()` 返回 `4`，单机配置的 `game_server.server.lypath` 指向实际的 `D:\simulation\LingYi\Data\lyengine`。本模块使用原版配置作为基础，在运行时关闭 RL_agents 并接入独立规则智能体。

### 3. 单独准备 Laya

上游为 [NandhaKishorM/laya](https://github.com/NandhaKishorM/laya)。本地开发所用提交为 `ffc142012af63d015fa19f05f29f9c5ff329a2cc`（项目版本 0.4.12）。模型为 [convaiinnovations/laya-multilingual](https://huggingface.co/convaiinnovations/laya-multilingual)，权重版本未锁定，推演结果不保证完全一致。

```powershell
cd D:\simulation
git clone https://github.com/NandhaKishorM/laya.git laya
git -C laya checkout ffc142012af63d015fa19f05f29f9c5ff329a2cc
py -3.12 -m venv laya\.venv
.\laya\.venv\Scripts\python.exe -m pip install -e '.\laya[serve]'
.\laya\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('convaiinnovations/laya-multilingual', local_dir=r'laya/models/laya-multilingual')"
```

按上游说明安装匹配显卡驱动的 CUDA PyTorch，并确认 `torch.cuda.is_available()` 为 True。Laya 使用独立 Python 3.12 环境；不要替换 LingYi 自带的 Python 3.9。启动脚本设置离线加载，因此需预先下载完整模型。

### 4. 检查并启动

在 PowerShell 中先运行 `codex --version`，完成 CLI 登录。随后：

```powershell
cd D:\simulation
.\run_codex_laya.ps1 -Interactive -CompactOutput
# 或明确指定已安装的想定名（不含 .scen）
.\run_codex_laya.ps1 -Scenario '你的想定名' -Sides @('红方','蓝方') -MaxWallMinutes 0 -CompactOutput
```

阵营名必须与想定一致。`-MaxWallMinutes 0` 表示等待自然结束；默认非交互运行有 45 分钟墙钟上限。可通过 `-CodexModel` 指定有访问权限的模型，通过 `-CodexInterval 600` 设置规划间隔（推演秒），通过 `-KnowledgeRoot` 使用另一套知识库，或用 `-DisableReflection` 关闭反思。

运行产物保存在 `LingYi\intelligent\ai\headless_runs\`，包含事件、逐轮态势与计划、反思和 `score_summary.md/json`。非自然结束时显示阶段分数；无法核对的得分来源会显示未知或差额。

## 人类 skills 与自动经验

首次运行按想定、阵营自动创建：

```text
codex_knowledge/workspaces/<想定哈希_名称>/<阵营哈希_名称>/
  workspace.json
  skills/<指导名>/SKILL.md
  experience/*.md
```

控制台打印实际路径。同一想定与阵营的后续对局共用该目录，各局会话和日志仍独立。人类可编辑 `skills/*/SKILL.md`，或放入 Markdown 经验；自动反思写入 `experience`。公共 `codex_knowledge/skills` 作为初始种子，已有 workspace 文件不会被后续初始化覆盖。

当前每局首次规划读取指导（预算约 16k 字符）和最近最多 3 份经验（预算约 12k 字符），不会在局中实时重载；技能附带脚本和引用文件不会自动执行。Codex 在只读沙箱中规划，文件由 Python 保存。这里的“自进化”是历史经验复用，不涉及模型权重训练，也不保证战绩持续提升。

## 测试

安装完成后，在兼容的原版 SDK 环境中运行：

```powershell
cd D:\simulation\LingYi\intelligent\ai
..\new_env\ray210\python.exe -m unittest -v test_codex_knowledge test_codex_process test_laya_command_agent test_headless_multiagent test_unit_loss_monitor test_scenario_brief test_score_report
```

这些测试不调用真实 Codex/Laya，也不启动真实推演；仍需原版 SDK 才能导入部分类型。独立仓库不是脱离 LingYi 的模拟器。

## 已知限制

- 模型输出具有不确定性；任务下达涉及多次 SDK 调用，不能保证原子执行。
- 部分环境中 SDK 启动就绪可能较慢，出现 `reset returned -1`；需查看相应引擎日志，当前版本未实现完整的启动就绪重试。
- 知识文件发布使用硬链接保证并发写入完整性，建议使用本地 NTFS 目录。
- 中途强制终止进程可能无法生成反思或最终计分；得分明细依赖对应房间的日志与回放。

## 许可与第三方依赖

本仓库扩展代码使用 Apache License 2.0，见 [LICENSE](LICENSE)。LingYi 及其 SDK、想定、资源由各自权利人授权，本仓库的许可证不授予这些内容的使用或再分发权。Laya 与模型按其各自上游许可证和模型卡使用。仓库未附带上述第三方实现及权重。

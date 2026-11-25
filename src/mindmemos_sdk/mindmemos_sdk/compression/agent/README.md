# 我们的 Agent：动态阈值、增量／全量切换、异步压缩（ADyn）

## 代码来源

主 Agent 使用上游 [OpenClaw](https://github.com/openclaw/openclaw) `2026.3.11`，任务运行与评分采用固定版本 [WildClawBench](https://github.com/InternLM/WildClawBench/tree/56bc20d31ebf5fdee627c781b55a1e4959893bb0)。与 [Baseline 1](../baselines/baseline1/README.md)、[Baseline 2](../baselines/baseline2/README.md) 使用同一镜像、主模型和[31 项任务列表](../task_list_31.md)。

本目录上传的是我们实际实验中的 **`dual_a` + dynamic + async** 接入实现，来源为对应实验的冻结源码。发布版已删除 ADyn 不使用的历史 prompt，并将 `prompts.py` 精简为仅构造 ADyn 的增量／全量请求；两份实际 prompt、动态阈值、异步调度和提交逻辑保持原样。逐文件来源、原始与发布 SHA-256、删减记录见 [source_manifest.json](source_manifest.json)。

| 代码 | 用途与来源 |
|---|---|
| [runner.py](source/src/agents/openclaw/runner.py) | 基于 WildClawBench OpenClaw runner 增加代理启动、模型接入、关闭和日志收集；这里保留实验版本 |
| [openclaw_bridge.mjs](source/src/context_compression/openclaw_bridge.mjs)、[install_bridge.py](source/src/context_compression/install_bridge.py) | 我们的宿主消息捕获与安装补丁，在原生上下文变换前记录有序的新消息 |
| [core.py](source/src/context_compression/core.py) | 我们的自有历史、完整工具组、动态阈值、快照与记忆提交 |
| [async_core.py](source/src/context_compression/async_core.py) | 我们的后台压缩、增量升级全量、取消及晚到结果处理 |
| [integration.py](source/src/context_compression/integration.py)、[proxy.py](source/src/context_compression/proxy.py) | 容器内代理接入、主请求重建、压缩器和用量日志 |
| [compressor.py](source/src/context_compression/compressor.py)、[cancellation.py](source/src/context_compression/cancellation.py) | 原实验的压缩调用、响应校验、超时和连接取消 |
| [增量 prompt](source/src/context_compression/a_incremental_prompt.txt)、[全量 prompt](source/src/context_compression/a_full_prompt.txt) | 我们的方法使用的两份 prompt；不是 Baseline 2 的原生摘要 prompt |
| [ours_observation](source/src/ours_observation/)、[host_observation](source/src/host_observation/) | 原实验的计量钩子与统计模块 |

实际使用的 prompt 只有两份：

```text
source/src/context_compression/
├── a_incremental_prompt.txt   # 增量：输出新增历史需要追加的记忆
└── a_full_prompt.txt          # 全量：整合旧记忆与可压缩历史
```

`prompts.py` 按程序选定的 `incremental`／`full` 模式读取对应文件，不再加载其他历史模板；不支持的 prompt 方法会明确报错。

## 与已上传压缩 SDK 的关系

上层 [compression SDK](../README.md) 提供 `compress_memory()` 单次压缩操作。本目录补齐 Agent 一侧的历史管理、触发、异步调度与结果应用。

为保留原实验的取消、超时和日志语义，这份复现入口调用冻结源码中的压缩器，不改接 SDK 的 `compress_memory()`。两者的增量／全量 prompt 逐字节相同（SDK 的 `prompts/*_prompt.txt` 与本目录 `a_*_prompt.txt` SHA-256 一致），构造的请求正文相同；运行接口与取消能力不同。这里也不需要启动 MindMemOS 服务。

## 相对两个 baseline 的改动

相对 Baseline 1，我们在每个主模型请求边界维护独立的 `TASK / M / R`，增加动态触发和异步压缩。安装器会修改一次性容器的 Pi Agent loop 捕获点，并为 provider 导出消息转换函数；另安装计量钩子。不能将其描述为完全未修改宿主运行代码。

相对 Baseline 2，本实现用程序在增量与全量模式之间选择，使用我们自己的两份 prompt，并在后台调用 Qwen。主模型不等待刚发出的摘要；到后续请求边界才检查、应用已完成结果。宿主原生 compaction 保持默认开启，没有安装 Baseline 2 的同步补丁，也没有将 `_checkCompaction` 置空。

原始 task 和 system/developer 前缀保留；`M` 是已应用的摘要块，`R` 是尚未被摘要覆盖的原始历史。主请求使用 `prefix + TASK + memory_messages(M) + remaining R`。消息捕获通过序号 delta 去重，即使宿主改写自己的历史，也继续维护我们自己的 M/R。

## 模型、阈值与异步流程

运行配置见 [config.json](config.json)。

| 项目 | 值 |
|---|---|
| 主 Agent | `openrouter/openai/gpt-5.5`；不设置 thinking override |
| 压缩器 | `qwen/qwen3.5-122b-a10b`，`reasoning={"effort":"high"}` |
| 评分模型 | `openai/gpt-5.4` |
| 方法 | `dual_a`，`execution_mode=async`，`dynamic_thresholds=true` |
| 增量门槛下限 | 8,192 |
| 配置中的全量门槛 | 16,384；dynamic 启用时实际门槛按下式计算 |
| 最近历史保留 | `keep_recent_groups=1` |
| 压缩调用超时 | 240 秒 |
| 失败后重试间隔 | `retry_after_steps=1`，由后续请求步骤驱动 |
| 压缩采样与输出长度 | 不显式设置 temperature、max_tokens 或 max_output_tokens |
| 原生上下文管理 | 默认配置；不将主模型 context window 改为压缩门槛 |
| 默认任务并发 | 1；可用 `--workers 1` 或 `--workers 2` |

触发计量为 `E(x) = ceil(UTF8_bytes(canonical_JSON(x)) / 4)`，不是模型 tokenizer 或完整 API 输入。排除 task、固定前缀、工具 schema 和最近完整组；工具调用及其全部结果不能被拆开，不跨越不完整或不支持的多模态边界。

记 `R_e` 为本轮可压缩的原始历史前缀，动态规则为：

```text
recent_max = max(increments[:-1][-5:], default=0)
I = max(8192, 2 * recent_max)
F = 2 * max(I, memory_after_last_full)

R_e 为空：不触发
E(memory_messages(M) + R_e) >= F：全量
否则 E(R_e) >= I：增量
否则：不触发
```

`increments` 记录可压缩历史的非负增长量；窗口排除本次刚记录的增量。`memory_after_last_full` 是最近一次成功应用全量摘要后记忆消息的估值，初始为 0，增量应用不会更新它。

两种模式都把 TASK、M、R_e 提供给压缩器。增量结果追加到 M，保留旧摘要块原文；全量结果替换整个 M。这里只删除快照覆盖的原始历史，压缩运行期间新增的消息继续保留。

每次主请求依次摄入新消息、检查并提交已完成结果、判断新触发、组装请求。提交前核对 session/generation/revision 和快照前缀，并要求替换后的记忆字节数小于原 M + R_e。失败、空结果、过期结果或 `not_smaller` 不删除原历史。

有增量压缩在途且新状态达到全量门槛时，取消旧增量并用新快照启动全量；增量遇到另一个增量、或全量已经在途时，不重复发起。任务结束取消并有限等待收尾，不再应用晚到摘要。这里没有新增主模型输入硬上限，异步期间历史仍可能继续增长。

## 运行与封装改动

Python、固定 WildClawBench、镜像与任务环境准备见[公共依赖](../baselines/README.md)。从 MindMemOS 仓库根目录执行；省略 `--execute` 只校验源码／任务 hash 并打印计划，不启动容器或调用模型：

```bash
python src/mindmemos_sdk/mindmemos_sdk/compression/agent/run.py \
  --benchmark-root /path/to/WildClawBench --all

python src/mindmemos_sdk/mindmemos_sdk/compression/agent/run.py \
  --benchmark-root /path/to/WildClawBench --all \
  --env-file /path/to/private.env \
  --output /path/to/new-adyn-output --execute
```

单任务使用 `--task <task_id>` 替换 `--all`。凭据也可直接从环境提供。输出目录必须是新目录；不自动重跑失败任务。

[run.py](run.py) 和 [bootstrap.py](bootstrap.py) 是新增可移植入口：用参数替代固定本机目录，加载本目录的冻结 runner 和压缩模块，复用外部固定 benchmark 的任务执行与评分，不改写 benchmark 工作区源码。[agent.py](agent.py) 提取原 `ObservedOurs` worker 的观测接入。配置来自原运行设置，仅提取无凭据的压缩参数和原生模型描述。

本入口不带历史批次选择、人工放行或按计量状态暂停后续队列的控制逻辑。原始压缩调用、快照提交、模型注册和计量算法保持原样；prompt 加载入口已限定为 ADyn。两种模式构造的请求正文与原实验实现一致。本入口运行上述 ADyn 配置。

每项任务保存 `result.json`、评分、`chat.jsonl`、`measurement.json`、`meter/` 和 `compression/`，后者含事件、主模型／压缩用量和快照正文审计。`compression_applied` 才表示摘要实际应用。原统计器对未应用调用保留 unknown 视图及可得的 `actual_usage`；比较“已使用”用量时仍须按轨迹和应用事件筛选，不能把它当作完整账户账单。异步压缩累计时长可能与 Agent 重叠，不直接相加。

## 离线验证

```bash
cd src/mindmemos_sdk/mindmemos_sdk/compression/agent/source
python -m unittest discover -s tests/context_compression -t .
```

测试覆盖实际两份 prompt、动态门槛、增量升级全量、异步提交、保留新历史、工具组、取消、失败和日志。旧 prompt 的归档测试已移除，相关 HTTP／审计测试已改为 ADyn 场景。

该测试集使用原 benchmark 的 `src`／`tests` 命名空间，按上述方式独立运行，已排除 MindMemOS 仓库根 pytest 的自动收集。

已准备固定镜像及 benchmark 依赖时，从 MindMemOS 仓库根目录运行：

```bash
python src/mindmemos_sdk/mindmemos_sdk/compression/agent/tests/container_smoke.py \
  --benchmark-root /path/to/WildClawBench
```

该检查使用断网临时容器和本地模拟上游，验证运行接入、原生 CLI、Qwen 路由与异步摘要应用；不运行 benchmark 任务或调用真实模型。发布验证记录见 [validation.json](validation.json)，离线通过不代表重新跑过 31 项实验。源自 WildClawBench 的代码遵循[随附 MIT License](../baselines/LICENSE_WildClawBench)。
